"""Hardcover.app metadata provider, with mocked HTTP (no network)."""

from __future__ import annotations

import json
from email.message import Message
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
import structlog
from django.test import override_settings

from ebook_server.core.hardcover_provider import (
    HARDCOVER_ENDPOINT,
    MAX_RESPONSE_BYTES,
    HardcoverMetadataProvider,
    HardcoverProviderError,
    ProviderUnavailable,
)
from ebook_server.core.identification import (
    IdentificationInput,
    _provider_instances,
    resolve_candidates,
)

TOKEN = "hc-test-token-SECRET-1234567890"
PROVIDER_REF = "ebook_server.core.hardcover_provider.HardcoverMetadataProvider"
URLOPEN = "ebook_server.core.hardcover_provider.urlopen"


class _FakeResponse:
    status = 200

    def __init__(self, body: bytes, headers: Message | None = None) -> None:
        self._body = body
        self.headers = headers or Message()

    def read(self, amount: int = -1) -> bytes:
        return self._body if amount < 0 else self._body[:amount]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _json(payload: object) -> _FakeResponse:
    return _FakeResponse(json.dumps(payload).encode())


def _http_error(code: int, retry_after: str | None = None) -> HTTPError:
    headers = Message()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return HTTPError(HARDCOVER_ENDPOINT, code, "error", headers, None)


class _Recorder:
    """Fake ``urlopen`` returning queued responses and recording requests."""

    def __init__(self, *responses: _FakeResponse | BaseException) -> None:
        self.responses = list(responses)
        self.requests: list[Any] = []

    def __call__(self, request: Any, timeout: float = 0) -> _FakeResponse:
        assert timeout > 0
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def body(self, index: int) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(self.requests[index].data)
        return result


def _signals(**overrides: Any) -> IdentificationInput:
    values: dict[str, Any] = {
        "title_hints": [],
        "author_hints": [],
        "isbns": [],
        "folder_hints": [],
        "language": None,
        "publisher": None,
        "description": None,
    }
    values.update(overrides)
    return IdentificationInput(**values)


BOOK = {
    "id": 328491,
    "title": "Project Hail Mary",
    "subtitle": None,
    "description": "A lone astronaut must save the earth.",
    "release_year": 2021,
    "slug": "project-hail-mary",
    "image": {"url": "https://assets.hardcover.app/edition/1/cover.jpg"},
    "contributions": [{"author": {"name": "Andy Weir"}}],
}

EDITION = {
    "id": 30405274,
    "title": "Project Hail Mary",
    "subtitle": "A Novel",
    "isbn_10": "0593135202",
    "isbn_13": "9780593135204",
    "release_year": 2021,
    "image": {"url": "https://assets.hardcover.app/edition/2/cover.jpg"},
    "publisher": {"name": "Ballantine Books"},
    "language": {"language": "English", "code2": "en", "code3": "eng"},
    "book": BOOK,
}


def _provider() -> HardcoverMetadataProvider:
    with override_settings(BOOK_SCANNER_HARDCOVER_API_TOKEN=TOKEN):
        return HardcoverMetadataProvider()


def test_without_token_provider_is_unavailable_and_skipped() -> None:
    with override_settings(BOOK_SCANNER_HARDCOVER_API_TOKEN=""):
        with pytest.raises(ProviderUnavailable):
            HardcoverMetadataProvider()
        with (
            override_settings(BOOK_SCANNER_METADATA_PROVIDERS=[PROVIDER_REF]),
            structlog.testing.capture_logs() as logs,
        ):
            assert _provider_instances() == []
    assert logs[0]["event"] == "metadata_provider_unavailable"


