"""Bibliographic identification helpers and metadata provider abstraction."""

from __future__ import annotations

import importlib
import json
import re
import socket
import time
from dataclasses import dataclass, field
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import structlog
from django.conf import settings as django_settings
from isbnlib import is_isbn10, is_isbn13, to_isbn13
from rapidfuzz.fuzz import ratio, token_set_ratio, token_sort_ratio
from rapidfuzz.utils import default_process

from .rate_limit import RateLimitWaitExceeded, throttle_provider

logger = structlog.get_logger(__name__)

MAX_AUTHOR_LOOKUPS = 5
MAX_RETRY_AFTER_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 5
# Subject/genre tags are evidence only (#50); keep them bounded so an untrusted file or
# a provider record with thousands of subjects cannot bloat job evidence or candidates.
MAX_SUBJECTS = 25
MAX_SUBJECT_LENGTH = 128
OPEN_LIBRARY_SEARCH_FIELDS = ",".join(
    (
        "key",
        "title",
        "author_name",
        "author_key",
        "first_publish_year",
        "language",
        "edition_count",
        "cover_i",
        "cover_edition_key",
        "subject",
    )
)
DEFAULT_USER_AGENT = (
    "ebook-server (+https://github.com/SasquatchPDX/c-base_book-scanner; "
    "self-hosted hackerspace library)"
)

AUTO_ACCEPT_SCORE = getattr(
    django_settings,
    "BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT",
    0.86,
)
REVIEW_SCORE = getattr(django_settings, "BOOK_SCANNER_IDENTIFICATION_REVIEW", 0.55)
# Evidence key for title/author hints read from the first lines of plain-text books
# (see core.text_header); written by intake, read when building signals.
TEXT_HEADER_EVIDENCE_KEY = "text_header"
# Added to an already linked Book's score when the file is another format of it (same
# source folder, same file name, ``SourceLocation.group_sibling_formats``). With an equal
# title (0.55) it clears auto-accept; alone it does not (#39).
SIBLING_FORMAT_BONUS = getattr(
    django_settings,
    "BOOK_SCANNER_IDENTIFICATION_SIBLING_BONUS",
    0.35,
)
# Title similarity above which two candidates may describe the same book.
SAME_BOOK_TITLE_MIN_SCORE = 0.9
# Author-name variants (same surname, compatible given names) count as a partial match.
AUTHOR_VARIANT_MATCH_SCORE = 0.9
# Minimum rapidfuzz ratio for two given-name tokens to count as spellings of one name.
AUTHOR_GIVEN_NAME_MIN_RATIO = 85.0


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
        """Return raw candidate dicts from external metadata source."""


