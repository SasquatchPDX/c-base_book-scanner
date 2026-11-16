"""Bibliographic identification helpers and metadata provider abstraction."""

from __future__ import annotations

import dataclasses
import importlib
import inspect
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, NoReturn, Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import structlog
from django.conf import settings as django_settings
from isbnlib import is_isbn10, is_isbn13, to_isbn13
from rapidfuzz.fuzz import token_set_ratio

from .languages import normalize_language, normalize_languages

logger = structlog.get_logger(__name__)

MAX_AUTHOR_LOOKUPS = 5
MAX_RETRY_AFTER_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 5
DEFAULT_USER_AGENT = "ebook-server (self-hosted hackerspace library)"

AUTO_ACCEPT_SCORE = getattr(
    django_settings,
    "BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT",
    0.86,
)
REVIEW_SCORE = getattr(django_settings, "BOOK_SCANNER_IDENTIFICATION_REVIEW", 0.55)
# Evidence key holding hints from the source's configured folder layout
# (see core.source_layout); written by intake, read when building signals.
PATH_LAYOUT_EVIDENCE_KEY = "path_layout"

# Open Library search returns a reduced field set unless fields are requested.
# Work-level ISBN/publisher lists span all editions; "editions" adds the single
# edition that best matches the query (biased by the ``lang`` parameter).
OPENLIBRARY_SEARCH_FIELDS = ",".join(
    [
        "key",
        "title",
        "subtitle",
        "author_name",
        "first_publish_year",
        "language",
        "isbn",
        "publisher",
        "edition_count",
        "cover_i",
        "editions",
        "editions.key",
        "editions.title",
        "editions.language",
        "editions.publisher",
        "editions.publish_date",
        "editions.isbn",
    ]
)
PUBLISHER_MATCH_RATIO = 90
_PUBLISHER_NOISE = {
    "books",
    "book",
    "publishing",
    "publishers",
    "publisher",
    "press",
    "group",
    "inc",
    "llc",
    "ltd",
    "limited",
    "co",
    "company",
    "corp",
    "verlag",
    "gmbh",
    "ag",
    "the",
    "and",
    "&",
    "media",
    "brand",
}


class ProviderRequestError(RuntimeError):
    """A provider request failed (unreachable, timed out, HTTP error, bad response).

    Distinct from "no results": a provider that answers with nothing returns ``[]``.
    """


PROVIDER_STATUS_RESULTS = "results"
PROVIDER_STATUS_NO_RESULTS = "no_results"
PROVIDER_STATUS_ERROR = "error"


@dataclass(frozen=True)
class ProviderOutcome:
    """What one provider did during one identification run."""

    provider_id: str
    label: str
    status: str
    candidate_count: int = 0
    duration_ms: int = 0
    error: str = ""
    requests: tuple[dict[str, Any], ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "label": self.label,
            "status": self.status,
            "candidate_count": self.candidate_count,
            "duration_ms": self.duration_ms,
            "error": self.error,
            "requests": [dict(item) for item in self.requests],
        }


def describe_request_error(exc: BaseException) -> str:
    """Short, user-facing reason for a failed provider request (no URLs, no payload)."""
    if isinstance(exc, ProviderRequestError):
        return str(exc)
    if isinstance(exc, HTTPError):
        if exc.code == 429:
            return "rate limited (HTTP 429)"
        if exc.code >= 500:
            return f"provider unavailable (HTTP {exc.code})"
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, TimeoutError):
            return "timed out"
        return f"unreachable ({reason})"
    if isinstance(exc, TimeoutError):
        return "timed out"
    if isinstance(exc, ValueError):
        return "invalid response"
    return type(exc).__name__