def test_isbn_lookup_posts_graphql_with_bearer_and_maps_edition() -> None:
    recorder = _Recorder(_json({"data": {"editions": [EDITION]}}))
    with patch(URLOPEN, recorder):
        hits = _provider().search("ignored", isbn="978-0-593-13520-4")

    request = recorder.requests[0]
    assert request.full_url == "https://api.hardcover.app/v1/graphql"
    assert request.get_method() == "POST"
    assert request.get_header("Authorization") == f"Bearer {TOKEN}"
    assert request.get_header("Content-type") == "application/json"
    sent = recorder.body(0)
    assert "editions(where: $where" in sent["query"]
    assert sent["variables"]["where"] == {"isbn_13": {"_eq": "9780593135204"}}
    assert len(recorder.requests) == 1  # an ISBN hit needs no text search

    assert len(hits) == 1
    hit = hits[0]
    assert hit["key"] == "hardcover:edition:30405274"
    assert hit["title"] == "Project Hail Mary"
    assert hit["subtitle"] == "A Novel"
    assert hit["author_name"] == ["Andy Weir"]
    assert hit["isbn_13"] == "9780593135204"
    assert hit["isbn_10"] == "0593135202"
    assert hit["publisher"] == "Ballantine Books"
    assert hit["language"] == "eng"
    assert hit["first_publish_year"] == 2021
    assert hit["cover_url"] == "https://assets.hardcover.app/edition/2/cover.jpg"
    assert hit["description"] == "A lone astronaut must save the earth."
    assert hit["source_url"] == "https://hardcover.app/books/project-hail-mary"


def test_isbn10_lookup_never_sends_null_comparisons() -> None:
    recorder = _Recorder(_json({"data": {"editions": []}}), _json({"data": {"search": {}}}))
    with patch(URLOPEN, recorder):
        assert _provider().search("Dune", isbn="0441172717") == []

    where = recorder.body(0)["variables"]["where"]
    assert where == {
        "_or": [
            {"isbn_10": {"_eq": "0441172717"}},
            {"isbn_13": {"_eq": "9780441172719"}},
        ]
    }
    assert "null" not in json.dumps(where)


def test_isbn_hit_scores_through_shared_normaliser() -> None:
    recorder = _Recorder(_json({"data": {"editions": [EDITION]}}))
    signals = _signals(
        title_hints=["Project Hail Mary"],
        author_hints=["Andy Weir"],
        isbns=["9780593135204"],
    )
    with (
        patch(URLOPEN, recorder),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [_provider()],
        ),
    ):
        candidates = resolve_candidates(signals)

    top = candidates[0]
    assert top.provider_id == "hardcover"
    assert top.provider_ref == "hardcover:edition:30405274"
    assert top.isbn13 == "9780593135204"
    assert top.authors == ["Andy Weir"]
    assert top.publisher == "ballantine books"
    assert top.publication_year == 2021
    assert top.score >= 0.95


def test_title_author_search_uses_ids_then_typed_books_query_in_rank_order() -> None:
    other = dict(BOOK, id=11, title="The Martian", slug="the-martian")
    recorder = _Recorder(
        _json({"data": {"search": {"ids": [328491, 11, 999], "error": None}}}),
        # books(where: id _in) does not preserve order; 999 is missing entirely.
        _json({"data": {"books": [other, BOOK]}}),
    )
    with patch(URLOPEN, recorder):
        hits = _provider().search("Project Hail Mary", author="Andy Weir", folder_hint="uploads")

    search_request = recorder.body(0)
    assert search_request["variables"]["query"] == "Project Hail Mary Andy Weir"
    assert "uploads" not in search_request["variables"]["query"]
    assert 'query_type: "Book"' in search_request["query"]
    assert recorder.body(1)["variables"] == {"ids": [328491, 11, 999]}

    assert [hit["key"] for hit in hits] == ["hardcover:book:328491", "hardcover:book:11"]
    assert hits[0]["author_name"] == ["Andy Weir"]
    assert hits[0]["first_publish_year"] == 2021
    assert hits[0]["cover_url"].startswith("https://assets.hardcover.app/")
    # Work-level results carry no edition-specific identifiers.
    assert "isbn_13" not in hits[0]
    assert "publisher" not in hits[0]


def test_search_without_ids_makes_a_single_request() -> None:
    recorder = _Recorder(_json({"data": {"search": {"ids": [], "error": None}}}))
    with patch(URLOPEN, recorder):
        assert _provider().search("Nothing Like This Exists") == []
    assert len(recorder.requests) == 1


@pytest.mark.parametrize("status", [401, 403, 500, 503])
def test_http_errors_are_provider_failures_without_token(status: int) -> None:
    recorder = _Recorder(_http_error(status))
    with patch(URLOPEN, recorder), pytest.raises(HardcoverProviderError) as excinfo:
        _provider().search("Dune")

    assert excinfo.value.status == status
    assert TOKEN not in str(excinfo.value)
    assert TOKEN not in repr(excinfo.value)
    assert len(recorder.requests) == 1