class ProviderRequestError(OSError):
    """A metadata provider could not answer the request.

    Distinct from "no results": the provider timed out, was unreachable, answered
    with an HTTP error, rate limited us or returned a malformed response. ``reason``
    is a short, safe description (never a URL, query string or credential), shown
    to reviewers and recorded on the search run.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def describe_provider_error(exc: BaseException) -> str:
    """Short, credential-free reason for a provider failure, for reviewers and logs."""
    if isinstance(exc, ProviderRequestError):
        return exc.reason
    if isinstance(exc, RateLimitWaitExceeded):
        return "rate limited (local request budget exhausted)"
    if isinstance(exc, HTTPError):
        if exc.code == 429:
            return "rate limited (HTTP 429)"
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return "timed out"
        return "unreachable"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(exc, (UnicodeDecodeError, json.JSONDecodeError)):
        return "malformed response"
    if isinstance(exc, OSError) and type(exc).__name__.endswith("ProviderError"):
        # Provider-specific errors (e.g. Hardcover's) promise credential-free messages.
        return str(exc)[:200] or type(exc).__name__
    return type(exc).__name__


@dataclass(frozen=True)
class ProviderOutcome:
    """What one provider did during one search run."""

    provider_id: str
    status: str  # "results" | "no_results" | "error"
    result_count: int
    duration_seconds: float
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "result_count": self.result_count,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
        }


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
    subjects: list[str] = field(default_factory=list)


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


def normalize_subjects(values: Any) -> list[str]:
    """Return trimmed, de-duplicated (case-insensitive), bounded subject/genre tags.

    Tags are kept as free text in first-seen order; no taxonomy mapping is applied here.
    """
    subjects: list[str] = []
    seen: set[str] = set()
    for value in _as_str_list(values):
        tag = re.sub(r"\s+", " ", value).strip()
        if not tag or len(tag) > MAX_SUBJECT_LENGTH:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        subjects.append(tag)
        if len(subjects) >= MAX_SUBJECTS:
            break
    return subjects


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


def _header_strings(value: Any) -> list[str]:
    return [item.strip() for item in _as_str_list(value) if item.strip()]


def _apply_text_header_hints(
    header: dict[str, Any],
    title_hints: list[str],
    author_hints: list[str],
) -> tuple[list[str], list[str]]:
    """Merge hints read from a plain-text book's first lines (see ``core.text_header``).

    Precedence for authors: explicit header rules (``Author:``, Gutenberg header,
    ``by ...``, copyright line) replace the filename's "A - B" split guesses; heuristic
    name-like lines are only appended after the other hints, so they never become the
    author sent to providers ahead of them. Hints from an admin-configured source such
    as a folder layout must likewise rank ahead of anything guessed from the text.
    """
    title_hints = list(title_hints)
    author_hints = list(author_hints)
    header_title = header.get("title")
    if isinstance(header_title, str) and header_title.strip():
        header_title = header_title.strip()
        title_hints.insert(1 if title_hints else 0, header_title)
        # A filename part the text names as the title is not an author.
        author_hints = [
            hint for hint in author_hints if _title_match_score(hint, header_title) < 0.95
        ]
    explicit_authors = _header_strings(header.get("authors"))
    if explicit_authors:
        return title_hints, explicit_authors
    author_hints = [
        hint for hint in author_hints if normalize_text(hint) not in {"unknown", "anonymous"}
    ]
    for likely in _header_strings(header.get("likely_authors")):
        if not any(_author_name_similarity(likely, known) for known in author_hints):
            author_hints.append(likely)
    return title_hints, author_hints


# Evidence key holding hints from the source's configured folder layout (#35); written
# by intake from ``core.source_layout``, read here when building signals.
PATH_LAYOUT_EVIDENCE_KEY = "path_layout"


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
        title_hints = filename_title_hints

    author_hints = filename_author_hints
    header = extracted_metadata.get(TEXT_HEADER_EVIDENCE_KEY)
    if isinstance(header, dict):
        title_hints, author_hints = _apply_text_header_hints(header, title_hints, author_hints)
    metadata_authors = extracted_metadata.get("authors")
    if isinstance(metadata_authors, list):
        for raw_author in metadata_authors:
            if isinstance(raw_author, str):
                normalized_author = raw_author.strip()
                if normalized_author and normalized_author not in author_hints:
                    author_hints.insert(0, normalized_author)

    title_hints, author_hints = _apply_path_layout_hints(
        extracted_metadata, list(title_hints), list(author_hints)
    )

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
        language=normalize_text(extracted_metadata.get("language")),
        publisher=normalize_text(extracted_metadata.get("publisher")),
        description=extracted_metadata.get("description")
        if isinstance(extracted_metadata.get("description"), str)
        else None,
    )


def _apply_path_layout_hints(
    extracted_metadata: dict[str, Any],
    title_hints: list[str],
    author_hints: list[str],
) -> tuple[list[str], list[str]]:
    """Merge folder-layout hints (evidence, not truth) into the title/author hints.

    A layout title goes after an embedded title (usually the best signal) and before
    the raw filename guesses. A layout author replaces the filename "A - B" split
    guesses, which add *both* halves as authors, but never outranks embedded authors.
    """
    layout = extracted_metadata.get(PATH_LAYOUT_EVIDENCE_KEY)
    if not isinstance(layout, dict):
        return title_hints, author_hints

    metadata_title = extracted_metadata.get("title")
    has_embedded_title = isinstance(metadata_title, str) and bool(normalize_text(metadata_title))
    layout_title = layout.get("title")
    if isinstance(layout_title, str) and layout_title.strip():
        title_hints.insert(1 if has_embedded_title else 0, layout_title.strip())

    raw_layout_authors = layout.get("authors")
    layout_authors = [
        author.strip()
        for author in (raw_layout_authors if isinstance(raw_layout_authors, list) else [])
        if isinstance(author, str) and author.strip()
    ]
    if layout_authors:
        raw_embedded = extracted_metadata.get("authors")
        embedded = [
            author.strip()
            for author in (raw_embedded if isinstance(raw_embedded, list) else [])
            if isinstance(author, str) and author.strip()
        ]
        known = {normalize_text(author) for author in embedded}
        author_hints = embedded + [a for a in layout_authors if normalize_text(a) not in known]
    return title_hints, author_hints


class OpenLibraryMetadataProvider:
    """Public Open Library metadata provider (primary external source)."""

    provider_id = "openlibrary"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        isbn_error: ProviderRequestError | None = None
        if isbn:
            try:
                isbn_candidate = self._query_isbn(isbn)
            except ProviderRequestError as exc:
                # Still try the text search; the failure is reported only if that
                # finds nothing either, so it is never mistaken for "no results".
                isbn_error = exc
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
        hits = self._query_search(" ".join(query_parts))
        if not hits and isbn_error is not None:
            raise isbn_error
        return hits

    def _query_isbn(self, isbn: str) -> dict[str, Any] | None:
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        try:
            payload = self._get_json(url)
        except HTTPError as exc:
            if exc.code == 404:  # unknown ISBN: a genuine miss, not a failure
                return None
            raise self._request_error("isbn", exc) from exc
        except (OSError, ValueError) as exc:
            raise self._request_error("isbn", exc) from exc
        if not isinstance(payload, dict):
            return None
        payload = dict(payload)
        payload["_queried_isbn"] = isbn
        names = self._resolve_author_names(payload)
        if names:
            payload["author_name"] = names
        return payload

    def _resolve_author_names(self, payload: dict[str, Any]) -> list[str]:
        """Resolve an edition's author references to names (bounded, best effort).

        Many edition records carry no ``authors`` at all, only ``works: [{key}]``;
        the authors are then taken from the (first) work record.
        """
        keys = _open_library_author_keys(payload.get("authors"))
        if not keys:
            keys = self._work_author_keys(payload.get("works"))
        names: list[str] = []
        for key in keys[:MAX_AUTHOR_LOOKUPS]:
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

    def _work_author_keys(self, works: Any) -> list[str]:
        """Author keys of the edition's first work record (``/works/OL...W``)."""
        if not isinstance(works, list) or not works:
            return []
        entry = works[0]
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"/works/OL\d+W", key):
            return []
        try:
            work_payload = self._get_json(f"https://openlibrary.org{key}.json")
        except (OSError, ValueError) as exc:
            logger.warning(
                "metadata_provider_request_failed",
                provider_id=self.provider_id,
                operation="work",
                error_type=type(exc).__name__,
            )
            return []
        if not isinstance(work_payload, dict):
            return []
        return _open_library_author_keys(work_payload.get("authors"))

    def _query_search(self, query: str) -> list[dict[str, Any]]:
        # Open Library's default search field set omits ``subject``; request it explicitly
        # alongside the default fields the candidate normalizer and scorer rely on.
        encoded = urlencode({"q": query, "limit": "5", "fields": OPEN_LIBRARY_SEARCH_FIELDS})
        url = f"https://openlibrary.org/search.json?{encoded}"
        try:
            payload = self._get_json(url)
        except (OSError, ValueError) as exc:
            raise self._request_error("search", exc) from exc
        docs = payload.get("docs", []) if isinstance(payload, dict) else None
        if not isinstance(docs, list):
            raise self._request_error("search", ProviderRequestError("malformed response"))
        return [doc for doc in docs if isinstance(doc, dict)]

    def _request_error(self, operation: str, exc: BaseException) -> ProviderRequestError:
        reason = describe_provider_error(exc)
        logger.warning(
            "metadata_provider_request_failed",
            provider_id=self.provider_id,
            operation=operation,
            error_type=type(exc).__name__,
            reason=reason,
        )
        return ProviderRequestError(reason)

    def _get_json(self, url: str) -> Any:
        user_agent = str(
            getattr(django_settings, "BOOK_SCANNER_METADATA_USER_AGENT", "") or DEFAULT_USER_AGENT
        )
        for attempt in range(2):
            # Proactive client-side pacing (shared across workers via Redis when configured);
            # the 429 handling below is only the fallback when the server still objects.
            throttle_provider(self.provider_id)
            request = Request(url, headers={"User-Agent": user_agent})
            try:
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    if response.status >= 400:
                        raise URLError(f"provider error {response.status}")
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code != 429 or attempt == 1:
                    raise
                time.sleep(_retry_after_seconds(exc))
        raise URLError("provider rate limited")  # pragma: no cover - loop always returns/raises


