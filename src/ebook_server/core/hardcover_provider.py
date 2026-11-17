"""Hardcover.app metadata provider (GraphQL, bearer token).

API reference: https://docs.hardcover.app/api/getting-started/ (source of truth used for
this module: the official ``hardcoverapp/hardcover-docs`` repository and its published
``schema.graphql``). Only fields that appear in that schema are queried.

The provider is opt-in: list it in ``BOOK_SCANNER_METADATA_PROVIDERS`` *and* set
``BOOK_SCANNER_HARDCOVER_API_TOKEN``. Without a token the constructor raises
:class:`ProviderUnavailable`, so the provider is skipped once instead of failing every
lookup.

Failure semantics (AGENTS.md: provider failures must not look like "no results"):
HTTP 401/403/408/429/5xx, GraphQL ``errors`` and malformed or oversized responses raise
:class:`HardcoverProviderError`. ``resolve_candidates`` records that as
``metadata_provider_failed`` and carries on with the other providers. The token never
appears in exception messages or log events.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from email.message import Message
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import structlog
from django.conf import settings as django_settings

from .identification import (
    DEFAULT_USER_AGENT,
    MAX_RETRY_AFTER_SECONDS,
    _isbn10_to_isbn13,
    _normalize_identifier,
)

logger = structlog.get_logger(__name__)

HARDCOVER_ENDPOINT = "https://api.hardcover.app/v1/graphql"
DEFAULT_TIMEOUT_SECONDS = 10.0
MAX_RESPONSE_BYTES = 1024 * 1024
MAX_RESULTS = 5

# Shared selection for a book. ``contributions(where: contributor_role slug = author)``
# is the documented way to get a book's authors (guide "Getting Book Details").
# Depth note: Hardcover's roadmap announces a maximum query depth of 3; the edition
# query below follows the documented "Edition Details by ISBN" example, which nests
# ``editions > book > contributions > author``.
_BOOK_FIELDS = """
        id
        title
        subtitle
        description
        release_year
        slug
        image { url }
        contributions(where: {contributor_role: {slug: {_eq: "author"}}}, limit: 10) {
            author { name }
        }
"""

ISBN_QUERY = (
    """
query EbookServerEditionByIsbn($where: editions_bool_exp!, $limit: Int!) {
    editions(where: $where, limit: $limit) {
        id
        title
        subtitle
        isbn_10
        isbn_13
        release_year
        image { url }
        publisher { name }
        language { language code2 code3 }
        book {"""
    + _BOOK_FIELDS
    + """        }
    }
}
"""
)

SEARCH_QUERY = """
query EbookServerSearch($query: String!, $perPage: Int!) {
    search(query: $query, query_type: "Book", per_page: $perPage, page: 1) {
        ids
        error
    }
}
"""

BOOKS_BY_ID_QUERY = (
    """
query EbookServerBooksById($ids: [Int!]!) {
    books(where: {id: {_in: $ids}}) {"""
    + _BOOK_FIELDS
    + """    }
}
"""
)


class ProviderUnavailable(ValueError):
    """The provider is configured but cannot run (e.g. no API token)."""


class HardcoverProviderError(OSError):
    """A Hardcover request failed. The message never contains the API token."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


@dataclass(frozen=True)
class _HttpOutcome:
    status: int
    body: bytes
    retry_after: float | None
    error: str | None = None


def _parse_retry_after(headers: Message | None) -> float | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def throttle_hook(provider_id: str) -> None:
    """Client-side pacing hook, called before every outbound Hardcover request.

    PR #63 (``fix/issue-10-provider-rate-limit``) adds
    ``ebook_server.core.rate_limit.throttle_provider(provider_id)``. Once that is on
    master, replace this body with a call to it. Hardcover's free plan allows 60
    requests/minute with a burst of 10, so a 1 request/second limit fits.
    """
    del provider_id


