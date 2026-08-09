"""Bibliographic identification helpers and metadata provider abstraction."""

from __future__ import annotations

import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Protocol
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from django.conf import settings as django_settings

# Optional normalization/similarity libraries improve matching precision when available.
try:
    from isbnlib import is_isbn10, is_isbn13, to_isbn13
except Exception:  # pragma: no cover - optional dependency path
    is_isbn10: Callable[[str], bool] | None = None
    is_isbn13: Callable[[str], bool] | None = None
    to_isbn13: Callable[[str], str] | None = None

try:
    from rapidfuzz.fuzz import token_set_ratio
except Exception:  # pragma: no cover - optional dependency path
    token_set_ratio: Callable[[str, str], int | float] | None = None


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
    if is_isbn10 is not None and is_isbn13 is not None:
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
    if to_isbn13 is not None:
        try:
            converted = to_isbn13(isbn10)
            if _isbn13_valid(converted):
                return converted
        except Exception:
            pass
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
        description=extracted_metadata.get("description") if isinstance(
            extracted_metadata.get("description"), str
        ) else None,
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

        query_parts = [part for part in (title, author, folder_hint) if part]
        if not query_parts:
            return []
        return self._query_search(" ".join(query_parts))

    def _query_isbn(self, isbn: str) -> dict[str, Any] | None:
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        try:
            payload = self._get_json(url)
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        return payload

    def _query_search(self, query: str) -> list[dict[str, Any]]:
        encoded = urlencode({"q": query, "limit": "5"})
        url = f"https://openlibrary.org/search.json?{encoded}"
        try:
            payload = self._get_json(url)
        except (OSError, ValueError):
            return []
        docs = payload.get("docs", []) if isinstance(payload, dict) else []
        return docs if isinstance(docs, list) else []

    def _get_json(self, url: str) -> Any:
        request = Request(url, headers={"User-Agent": "ebook-server"})
        with urlopen(request, timeout=5) as response:
            if response.status >= 400:
                raise URLError(f"provider error {response.status}")
            return json.loads(response.read().decode("utf-8"))


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
        raw_list = _query_provider(provider, signals)
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

    # As a fallback, try non-title search if only identifiers were available.
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
    authors = [
        str(value)
        for value in raw_authors
        if isinstance(value, str) and normalize_text(value).strip()
    ] if isinstance(raw_authors, list) else []

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

    publisher = None
    raw_publisher = payload.get("publisher")
    if isinstance(raw_publisher, list) and raw_publisher:
        publisher = normalize_text(raw_publisher[0])
    elif isinstance(raw_publisher, str):
        publisher = normalize_text(raw_publisher)

    publication_year = None
    raw_publish = payload.get("first_publish_year")
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
        description=payload.get("subtitle")
        if isinstance(payload.get("subtitle"), str)
        else None,
        publisher=publisher,
        edition=payload.get("edition_name")
        if isinstance(payload.get("edition_name"), str)
        else None,
        publication_year=publication_year,
        score=0.0,
        raw=payload,
    )
    return _apply_scoring(signals, candidate)


def _title_match_score(left: str, right: str) -> float:
    if token_set_ratio is not None:
        return token_set_ratio(normalize_text(left), normalize_text(right)) / 100.0
    return SequenceMatcher(None, normalize_text(left), normalize_text(right)).ratio()


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
            value
            for value in signals.isbns
            if value in {candidate.isbn10, candidate.isbn13}
        }
        if isbn_matches:
            score += 0.95

    title_scores = [
        _title_match_score(title_hint, candidate.title)
        for title_hint in signals.title_hints
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