def _open_library_author_keys(raw_authors: Any) -> list[str]:
    """Author keys from ``[{"key": ...}]`` (editions) or ``[{"author": {"key": ...}}]`` (works)."""
    if not isinstance(raw_authors, list):
        return []
    keys: list[str] = []
    for entry in raw_authors:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        nested = entry.get("author")
        if not isinstance(key, str) and isinstance(nested, dict):
            key = nested.get("key")
        if isinstance(key, str) and re.fullmatch(r"/authors/OL\d+A", key) and key not in keys:
            keys.append(key)
    return keys


def _retry_after_seconds(error: HTTPError) -> float:
    """Bounded Retry-After (seconds form only) for a 429 response."""
    raw = error.headers.get("Retry-After") if error.headers else None
    try:
        delay = float(raw) if raw is not None else 1.0
    except ValueError:
        delay = 1.0
    return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)


def _provider_instances() -> list[MetadataProvider]:
    configured = getattr(
        django_settings,
        "BOOK_SCANNER_METADATA_PROVIDERS",
        ["ebook_server.core.identification.OpenLibraryMetadataProvider"],
    )
    providers: list[MetadataProvider] = []
    for provider_ref in configured:
        if provider_ref == "_disabled_":
            continue
        module_path, _, symbol = provider_ref.rpartition(".")
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, symbol)
            provider = cls()
            providers.append(provider)
        except ValueError as exc:
            # A provider that cannot run as configured (e.g. a missing API key) is
            # skipped with a warning instead of failing every lookup.
            logger.warning(
                "metadata_provider_unavailable",
                provider_ref=provider_ref,
                reason=str(exc),
            )
            continue
        except (ImportError, AttributeError, TypeError):
            continue
    return providers


