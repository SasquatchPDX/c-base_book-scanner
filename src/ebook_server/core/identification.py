"""Bibliographic identification helpers and metadata provider abstraction."""

from __future__ import annotations

import importlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import structlog
from django.conf import settings as django_settings
from isbnlib import is_isbn10, is_isbn13, to_isbn13
from rapidfuzz.fuzz import token_set_ratio

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
        title_hints = filename_title_hints

    author_hints = filename_author_hints
    metadata_authors = extracted_metadata.get("authors")
    if isinstance(metadata_authors, list):
        for raw_author in metadata_authors:
            if isinstance(raw_author, str):
                normalized_author = raw_author.strip()
                if normalized_author and normalized_author not in author_hints:
                    author_hints.insert(0, normalized_author)

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
        if isbn:
            isbn_candidate = self._query_isbn(isbn)
            if isbn_candidate:
                return [isbn_candidate]

        # The folder name is only a last-resort hint: for web uploads it is a constant
        # such as "uploads", and adding it to the query makes real titles match nothing.
        query_parts = [part for part in (title, author) if part]
        if not query_parts and folder_hint:
            query_parts = [folder_hint]
        if not query_parts:
            return []
        return self._query_search(" ".join(query_parts))

    def _query_isbn(self, isbn: str) -> dict[str, Any] | None:
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        try:
            payload = self._get_json(url)
        except (OSError, ValueError) as exc:
            logger.warning(
                "metadata_provider_request_failed",
                provider_id=self.provider_id,
                operation="isbn",
                error_type=type(exc).__name__,
            )
            return None
        if not isinstance(payload, dict):
            return None
        payload = dict(payload)
        payload["_queried_isbn"] = isbn
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

    def _query_search(self, query: str) -> list[dict[str, Any]]:
        encoded = urlencode({"q": query, "limit": "5"})
        url = f"https://openlibrary.org/search.json?{encoded}"
        try:
            payload = self._get_json(url)
        except (OSError, ValueError) as exc:
            logger.warning(
                "metadata_provider_request_failed",
                provider_id=self.provider_id,
                operation="search",
                error_type=type(exc).__name__,
            )
            return []
        docs = payload.get("docs", []) if isinstance(payload, dict) else []
        return docs if isinstance(docs, list) else []

    def _get_json(self, url: str) -> Any:
        user_agent = str(
            getattr(django_settings, "BOOK_SCANNER_METADATA_USER_AGENT", "") or DEFAULT_USER_AGENT
        )
        for attempt in range(2):
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
        except (ImportError, AttributeError, TypeError):
            continue
    return providers


def resolve_candidates(signals: IdentificationInput) -> list[IdentificationCandidate]:
    raw_candidates: list[tuple[MetadataProvider, dict[str, Any]]] = []
    for provider in _provider_instances():
        started_at = perf_counter()
        try:
            raw_list = _query_provider(provider, signals)
        except Exception as exc:  # provider boundary: one provider must not abort intake
            logger.exception(
                "metadata_provider_failed",
                provider_id=provider.provider_id,
                duration_seconds=perf_counter() - started_at,
                error_type=type(exc).__name__,
            )
            continue
        logger.info(
            "metadata_provider_completed",
            provider_id=provider.provider_id,
            candidate_count=len(raw_list),
            duration_seconds=perf_counter() - started_at,
        )
        for candidate in raw_list:
            raw_candidates.append((provider, candidate))

    normalized: list[IdentificationCandidate] = []
    for provider, payload in raw_candidates:
        normalized.append(_normalize_candidate(provider.provider_id, payload, signals))

    normalized.sort(key=lambda item: item.score, reverse=True)
    return normalized


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