class MetadataProvider(Protocol):
    """Minimal metadata-provider contract."""

    provider_id: str

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw candidate dicts from external metadata source.

        Providers may additionally accept a ``language`` keyword (normalised ISO
        639-1/-3 code) to bias results; it is only passed when accepted.
        """


@dataclass(frozen=True)
class IdentificationInput:
    title_hints: list[str]
    author_hints: list[str]
    isbns: list[str]
    folder_hints: list[str]
    language: str | None
    publisher: str | None
    description: str | None

    def best_title(self) -> str | None:
        return self.title_hints[0] if self.title_hints else None

    def best_author(self) -> str | None:
        return self.author_hints[0] if self.author_hints else None


@dataclass(frozen=True)
class IdentificationCandidate:
    provider_id: str
    provider_ref: str
    title: str
    authors: list[str]
    isbn10: str | None
    isbn13: str | None
    language: str | None
    description: str | None
    publisher: str | None
    edition: str | None
    publication_year: int | None
    score: float
    raw: dict[str, Any]
    # Comparison-only data: every language/publisher/ISBN-13 known for the work.
    languages: tuple[str, ...] = ()
    publishers: tuple[str, ...] = ()
    work_isbns: tuple[str, ...] = ()


def _normalize_identifier(value: str | None) -> str | None:
    if not value:
        return None

    normalized = re.sub(r"[^0-9Xx]", "", value).upper()
    if is_isbn10(normalized):
        return normalized
    if is_isbn13(normalized):
        return normalized

    if len(normalized) == 10 and _isbn10_valid(normalized):
        return normalized
    if len(normalized) == 13 and _isbn13_valid(normalized):
        return normalized
    return None


def _isbn10_check(isbn10: str) -> bool:
    total = 0
    for index, char in enumerate(isbn10):
        value = 10 if char == "X" else int(char)
        total += value * (10 - index)
    return total % 11 == 0


def _isbn10_to_isbn13(isbn10: str) -> str:
    converted = to_isbn13(isbn10)
    if _isbn13_valid(converted):
        return converted
    body = isbn10[:-1]
    prefix12 = f"978{body}"
    total = 0
    for index, char in enumerate(prefix12):
        value = int(char)
        total += value * (1 if index % 2 == 0 else 3)
    check = (10 - (total % 10)) % 10
    return f"{prefix12}{check}"


def _isbn13_valid(isbn13: str) -> bool:
    if len(isbn13) != 13 or not isbn13.isdigit():
        return False
    if not isbn13.startswith("978") and not isbn13.startswith("979"):
        return False
    check_digit = int(isbn13[-1])
    total = 0
    for index, char in enumerate(isbn13[:-1]):
        value = int(char)
        total += value * (1 if index % 2 == 0 else 3)
    expected = (10 - (total % 10)) % 10
    return expected == check_digit


def _isbn10_valid(isbn10: str) -> bool:
    if not re.fullmatch(r"[0-9]{9}[0-9X]", isbn10):
        return False
    return _isbn10_check(isbn10)


def normalize_isbns(raw: str | None) -> list[str]:
    """Return validated ISBN-10/13 values from loosely formatted string."""
    if not raw:
        return []
    values: list[str] = []
    for token in re.split(r"[\s,;]+", raw):
        normalized = _normalize_identifier(token)
        if not normalized:
            continue
        if normalized not in values:
            values.append(normalized)
        if len(normalized) == 10:
            converted = _isbn10_to_isbn13(normalized)
            if converted not in values:
                values.append(converted)
    return values


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.lower().strip().split())


def parse_filename_signals(raw_filename: str) -> tuple[list[str], list[str]]:
    """Return title and author candidates from filename patterns."""
    stem = Path(raw_filename).stem
    if not stem:
        return [], []

    normalized = stem.replace("_", " ")
    title_hints = [normalized]
    author_hints: list[str] = []

    for sep in [" - ", " -", "- ", "--"]:
        if sep in normalized:
            left, right = normalized.split(sep, 1)
            left = left.strip()
            right = right.strip()
            if left and right:
                title_hints.extend([left, right])
                author_hints.extend([left, right])
            break

    return list(dict.fromkeys([x for x in title_hints if x])), list(
        dict.fromkeys([x for x in author_hints if x]),
    )


def build_identification_input(
    *,
    source_path: str,
    original_filename: str,
    extracted_metadata: dict[str, Any],
) -> IdentificationInput:
    filename_title_hints, filename_author_hints = parse_filename_signals(original_filename)

    metadata_title = extracted_metadata.get("title")
    if isinstance(metadata_title, str) and normalize_text(metadata_title):
        title_hints = [metadata_title] + filename_title_hints
    else:
        title_hints = list(filename_title_hints)

    author_hints = list(filename_author_hints)
    metadata_authors = extracted_metadata.get("authors")
    if isinstance(metadata_authors, list):
        for raw_author in metadata_authors:
            if isinstance(raw_author, str):
                normalized_author = raw_author.strip()
                if normalized_author and normalized_author not in author_hints:
                    author_hints.insert(0, normalized_author)

    layout = extracted_metadata.get(PATH_LAYOUT_EVIDENCE_KEY)
    if isinstance(layout, dict):
        layout_title = layout.get("title")
        if isinstance(layout_title, str) and layout_title.strip():
            # After an embedded title (usually best), before raw filename guesses.
            position = (
                1 if isinstance(metadata_title, str) and normalize_text(metadata_title) else 0
            )
            title_hints.insert(position, layout_title.strip())
        layout_authors = [
            author.strip()
            for author in layout.get("authors") or []
            if isinstance(author, str) and author.strip()
        ]
        if layout_authors:
            # A configured layout is a deliberate statement about where the author is;
            # the filename split guesses (both halves of "A - B") would dilute scoring.
            embedded: list[str] = []
            if isinstance(metadata_authors, list):
                embedded = [a.strip() for a in metadata_authors if isinstance(a, str) and a.strip()]
            known = {normalize_text(h) for h in embedded}
            author_hints = embedded + [a for a in layout_authors if normalize_text(a) not in known]

    title_hints = list(dict.fromkeys([h for h in title_hints if h]))
    author_hints = [h for h in dict.fromkeys(author_hints).keys()] if author_hints else []

    isbns: list[str] = []
    for field_name in ["isbn", "isbn10", "isbn13", "identifier"]:
        value = extracted_metadata.get(field_name)
        if not value:
            continue
        if isinstance(value, str):
            for isbn in normalize_isbns(value):
                if isbn not in isbns:
                    isbns.append(isbn)

    folder_hint = Path(source_path).parent.name
    folder_hints = [folder_hint] if folder_hint else []

    return IdentificationInput(
        title_hints=title_hints,
        author_hints=author_hints,
        isbns=isbns,
        folder_hints=[h for h in folder_hints if h],
        language=_signal_language(extracted_metadata),
        publisher=normalize_text(extracted_metadata.get("publisher")),
        description=extracted_metadata.get("description")
        if isinstance(extracted_metadata.get("description"), str)
        else None,
    )


class HttpMetadataProvider:
    """Shared HTTP plumbing for providers: request log, error reporting, 429 retry."""

    provider_id = "http"
    label = "HTTP provider"

    def __init__(self) -> None:
        # Per-run log of the requests made, read by resolve_candidates_with_report.
        self.request_log: list[dict[str, Any]] = []

    def _log(self, request: str, status: str, *, count: int = 0, error: str = "") -> None:
        entry: dict[str, Any] = {"request": request, "status": status, "count": count}
        if error:
            entry["error"] = error
        self.request_log.append(entry)

    def _request_failed(self, request: str, operation: str, exc: BaseException) -> NoReturn:
        reason = describe_request_error(exc)
        logger.warning(
            "metadata_provider_request_failed",
            provider_id=self.provider_id,
            operation=operation,
            error_type=type(exc).__name__,
        )
        self._log(request, PROVIDER_STATUS_ERROR, error=reason)
        raise ProviderRequestError(f"{self.label}: {reason}") from exc

    def _get_bytes(self, url: str) -> bytes:
        user_agent = str(
            getattr(django_settings, "BOOK_SCANNER_METADATA_USER_AGENT", "") or DEFAULT_USER_AGENT
        )
        for attempt in range(2):
            request = Request(url, headers={"User-Agent": user_agent})
            try:
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    if response.status >= 400:
                        raise URLError(f"provider error {response.status}")
                    return bytes(response.read())
            except HTTPError as exc:
                if exc.code != 429 or attempt == 1:
                    raise
                time.sleep(_retry_after_seconds(exc))
        raise URLError("provider rate limited")  # pragma: no cover - loop always returns/raises

    def _get_json(self, url: str) -> Any:
        return json.loads(self._get_bytes(url).decode("utf-8"))


class OpenLibraryMetadataProvider(HttpMetadataProvider):
    """Public Open Library metadata provider (primary external source)."""

    provider_id = "openlibrary"
    label = "Open Library"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
        language: str | None = None,
    ) -> list[dict[str, Any]]:
        isbn_error: ProviderRequestError | None = None
        if isbn:
            try:
                isbn_candidate = self._query_isbn(isbn)
            except ProviderRequestError as exc:
                isbn_error = exc  # still try the title search below
                isbn_candidate = None
            if isbn_candidate:
                return [isbn_candidate]

        # The folder name is only a last-resort hint: for web uploads it is a constant
        # such as "uploads", and adding it to the query makes real titles match nothing.
        query_parts = [part for part in (title, author) if part]
        if not query_parts and folder_hint:
            query_parts = [folder_hint]
        if not query_parts:
            if isbn_error is not None:
                raise isbn_error
            return []
        return self._query_search(" ".join(query_parts), language=language)

    def _query_isbn(self, isbn: str) -> dict[str, Any] | None:
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        request = f"ISBN {isbn}"
        try:
            payload = self._get_json(url)
        except HTTPError as exc:
            if exc.code == 404:
                self._log(request, PROVIDER_STATUS_NO_RESULTS)
                return None
            self._request_failed(request, "isbn", exc)
        except (OSError, ValueError) as exc:
            self._request_failed(request, "isbn", exc)
        if not isinstance(payload, dict):
            self._log(request, PROVIDER_STATUS_NO_RESULTS)
            return None
        self._log(request, PROVIDER_STATUS_RESULTS, count=1)
        payload = dict(payload)
        payload["_queried_isbn"] = isbn
        if isinstance(payload.get("key"), str):
            payload["_record_url"] = f"https://openlibrary.org{payload['key']}"
        names = self._resolve_author_names(payload)
        if names:
            payload["author_name"] = names
        return payload

    def _resolve_author_names(self, payload: dict[str, Any]) -> list[str]:
        """Resolve edition ``authors: [{key}]`` references to names (bounded, best effort)."""
        raw_authors = payload.get("authors")
        if not isinstance(raw_authors, list):
            return []
        names: list[str] = []
        for entry in raw_authors[:MAX_AUTHOR_LOOKUPS]:
            key = entry.get("key") if isinstance(entry, dict) else None
            if not isinstance(key, str) or not re.fullmatch(r"/authors/OL\d+A", key):
                continue
            try:
                author_payload = self._get_json(f"https://openlibrary.org{key}.json")
            except (OSError, ValueError) as exc:
                logger.warning(
                    "metadata_provider_request_failed",
                    provider_id=self.provider_id,
                    operation="author",
                    error_type=type(exc).__name__,
                )
                continue
            name = author_payload.get("name") if isinstance(author_payload, dict) else None
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return names

    def _query_search(self, query: str, *, language: str | None = None) -> list[dict[str, Any]]:
        params = {"q": query, "limit": "5", "fields": OPENLIBRARY_SEARCH_FIELDS}
        if language and len(language) == 2:
            params["lang"] = language  # prefers editions in the file's language
        encoded = urlencode(params)
        url = f"https://openlibrary.org/search.json?{encoded}"
        request = f'search "{query}"' + (f" (lang={params['lang']})" if "lang" in params else "")
        try:
            payload = self._get_json(url)
        except (OSError, ValueError) as exc:
            self._request_failed(request, "search", exc)
        docs = payload.get("docs", []) if isinstance(payload, dict) else []
        docs = [doc for doc in docs if isinstance(doc, dict)] if isinstance(docs, list) else []
        for doc in docs:
            if isinstance(doc.get("key"), str):
                doc["_record_url"] = f"https://openlibrary.org{doc['key']}"
        self._log(
            request,
            PROVIDER_STATUS_RESULTS if docs else PROVIDER_STATUS_NO_RESULTS,
            count=len(docs),
        )
        return docs


def _retry_after_seconds(error: HTTPError) -> float:
    """Bounded Retry-After (seconds form only) for a 429 response."""
    raw = error.headers.get("Retry-After") if error.headers else None
    try:
        delay = float(raw) if raw is not None else 1.0
    except ValueError:
        delay = 1.0
    return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)


def _configured_provider_refs() -> list[str]:
    configured = getattr(
        django_settings,
        "BOOK_SCANNER_METADATA_PROVIDERS",
        ["ebook_server.core.identification.OpenLibraryMetadataProvider"],
    )
    return [ref for ref in configured if isinstance(ref, str) and ref != "_disabled_"]


def _load_provider(provider_ref: str) -> MetadataProvider:
    module_path, _, symbol = provider_ref.rpartition(".")
    module = importlib.import_module(module_path)
    cls = getattr(module, symbol)
    return cast(MetadataProvider, cls())


def _provider_instances() -> list[MetadataProvider]:
    providers: list[MetadataProvider] = []
    for provider_ref in _configured_provider_refs():
        try:
            providers.append(_load_provider(provider_ref))
        except (ImportError, AttributeError, TypeError, ValueError):
            continue
    return providers


def _provider_load_errors() -> list[tuple[str, str]]:
    """Configured providers that cannot be instantiated: (reference, reason)."""
    errors: list[tuple[str, str]] = []
    for provider_ref in _configured_provider_refs():
        try:
            _load_provider(provider_ref)
        except ValueError as exc:  # provider-reported configuration problem
            errors.append((provider_ref, str(exc)))
        except (ImportError, AttributeError, TypeError) as exc:
            errors.append((provider_ref, f"{type(exc).__name__}: {exc}"))
    return errors


def provider_label(provider: Any) -> str:
    label = getattr(provider, "label", None)
    return label if isinstance(label, str) and label else str(provider.provider_id)


def configured_providers() -> list[dict[str, Any]]:
    """Providers a search will ask, plus configured ones that fail to load."""
    providers = [
        {"provider_id": provider.provider_id, "label": provider_label(provider), "available": True}
        for provider in _provider_instances()
    ]
    providers.extend(
        {"provider_id": ref, "label": ref, "available": False, "error": reason}
        for ref, reason in _provider_load_errors()
    )
    return providers


def resolve_candidates(signals: IdentificationInput) -> list[IdentificationCandidate]:
    return resolve_candidates_with_report(signals)[0]


def resolve_candidates_with_report(
    signals: IdentificationInput,
) -> tuple[list[IdentificationCandidate], list[ProviderOutcome]]:
    """Ask every provider; return ranked candidates and what each provider did."""
    outcomes: list[ProviderOutcome] = [
        ProviderOutcome(
            provider_id=ref,
            label=ref,
            status=PROVIDER_STATUS_ERROR,
            error=f"provider could not be loaded ({reason})",
        )
        for ref, reason in _provider_load_errors()
    ]
    raw_candidates: list[tuple[MetadataProvider, dict[str, Any]]] = []
    for provider in _provider_instances():
        started_at = perf_counter()
        calls: list[str] = []
        error = ""
        raw_list: list[dict[str, Any]] = []
        try:
            raw_list = _query_provider(provider, signals, calls)
        except Exception as exc:  # provider boundary: one provider must not abort intake
            error = describe_request_error(exc)
            logger.warning(
                "metadata_provider_failed",
                provider_id=provider.provider_id,
                duration_seconds=perf_counter() - started_at,
                error_type=type(exc).__name__,
            )
        duration_ms = int((perf_counter() - started_at) * 1000)
        logged = getattr(provider, "request_log", None)
        requests = (
            tuple(dict(item) for item in logged if isinstance(item, dict))
            if isinstance(logged, list) and logged
            else tuple({"request": call} for call in calls)
        )
        if error:
            status = PROVIDER_STATUS_ERROR
        elif raw_list:
            status = PROVIDER_STATUS_RESULTS
        else:
            status = PROVIDER_STATUS_NO_RESULTS
        outcomes.append(
            ProviderOutcome(
                provider_id=provider.provider_id,
                label=provider_label(provider),
                status=status,
                candidate_count=len(raw_list),
                duration_ms=duration_ms,
                error=error,
                requests=requests,
            )
        )
        logger.info(
            "metadata_provider_completed",
            provider_id=provider.provider_id,
            status=status,
            candidate_count=len(raw_list),
            duration_seconds=duration_ms / 1000,
        )
        for candidate in raw_list:
            raw_candidates.append((provider, candidate))

    normalized = [
        _normalize_candidate(provider.provider_id, payload, signals)
        for provider, payload in raw_candidates
    ]
    normalized.sort(key=lambda item: item.score, reverse=True)
    return normalized, outcomes


def _describe_call(title: str | None, author: str | None, isbn: str | None) -> str:
    parts = []
    if isbn:
        parts.append(f"ISBN {isbn}")
    if title:
        parts.append(f'title "{title}"')
    if author:
        parts.append(f'author "{author}"')
    return " + ".join(parts) or "no search terms"


def _query_provider(
    provider: MetadataProvider,
    signals: IdentificationInput,
    calls: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Try the title hints in turn; a failed request does not stop later attempts.

    Raises the last request error when nothing was found and a request failed, so
    an unreachable provider is reported as an error rather than as "no results".
    """
    calls = calls if calls is not None else []
    isbn = signals.isbns[0] if signals.isbns else None
    author = signals.best_author()
    folder_hint = signals.folder_hints[0] if signals.folder_hints else None
    extra: dict[str, Any] = {}
    if signals.language and _accepts_keyword(provider.search, "language"):
        extra["language"] = signals.language
    last_error: Exception | None = None

    attempts: list[str | None] = list(signals.title_hints[:3]) or [None]
    for title in attempts:
        calls.append(_describe_call(title, author, isbn))
        try:
            hits = provider.search(
                title=title,
                author=author,
                isbn=isbn,
                folder_hint=folder_hint,
                **extra,
            )
        except ProviderRequestError as exc:
            last_error = exc
            hits = []
        if hits:
            return hits
        # The ISBN lookup does not depend on the title; do not repeat it per title hint.
        isbn = None
    if last_error is not None:
        raise last_error
    return []