def resolve_candidates(signals: IdentificationInput) -> list[IdentificationCandidate]:
    candidates, _outcomes = resolve_candidates_with_report(signals)
    return candidates


def resolve_candidates_with_report(
    signals: IdentificationInput,
) -> tuple[list[IdentificationCandidate], list[ProviderOutcome]]:
    """Ranked candidates plus what every configured provider did.

    A failing provider never aborts the run: it is reported as ``error`` with a
    short reason, so a reviewer can tell "the provider was down" from "no match".
    """
    raw_candidates: list[tuple[MetadataProvider, dict[str, Any]]] = []
    outcomes: list[ProviderOutcome] = []
    for provider in _provider_instances():
        started_at = perf_counter()
        try:
            raw_list = _query_provider(provider, signals)
        except Exception as exc:  # provider boundary: one provider must not abort intake
            duration = perf_counter() - started_at
            reason = describe_provider_error(exc)
            # Network-level failures are expected; only unexpected ones need a traceback.
            log = logger.warning if isinstance(exc, OSError) else logger.exception
            log(
                "metadata_provider_failed",
                provider_id=provider.provider_id,
                duration_seconds=duration,
                error_type=type(exc).__name__,
                reason=reason,
            )
            outcomes.append(
                ProviderOutcome(
                    provider_id=provider.provider_id,
                    status="error",
                    result_count=0,
                    duration_seconds=duration,
                    error=reason,
                )
            )
            continue
        duration = perf_counter() - started_at
        logger.info(
            "metadata_provider_completed",
            provider_id=provider.provider_id,
            candidate_count=len(raw_list),
            duration_seconds=duration,
        )
        outcomes.append(
            ProviderOutcome(
                provider_id=provider.provider_id,
                status="results" if raw_list else "no_results",
                result_count=len(raw_list),
                duration_seconds=duration,
            )
        )
        for candidate in raw_list:
            raw_candidates.append((provider, candidate))

    normalized: list[IdentificationCandidate] = []
    for provider, payload in raw_candidates:
        normalized.append(_normalize_candidate(provider.provider_id, payload, signals))

    normalized.sort(key=lambda item: item.score, reverse=True)
    return normalized, outcomes