def test_auth_failure_is_logged_as_provider_failure_not_no_results() -> None:
    recorder = _Recorder(_http_error(401))
    with (
        patch(URLOPEN, recorder),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [_provider()],
        ),
        structlog.testing.capture_logs() as logs,
    ):
        candidates = resolve_candidates(_signals(title_hints=["Dune"]))

    assert candidates == []
    events = [entry["event"] for entry in logs]
    assert "metadata_provider_failed" in events
    assert "metadata_provider_completed" not in events
    assert TOKEN not in repr(logs)


def test_rate_limit_retries_once_after_short_retry_after() -> None:
    recorder = _Recorder(
        _http_error(429, retry_after="2"),
        _json({"data": {"search": {"ids": [], "error": None}}}),
    )
    slept: list[float] = []
    with (
        patch(URLOPEN, recorder),
        patch("ebook_server.core.hardcover_provider.time.sleep", slept.append),
    ):
        assert _provider().search("Dune") == []

    assert slept == [2.0]
    assert len(recorder.requests) == 2


def test_rate_limit_with_long_retry_after_fails_without_waiting() -> None:
    recorder = _Recorder(_http_error(429, retry_after="120"))
    slept: list[float] = []
    with (
        patch(URLOPEN, recorder),
        patch("ebook_server.core.hardcover_provider.time.sleep", slept.append),
        pytest.raises(HardcoverProviderError) as excinfo,
    ):
        _provider().search("Dune")

    assert excinfo.value.status == 429
    assert excinfo.value.retry_after == 120.0
    assert slept == []
    assert len(recorder.requests) == 1


def test_repeated_rate_limit_is_a_failure() -> None:
    recorder = _Recorder(_http_error(429, retry_after="1"), _http_error(429, retry_after="1"))
    with (
        patch(URLOPEN, recorder),
        patch("ebook_server.core.hardcover_provider.time.sleep", lambda _: None),
        pytest.raises(HardcoverProviderError) as excinfo,
    ):
        _provider().search("Dune")
    assert excinfo.value.status == 429


@pytest.mark.parametrize(
    "response",
    [
        _FakeResponse(b"<html>gateway error</html>"),
        _json(["not", "an", "object"]),
        _json({"data": None}),
        _json({"data": {"search": "nope"}}),
        _json({"data": {"search": {"ids": "1,2"}}}),
        _json({"data": {"search": {"ids": None, "error": "Search failed"}}}),
        _json({"errors": [{"message": "field 'search' not found"}]}),
        _FakeResponse(b"{" + b" " * MAX_RESPONSE_BYTES + b"}"),
    ],
)
def test_malformed_or_oversized_responses_are_failures(response: _FakeResponse) -> None:
    with patch(URLOPEN, _Recorder(response)), pytest.raises(HardcoverProviderError):
        _provider().search("Dune")


def test_malformed_books_response_is_a_failure() -> None:
    recorder = _Recorder(
        _json({"data": {"search": {"ids": [1]}}}),
        _json({"data": {"books": {"id": 1}}}),
    )
    with patch(URLOPEN, recorder), pytest.raises(HardcoverProviderError):
        _provider().search("Dune")


def test_network_errors_are_failures_without_token() -> None:
    with (
        patch(URLOPEN, _Recorder(URLError(f"boom {TOKEN}"))),
        pytest.raises(HardcoverProviderError) as excinfo,
    ):
        _provider().search("Dune")
    assert TOKEN not in str(excinfo.value)


def test_token_with_bearer_prefix_is_normalised_and_hidden_from_repr() -> None:
    with override_settings(BOOK_SCANNER_HARDCOVER_API_TOKEN=f"Bearer {TOKEN}"):
        provider = HardcoverMetadataProvider()
    recorder = _Recorder(_json({"data": {"search": {"ids": []}}}))
    with patch(URLOPEN, recorder):
        provider.search("Dune")
    assert recorder.requests[0].get_header("Authorization") == f"Bearer {TOKEN}"
    assert TOKEN not in repr(provider)