def _signal_language(extracted_metadata: dict[str, Any]) -> str | None:
    """Declared language when usable, otherwise a language detected from the text."""
    declared = normalize_language(_first_str(extracted_metadata.get("language")))
    if declared:
        return declared
    detected = extracted_metadata.get("detected_language")
    if isinstance(detected, dict):
        return normalize_language(_first_str(detected.get("code")))
    return None


def _accepts_keyword(func: Any, name: str) -> bool:
    try:
        parameters = inspect.signature(func).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.name == name or parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters
    )


def _first_str(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return next((item for item in value if isinstance(item, str) and item.strip()), None)
    return None


def _best_edition(payload: dict[str, Any]) -> dict[str, Any]:
    editions = payload.get("editions")
    docs = editions.get("docs") if isinstance(editions, dict) else None
    if isinstance(docs, list) and docs and isinstance(docs[0], dict):
        return docs[0]
    return {}


def _isbn13_set(values: Any) -> set[str]:
    result: set[str] = set()
    for value in _as_str_list(values):
        normalized = _normalize_identifier(value)
        if normalized is None:
            continue
        result.add(_isbn10_to_isbn13(normalized) if len(normalized) == 10 else normalized)
    return result


def _publisher_tokens(value: str) -> set[str]:
    tokens = set(re.findall(r"\w+", normalize_text(value)))
    return tokens - _PUBLISHER_NOISE


def _publisher_match(signal: str | None, candidates: tuple[str, ...]) -> bool:
    if not signal:
        return False
    left = _publisher_tokens(signal)
    if not left:
        return False
    for candidate in candidates:
        right = _publisher_tokens(candidate)
        if right and token_set_ratio(" ".join(sorted(left)), " ".join(sorted(right))) >= (
            PUBLISHER_MATCH_RATIO
        ):
            return True
    return False


def _normalize_candidate(
    provider_id: str,
    payload: dict[str, Any],
    signals: IdentificationInput,
) -> IdentificationCandidate:
    title = payload.get("title") or payload.get("title_suggest") or "Unknown title"
    if not isinstance(title, str):
        title = "Unknown title"

    raw_authors = payload.get("author_name")
    authors = (
        [
            str(value)
            for value in raw_authors
            if isinstance(value, str) and normalize_text(value).strip()
        ]
        if isinstance(raw_authors, list)
        else []
    )

    isbn10 = None
    isbn13 = None
    if isinstance(payload.get("isbn_10"), list) and payload.get("isbn_10"):
        isbn10 = _normalize_identifier(str(payload["isbn_10"][0]))
    elif isinstance(payload.get("isbn_10"), str):
        isbn10 = _normalize_identifier(payload["isbn_10"])

    if isinstance(payload.get("isbn_13"), list) and payload.get("isbn_13"):
        isbn13 = _normalize_identifier(str(payload["isbn_13"][0]))
    elif isinstance(payload.get("isbn_13"), str):
        isbn13 = _normalize_identifier(payload["isbn_13"])

    queried_isbn = _normalize_identifier(str(payload.get("_queried_isbn") or ""))
    if queried_isbn is not None:
        # Edition records can list several ISBNs; the one we looked up must be the one
        # the candidate carries, otherwise the ISBN-match score bonus is lost.
        if len(queried_isbn) == 10:
            isbn10 = queried_isbn
            isbn13 = _isbn10_to_isbn13(queried_isbn)
        else:
            isbn13 = queried_isbn
            isbn10 = next(
                (
                    value
                    for value in _as_str_list(payload.get("isbn_10"))
                    if (normalized := _normalize_identifier(value))
                    and len(normalized) == 10
                    and _isbn10_to_isbn13(normalized) == queried_isbn
                ),
                None,
            )
            if isbn10 is not None:
                isbn10 = _normalize_identifier(isbn10)

    edition = _best_edition(payload)
    if isbn10 is None and isbn13 is None and edition:
        # Search hit: take the ISBNs of the best-matching edition, not of the work.
        for value in _as_str_list(edition.get("isbn")):
            normalized = _normalize_identifier(value)
            if normalized is None:
                continue
            if len(normalized) == 13 and isbn13 is None:
                isbn13 = normalized
            elif len(normalized) == 10 and isbn10 is None:
                isbn10 = normalized
    work_isbns = (
        _isbn13_set(payload.get("isbn"))
        | _isbn13_set(payload.get("isbn_10"))
        | _isbn13_set(payload.get("isbn_13"))
        | _isbn13_set(edition.get("isbn"))
    )

    # Edition record ("publishers") or best edition of a search hit; the work-level
    # "publisher" list spans all editions, so it is only used for comparison.
    edition_publishers = _as_str_list(edition.get("publisher")) or _as_str_list(
        payload.get("publishers")
    )
    work_publishers = _as_str_list(payload.get("publisher"))
    publisher = next((p.strip() for p in edition_publishers if p.strip()), None)
    publishers = tuple(
        dict.fromkeys(p.strip() for p in edition_publishers + work_publishers if p.strip())
    )

    publication_year = None
    raw_publish = payload.get("first_publish_year")
    if raw_publish is None and isinstance(payload.get("publish_date"), str):
        year_match = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", payload["publish_date"])
        if year_match:
            raw_publish = int(year_match.group(1))
    if isinstance(raw_publish, int):
        publication_year = raw_publish
    elif isinstance(raw_publish, str) and raw_publish.isdigit():
        publication_year = int(raw_publish)

    # Edition languages first (edition record: [{"key": "/languages/eng"}]; search
    # edition: ["eng"]), then every language the work is known in.
    edition_languages = normalize_languages(edition.get("language")) or normalize_languages(
        payload.get("languages")
    )
    work_languages = normalize_languages(payload.get("language"))
    languages = tuple(dict.fromkeys(edition_languages + work_languages))
    if edition_languages:
        language: str | None = edition_languages[0]
    elif signals.language and signals.language in work_languages:
        language = signals.language
    else:
        language = work_languages[0] if work_languages else None

    candidate = IdentificationCandidate(
        provider_id=provider_id,
        provider_ref=str(payload.get("key") or payload.get("id") or payload.get("isbn") or ""),
        title=title,
        authors=authors,
        isbn10=isbn10,
        isbn13=isbn13,
        language=language,
        description=payload.get("subtitle") if isinstance(payload.get("subtitle"), str) else None,
        publisher=publisher,
        edition=payload.get("edition_name")
        if isinstance(payload.get("edition_name"), str)
        else None,
        publication_year=publication_year,
        score=0.0,
        raw=payload,
        languages=languages,
        publishers=publishers,
        work_isbns=tuple(sorted(work_isbns)),
    )
    return _apply_scoring(signals, candidate)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _title_match_score(left: str, right: str) -> float:
    return token_set_ratio(normalize_text(left), normalize_text(right)) / 100.0


def _author_match_score(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    left_set = {normalize_text(author) for author in left}
    right_set = {normalize_text(author) for author in right}
    if not left_set:
        return 0.0
    return len(left_set.intersection(right_set)) / len(left_set)


def _apply_scoring(
    signals: IdentificationInput,
    candidate: IdentificationCandidate,
) -> IdentificationCandidate:
    score = 0.0

    if signals.isbns:
        signal_isbns = _isbn13_set(signals.isbns)
        candidate_isbns = _isbn13_set([v for v in (candidate.isbn10, candidate.isbn13) if v]) | set(
            candidate.work_isbns
        )
        if signal_isbns & candidate_isbns:
            score += 0.95

    title_scores = [
        _title_match_score(title_hint, candidate.title) for title_hint in signals.title_hints
    ]
    if title_scores:
        score += 0.55 * max(title_scores)

    author_score = _author_match_score(signals.author_hints, candidate.authors)
    score += 0.3 * author_score

    if signals.language:
        candidate_languages = set(candidate.languages)
        if candidate.language:
            candidate_languages.add(normalize_language(candidate.language) or candidate.language)
        if signals.language in candidate_languages:
            score += 0.05

    if _publisher_match(
        signals.publisher,
        candidate.publishers or ((candidate.publisher,) if candidate.publisher else ()),
    ):
        score += 0.05

    if signals.description and candidate.description:
        normalized_signal = normalize_text(signals.description)
        if normalized_signal and normalized_signal in normalize_text(candidate.description):
            score += 0.05

    return dataclasses.replace(candidate, score=min(score, 1.0))


def best_candidate(
    candidates: list[IdentificationCandidate],
) -> IdentificationCandidate | None:
    if not candidates:
        return None
    if candidates[0].score < REVIEW_SCORE:
        return candidates[0]
    return candidates[0]