def _query_provider(
    provider: MetadataProvider,
    signals: IdentificationInput,
) -> list[dict[str, Any]]:
    isbn = signals.isbns[0] if signals.isbns else None
    author = signals.best_author()
    folder_hint = signals.folder_hints[0] if signals.folder_hints else None

    for title in signals.title_hints[:3]:
        hits = provider.search(
            title=title,
            author=author,
            isbn=isbn,
            folder_hint=folder_hint,
        )
        if hits:
            return hits
        # The ISBN lookup does not depend on the title; do not repeat it per title hint.
        isbn = None

    if signals.title_hints:
        return []
    # No title hints at all: try identifiers / author / folder alone.
    return provider.search(
        title=None,
        author=author,
        isbn=isbn,
        folder_hint=folder_hint,
    )


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

    publisher = None
    raw_publisher = payload.get("publisher") or payload.get("publishers")
    if isinstance(raw_publisher, list) and raw_publisher:
        publisher = normalize_text(str(raw_publisher[0]))
    elif isinstance(raw_publisher, str):
        publisher = normalize_text(raw_publisher)

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

    language: str | None = None
    raw_language = payload.get("language")
    if isinstance(raw_language, str):
        language = normalize_text(raw_language)
    elif isinstance(raw_language, list) and raw_language:
        first_language = raw_language[0]
        if isinstance(first_language, str):
            language = normalize_text(first_language)

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
        # Open Library search docs use ``subject``, edition records ``subjects``;
        # ``categories`` is the Google Books volumeInfo shape.
        subjects=normalize_subjects(
            _as_str_list(payload.get("subject"))
            + _as_str_list(payload.get("subjects"))
            + _as_str_list(payload.get("categories"))
        ),
    )
    return _apply_scoring(signals, candidate)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _title_match_score(left: str, right: str) -> float:
    """Similarity of two titles in [0, 1], penalising tokens only one side has.

    ``token_set_ratio`` alone scores 1.0 whenever one title's tokens are a subset of
    the other's ("Foundation" vs "Foundation and Earth"), which are different books.
    Averaging it with ``token_sort_ratio`` keeps word-order tolerance while making
    unmatched tokens cost score; only genuinely equal titles reach 1.0.
    """
    left_processed = default_process(left)
    right_processed = default_process(right)
    if not left_processed or not right_processed:
        return 0.0
    set_score = token_set_ratio(left_processed, right_processed)
    sort_score = token_sort_ratio(left_processed, right_processed)
    return (set_score + sort_score) / 200.0


def _author_name_tokens(name: str) -> list[str]:
    """Lowercased, punctuation-free name tokens with "Last, First" reordered."""
    if name.count(",") == 1:
        last, first = name.split(",")
        name = f"{first} {last}"
    return default_process(name).split()


