from __future__ import annotations

import json
from email.message import Message
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

from ebook_server.core.identification import (
    IdentificationInput,
    OpenLibraryMetadataProvider,
    resolve_candidates,
)


class _FakeResponse:
    status = 200

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _fake_urlopen(routes: dict[str, object], seen: list[str]) -> Any:
    def opener(request: Any, timeout: float = 0) -> _FakeResponse:
        del timeout
        url = request.full_url
        seen.append(url)
        for fragment, payload in routes.items():
            if fragment in url:
                return _FakeResponse(payload)
        raise HTTPError(url, 404, "not found", Message(), None)

    return opener


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


def test_open_library_query_ignores_folder_hint_when_title_present() -> None:
    seen: list[str] = []
    with patch(
        "ebook_server.core.identification.urlopen",
        _fake_urlopen({"search.json": {"docs": []}}, seen),
    ):
        provider = OpenLibraryMetadataProvider()
        provider.search("Dune", author="Frank Herbert", folder_hint="uploads")
        provider.search(None, folder_hint="uploads")

    assert "uploads" not in seen[0]
    assert "q=Dune+Frank+Herbert&" in seen[0]
    assert "q=uploads" in seen[1]


def test_isbn_lookup_resolves_authors_publisher_and_keeps_queried_isbn() -> None:
    edition = {
        "key": "/books/OL1M",
        "title": "Dune",
        "isbn_10": ["0441172717", "0340960191"],
        "isbn_13": ["9780441172719"],
        "authors": [{"key": "/authors/OL79034A"}],
        "publishers": ["Ace Books"],
        "publish_date": "Aug 1, 1990",
    }
    seen: list[str] = []
    routes = {"/isbn/": edition, "/authors/OL79034A": {"name": "Frank Herbert"}}
    signals = _signals(
        title_hints=["Dune", "Dune (Frank Herbert)"],
        isbns=["0340960191", "9780340960196"],
        folder_hints=["uploads"],
    )
    with (
        patch("ebook_server.core.identification.urlopen", _fake_urlopen(routes, seen)),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [OpenLibraryMetadataProvider()],
        ),
    ):
        candidates = resolve_candidates(signals)

    top = candidates[0]
    assert top.isbn10 == "0340960191"
    assert top.authors == ["Frank Herbert"]
    assert top.publisher == "ace books"
    assert top.publication_year == 1990
    assert top.score >= 0.95
    assert sum("/isbn/" in url for url in seen) == 1


def test_isbn_lookup_is_not_repeated_per_title_hint_when_it_misses() -> None:
    seen: list[str] = []
    signals = _signals(title_hints=["A", "B", "C"], isbns=["9780306406157"])
    with (
        patch(
            "ebook_server.core.identification.urlopen",
            _fake_urlopen({"search.json": {"docs": []}}, seen),
        ),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [OpenLibraryMetadataProvider()],
        ),
    ):
        resolve_candidates(signals)

    assert sum("/isbn/" in url for url in seen) == 1


def test_author_lookups_are_bounded_and_failures_tolerated() -> None:
    edition = {
        "title": "Anthology",
        "authors": [{"key": f"/authors/OL{n}A"} for n in range(1, 9)],
    }
    seen: list[str] = []
    routes = {"/isbn/": edition, "/authors/OL1A": {"name": "First Author"}}
    with patch("ebook_server.core.identification.urlopen", _fake_urlopen(routes, seen)):
        payload = OpenLibraryMetadataProvider()._query_isbn("9780306406157")

    assert payload is not None
    assert payload["author_name"] == ["First Author"]
    assert sum("/authors/" in url for url in seen) == 5


def test_rate_limit_honours_bounded_retry_after_and_generic_user_agent() -> None:
    headers = Message()
    headers["Retry-After"] = "9999"
    calls: list[str] = []
    slept: list[float] = []

    def opener(request: Any, timeout: float = 0) -> _FakeResponse:
        del timeout
        calls.append(request.full_url)
        if len(calls) == 1:
            raise HTTPError(request.full_url, 429, "slow down", headers, None)
        assert "ebook-server" in request.get_header("User-agent")
        assert "@" not in request.get_header("User-agent")
        return _FakeResponse({"docs": [{"title": "Dune"}]})

    with (
        patch("ebook_server.core.identification.urlopen", opener),
        patch("ebook_server.core.identification.time.sleep", slept.append),
    ):
        docs = OpenLibraryMetadataProvider().search("Dune")

    assert docs == [{"title": "Dune"}]
    assert slept == [5.0]