class HardcoverMetadataProvider:
    """Hardcover.app GraphQL provider: ISBN lookup via ``editions``, text via ``search``."""

    provider_id = "hardcover"
    label = "Hardcover"
    endpoint = HARDCOVER_ENDPOINT

    def __init__(self) -> None:
        token = str(getattr(django_settings, "BOOK_SCANNER_HARDCOVER_API_TOKEN", "") or "")
        token = token.strip()
        if token.lower().startswith("bearer "):
            # Hardcover's account page shows the token with a "Bearer " prefix.
            token = token[len("bearer ") :].strip()
        if not token:
            raise ProviderUnavailable("needs an API token (BOOK_SCANNER_HARDCOVER_API_TOKEN)")
        self._token = token
        self._timeout = float(
            getattr(django_settings, "BOOK_SCANNER_HARDCOVER_TIMEOUT_SECONDS", None)
            or DEFAULT_TIMEOUT_SECONDS
        )

    def __repr__(self) -> str:  # never expose the token through repr()
        return f"{type(self).__name__}(endpoint={self.endpoint!r})"

    # ------------------------------------------------------------------ public API

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        if isbn:
            hits = self.lookup_isbn(isbn)
            if hits:
                return hits

        query_parts = [part.strip() for part in (title, author) if part and part.strip()]
        if not query_parts and folder_hint:
            query_parts = [folder_hint]
        if not query_parts:
            return []
        return self.search_text(" ".join(query_parts))

    def lookup_isbn(self, isbn: str) -> list[dict[str, Any]]:
        normalized = _normalize_identifier(isbn)
        if normalized is None:
            return []
        # Never send a null comparison: Hasura can treat ``{_eq: null}`` as "match all".
        if len(normalized) == 10:
            where: dict[str, Any] = {
                "_or": [
                    {"isbn_10": {"_eq": normalized}},
                    {"isbn_13": {"_eq": _isbn10_to_isbn13(normalized)}},
                ]
            }
        else:
            where = {"isbn_13": {"_eq": normalized}}
        data = self._graphql(
            ISBN_QUERY,
            {"where": where, "limit": MAX_RESULTS},
            operation="isbn",
        )
        editions = data.get("editions")
        if not isinstance(editions, list):
            raise HardcoverProviderError("malformed response: editions is not a list")
        return [
            self._edition_payload(edition, normalized)
            for edition in editions[:MAX_RESULTS]
            if isinstance(edition, dict)
        ]

    def search_text(self, query: str) -> list[dict[str, Any]]:
        data = self._graphql(
            SEARCH_QUERY,
            {"query": query[:256], "perPage": MAX_RESULTS},
            operation="search",
        )
        search = data.get("search")
        if not isinstance(search, dict):
            raise HardcoverProviderError("malformed response: search is not an object")
        if search.get("error"):
            raise HardcoverProviderError("search reported an error")
        raw_ids = search.get("ids") or []
        if not isinstance(raw_ids, list):
            raise HardcoverProviderError("malformed response: search.ids is not a list")
        ids = [value for value in raw_ids if isinstance(value, int)][:MAX_RESULTS]
        if not ids:
            return []

        books_data = self._graphql(BOOKS_BY_ID_QUERY, {"ids": ids}, operation="books")
        books = books_data.get("books")
        if not isinstance(books, list):
            raise HardcoverProviderError("malformed response: books is not a list")
        by_id = {book.get("id"): book for book in books if isinstance(book, dict)}
        # Keep the search ranking, which ``books(where: id _in)`` does not preserve.
        return [self._book_payload(by_id[book_id]) for book_id in ids if book_id in by_id]

    # ------------------------------------------------------------------ mapping
    #
    # Payloads use the field names the shared normaliser in identification.py reads
    # (title, author_name, isbn_10/isbn_13, publisher, first_publish_year, language,
    # key, _queried_isbn); Hardcover-specific extras (subtitle, description,
    # cover_url, slug) are kept for review pages and provenance.

    def _book_payload(self, book: dict[str, Any]) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "key": f"hardcover:book:{book.get('id')}",
            "hardcover_book_id": book.get("id"),
            "title": _str_or_none(book.get("title")),
            "subtitle": _str_or_none(book.get("subtitle")),
            "description": _str_or_none(book.get("description")),
            "author_name": _author_names(book.get("contributions")),
            "first_publish_year": _int_or_none(book.get("release_year")),
            "cover_url": _image_url(book.get("image")),
            "slug": _str_or_none(book.get("slug")),
            "source_url": _book_url(book.get("slug")),
        }
        return {key: value for key, value in payload.items() if value not in (None, [], "")}

    def _edition_payload(self, edition: dict[str, Any], queried_isbn: str) -> dict[str, Any]:
        book = _as_dict(edition.get("book"))
        payload = self._book_payload(book) if book else {}
        language = _as_dict(edition.get("language"))
        publisher = _as_dict(edition.get("publisher"))
        edition_values: dict[str, Any] = {
            "key": f"hardcover:edition:{edition.get('id')}",
            "hardcover_edition_id": edition.get("id"),
            "title": _str_or_none(edition.get("title")) or payload.get("title"),
            "subtitle": _str_or_none(edition.get("subtitle")) or payload.get("subtitle"),
            "isbn_10": _str_or_none(edition.get("isbn_10")),
            "isbn_13": _str_or_none(edition.get("isbn_13")),
            "publisher": _str_or_none(publisher.get("name")),
            # ISO 639-2 first, matching Open Library's language codes.
            "language": _str_or_none(language.get("code3")) or _str_or_none(language.get("code2")),
            "first_publish_year": _int_or_none(edition.get("release_year"))
            or payload.get("first_publish_year"),
            "cover_url": _image_url(edition.get("image")) or payload.get("cover_url"),
            "_queried_isbn": queried_isbn,
        }
        payload.update({k: v for k, v in edition_values.items() if v not in (None, [], "")})
        return payload

    # ------------------------------------------------------------------ transport

    def _graphql(self, query: str, variables: dict[str, Any], *, operation: str) -> dict[str, Any]:
        body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
        for attempt in range(2):
            throttle_hook(self.provider_id)
            outcome = self._send(body)
            if outcome.status == 429 and attempt == 0:
                delay = outcome.retry_after if outcome.retry_after is not None else 1.0
                if delay <= MAX_RETRY_AFTER_SECONDS:
                    logger.info(
                        "metadata_provider_rate_limited",
                        provider_id=self.provider_id,
                        operation=operation,
                        retry_after_seconds=delay,
                    )
                    time.sleep(delay)
                    continue
            break

        if outcome.error is not None:
            raise HardcoverProviderError(f"{operation} request failed: {outcome.error}")
        if outcome.status == 401:
            raise HardcoverProviderError(
                "authentication failed (HTTP 401): token missing, invalid or expired",
                status=401,
            )
        if outcome.status == 429:
            raise HardcoverProviderError(
                "rate limited (HTTP 429)",
                status=429,
                retry_after=outcome.retry_after,
            )
        if outcome.status != 200:
            raise HardcoverProviderError(
                f"{operation} request failed (HTTP {outcome.status})",
                status=outcome.status,
                retry_after=outcome.retry_after,
            )
        try:
            document = json.loads(outcome.body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise HardcoverProviderError("malformed response: not JSON") from None
        if not isinstance(document, dict):
            raise HardcoverProviderError("malformed response: not a JSON object")
        if document.get("errors"):
            # Messages may echo the query, never the token; still keep only the count.
            errors = document["errors"]
            count = len(errors) if isinstance(errors, list) else 1
            raise HardcoverProviderError(f"GraphQL returned {count} error(s)")
        data = document.get("data")
        if not isinstance(data, dict):
            raise HardcoverProviderError("malformed response: missing data")
        return data

    def _send(self, body: bytes) -> _HttpOutcome:
        """POST one GraphQL request. Returns an outcome instead of raising, so the
        frames of any exception raised later never hold the Authorization header."""
        user_agent = str(
            getattr(django_settings, "BOOK_SCANNER_METADATA_USER_AGENT", "") or DEFAULT_USER_AGENT
        )
        request = Request(
            self.endpoint,
            data=body,
            method="POST",
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": user_agent,
            },
        )
        try:
            with urlopen(request, timeout=self._timeout) as response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
                status = int(getattr(response, "status", 200))
                retry_after = _parse_retry_after(getattr(response, "headers", None))
        except HTTPError as exc:
            status = exc.code
            retry_after = _parse_retry_after(exc.headers)
            exc.close()
            return _HttpOutcome(status=status, body=b"", retry_after=retry_after)
        except (URLError, OSError, ValueError) as exc:
            return _HttpOutcome(status=0, body=b"", retry_after=None, error=type(exc).__name__)
        if len(payload) > MAX_RESPONSE_BYTES:
            return _HttpOutcome(
                status=status, body=b"", retry_after=None, error="response too large"
            )
        return _HttpOutcome(status=status, body=payload, retry_after=retry_after)


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _str_or_none(value: Any) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    return None


def _image_url(image: Any) -> str | None:
    if not isinstance(image, dict):
        return None
    url = image.get("url")
    if isinstance(url, str) and url.startswith("https://"):
        return url
    return None


def _book_url(slug: Any) -> str | None:
    if isinstance(slug, str) and slug and all(c.isalnum() or c in "-_" for c in slug):
        return f"https://hardcover.app/books/{slug}"
    return None


def _author_names(contributions: Any) -> list[str]:
    if not isinstance(contributions, list):
        return []
    names: list[str] = []
    for contribution in contributions:
        author = contribution.get("author") if isinstance(contribution, dict) else None
        name = author.get("name") if isinstance(author, dict) else None
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names