def _given_names_compatible(left: list[str], right: list[str]) -> bool:
    """True when the shorter given-name list aligns, in order, with the longer one.

    Tokens align when equal, when one is a single-letter initial of the other, or when
    they are close spellings of the same name; unmatched tokens in the longer list
    (a missing middle name or initial) are skipped.
    """
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    position = 0
    for token in shorter:
        while position < len(longer):
            candidate = longer[position]
            position += 1
            if (
                token == candidate
                or (len(token) == 1 and candidate.startswith(token))
                or (len(candidate) == 1 and token.startswith(candidate))
                or ratio(token, candidate) >= AUTHOR_GIVEN_NAME_MIN_RATIO
            ):
                break
        else:
            return False
    return True


def _author_name_similarity(left: str, right: str) -> float:
    """1.0 for the same name, a partial score for compatible variants, else 0.0.

    Variants share the surname and have compatible given names: "Iain M. Banks" vs
    "Iain Banks", "H. P. Lovecraft" vs "H P Lovecraft", "Banks, Iain" vs "Iain Banks".
    A shared surname alone ("Frank Herbert" vs "Brian Herbert") is not a match.
    """
    left_tokens = _author_name_tokens(left)
    right_tokens = _author_name_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    # Equal up to spacing too: "HP Lovecraft" / "H P Lovecraft", "Le Guin" / "LeGuin".
    if "".join(left_tokens) == "".join(right_tokens):
        return 1.0
    if left_tokens[-1] != right_tokens[-1]:
        return 0.0
    left_given = left_tokens[:-1]
    right_given = right_tokens[:-1]
    if not left_given or not right_given:
        return 0.0
    if _given_names_compatible(left_given, right_given):
        return AUTHOR_VARIANT_MATCH_SCORE
    return 0.0


def _author_match_score(left: list[str], right: list[str]) -> float:
    if not left or not right:
        return 0.0
    left_names = list(dict.fromkeys(normalize_text(author) for author in left))
    right_names = list(dict.fromkeys(normalize_text(author) for author in right))
    if not left_names:
        return 0.0
    total = sum(
        max(_author_name_similarity(name, other) for other in right_names) for name in left_names
    )
    return total / len(left_names)


# Creator values that say nothing about who wrote the book: office-suite and PDF
# tool defaults, account names, and "unknown" in a few languages. Compared after
# normalize_text().
_PLACEHOLDER_AUTHORS = frozenset(
    {
        "unknown",
        "unknown author",
        "unbekannt",
        "unbekannter autor",
        "inconnu",
        "anonymous",
        "anonym",
        "administrator",
        "admin",
        "user",
        "owner",
        "author",
        "n/a",
        "none",
        "various",
        "various authors",
        "multiple authors",
        "microsoft office user",
        "microsoft word",
        "windows user",
        "default",
    }
)


def is_placeholder_author(name: str) -> bool:
    """True for creator values such as "Microsoft Office User" that name nobody."""
    return normalize_text(name).strip(" .,;:-_") in _PLACEHOLDER_AUTHORS


def evidence_author_names(metadata: dict[str, Any]) -> list[str]:
    """Author names the file's own embedded metadata gives, without placeholders.

    This is evidence, not truth: it is only used to check a provider match and to
    suggest authors to a reviewer, never written onto a Book automatically.
    """
    raw_authors = metadata.get("authors")
    if not isinstance(raw_authors, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for value in raw_authors:
        if not isinstance(value, str):
            continue
        name = " ".join(value.split()).strip(" ,;")
        key = normalize_text(name)
        if not key or is_placeholder_author(name) or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _author_names_agree(left: str, right: str) -> bool:
    """Same person per the scorer's name matching, or a bare surname on one side."""
    if _author_name_similarity(normalize_text(left), normalize_text(right)) > 0:
        return True
    left_tokens = _author_name_tokens(normalize_text(left))
    right_tokens = _author_name_tokens(normalize_text(right))
    if not left_tokens or not right_tokens:
        return False
    # "Tolkien" vs "J. R. R. Tolkien": a surname-only credit is not a contradiction.
    single_token = len(left_tokens) == 1 or len(right_tokens) == 1
    return single_token and left_tokens[-1] == right_tokens[-1]


def authors_conflict(candidate_authors: list[str], evidence_authors: list[str]) -> bool:
    """True when both sides name authors and none of them is the same person.

    Names are compared with the scorer's author matching (``_author_name_similarity``),
    so "Banks, Iain" and "Iain M. Banks" agree while "Frank Herbert" and "Brian
    Herbert" do not. Missing authors on either side are not a conflict.
    """
    candidates = [name for name in candidate_authors if normalize_text(name)]
    evidence = [name for name in evidence_authors if normalize_text(name)]
    if not candidates or not evidence:
        return False
    return not any(
        _author_names_agree(candidate, other) for candidate in candidates for other in evidence
    )


def _apply_scoring(
    signals: IdentificationInput,
    candidate: IdentificationCandidate,
) -> IdentificationCandidate:
    score = 0.0

    if signals.isbns:
        isbn_matches = {
            value for value in signals.isbns if value in {candidate.isbn10, candidate.isbn13}
        }
        if isbn_matches:
            score += 0.95

    title_scores = [
        _title_match_score(title_hint, candidate.title) for title_hint in signals.title_hints
    ]
    if title_scores:
        score += 0.55 * max(title_scores)

    author_score = _author_match_score(signals.author_hints, candidate.authors)
    score += 0.3 * author_score

    if signals.language and candidate.language and signals.language == candidate.language:
        score += 0.05

    if signals.publisher and candidate.publisher and signals.publisher == candidate.publisher:
        score += 0.05

    if signals.description and candidate.description:
        normalized_signal = normalize_text(signals.description)
        if normalized_signal and normalized_signal in normalize_text(candidate.description):
            score += 0.05

    normalized_candidate = IdentificationCandidate(
        provider_id=candidate.provider_id,
        provider_ref=candidate.provider_ref,
        title=candidate.title,
        authors=candidate.authors,
        isbn10=candidate.isbn10,
        isbn13=candidate.isbn13,
        language=candidate.language,
        description=candidate.description,
        publisher=candidate.publisher,
        edition=candidate.edition,
        publication_year=candidate.publication_year,
        score=min(score, 1.0),
        raw=candidate.raw,
        subjects=candidate.subjects,
    )
    return normalized_candidate


def best_candidate(
    candidates: list[IdentificationCandidate],
) -> IdentificationCandidate | None:
    if not candidates:
        return None
    if candidates[0].score < REVIEW_SCORE:
        return candidates[0]
    return candidates[0]


def score_candidate(
    signals: IdentificationInput,
    candidate: IdentificationCandidate,
) -> IdentificationCandidate:
    """Score any candidate (e.g. an existing library Book) with the provider scorer."""
    return _apply_scoring(signals, candidate)


def same_book(left: IdentificationCandidate, right: IdentificationCandidate) -> bool:
    """Whether two candidates describe the same book.

    Shared ISBN, or near-equal titles with compatible authors (authors are only
    compared when both sides list some).
    """
    left_isbns = {value for value in (left.isbn10, left.isbn13) if value}
    right_isbns = {value for value in (right.isbn10, right.isbn13) if value}
    expanded_left = {isbn for value in left_isbns for isbn in normalize_isbns(value)}
    expanded_right = {isbn for value in right_isbns for isbn in normalize_isbns(value)}
    if expanded_left & expanded_right:
        return True
    if _title_match_score(left.title, right.title) < SAME_BOOK_TITLE_MIN_SCORE:
        return False
    if left.authors and right.authors:
        return _author_match_score(left.authors, right.authors) >= AUTHOR_VARIANT_MATCH_SCORE
    return True
