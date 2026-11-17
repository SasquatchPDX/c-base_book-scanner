"""Opt-in DNB and Google Books metadata providers (offline: urlopen is stubbed)."""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse

import pytest
from django.test import override_settings

from ebook_server.core import identification, metadata_providers
from ebook_server.core.identification import IdentificationInput, ProviderRequestError
from ebook_server.core.metadata_providers import (
    DnbMetadataProvider,
    GoogleBooksMetadataProvider,
    clean_dnb_title,
)

API_KEY = "secret-google-key"

DNB_RECORD = """<?xml version="1.0" encoding="UTF-8"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">
  <records><record><recordData>
    <dc xmlns:dc="http://purl.org/dc/elements/1.1/"
        xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
      <dc:title>[Ende] ; Die unendliche Geschichte : Roman / Michael Ende</dc:title>
      <dc:creator>Ende, Michael [Verfasser]</dc:creator>
      <dc:creator>Quadflieg, Roswitha [Illustrator]</dc:creator>
      <dc:publisher>Stuttgart : Thienemann</dc:publisher>
      <dc:date>1979</dc:date>
      <dc:language>ger</dc:language>
      <dc:identifier xsi:type="tel:ISBN">978-3-522-12800-1</dc:identifier>
      <dc:identifier xsi:type="dnb:IDN">790367958</dc:identifier>
    </dc>
  </recordData></record>
  <record><recordData>
    <dc xmlns:dc="http://purl.org/dc/elements/1.1/">
      <dc:title>Ohne IDN</dc:title>
    </dc>
  </recordData></record></records>
</searchRetrieveResponse>
"""


class _Response:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, limit: int = -1) -> bytes:
        return self._body if limit < 0 else self._body[:limit]

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _opener(body: bytes, seen: list[str]) -> Any:
    def opener(request: Any, timeout: float = 0) -> _Response:
        del timeout
        seen.append(request.full_url)
        return _Response(body)

    return opener


def _failing(exc: Exception, seen: list[str]) -> Any:
    def opener(request: Any, timeout: float = 0) -> _Response:
        del timeout
        seen.append(request.full_url)
        raise exc

    return opener


def test_clean_dnb_title_strips_uniform_title_and_responsibility() -> None:
    assert clean_dnb_title("[Ende] ; Die unendliche Geschichte : Roman / Michael Ende") == (
        "Die unendliche Geschichte",
        "Roman",
    )
    assert clean_dnb_title("Momo") == ("Momo", "")


def test_dnb_isbn_lookup_maps_records() -> None:
    seen: list[str] = []
    with patch.object(metadata_providers, "urlopen", _opener(DNB_RECORD.encode(), seen)):
        records = DnbMetadataProvider().search(None, isbn="9783522128001")

    assert len(seen) == 1
    assert parse_qs(urlparse(seen[0]).query)["query"] == ["num=9783522128001"]
    first, second = records
    assert first["key"] == "dnb:790367958"
    assert first["title"] == "Die unendliche Geschichte"
    assert first["subtitle"] == "Roman"
    assert first["author_name"] == ["Michael Ende"]
    assert first["isbn_13"] == ["9783522128001"]
    assert first["publishers"] == ["Thienemann"]
    assert first["source_url"] == "https://d-nb.info/790367958"
    assert "key" not in second  # saved with a per-run result-N key, never key=""


def test_dnb_text_search_escapes_cql_quotes() -> None:
    seen: list[str] = []
    with patch.object(metadata_providers, "urlopen", _opener(DNB_RECORD.encode(), seen)):
        DnbMetadataProvider().search('Die "unendliche" Geschichte', author="Ende")
    query = parse_qs(urlparse(seen[0]).query)["query"][0]
    assert query == 'tit="Die unendliche Geschichte" and per="Ende"'


def test_dnb_failures_raise_and_malformed_xml_is_an_error() -> None:
    seen: list[str] = []
    with patch.object(metadata_providers, "urlopen", _failing(URLError("dns"), seen)):
        with pytest.raises(ProviderRequestError, match="unreachable"):
            DnbMetadataProvider().search("Momo")
    with patch.object(metadata_providers, "urlopen", _opener(b"<not-xml", seen)):
        with pytest.raises(ProviderRequestError, match="malformed response"):
            DnbMetadataProvider().search("Momo")
    entity_bomb = b'<!DOCTYPE x [<!ENTITY a "aaaa">]><x>&a;</x>'
    with patch.object(metadata_providers, "urlopen", _opener(entity_bomb, seen)):
        with pytest.raises(ProviderRequestError, match="malformed response"):
            DnbMetadataProvider().search("Momo")


def test_google_books_is_skipped_without_a_key() -> None:
    with override_settings(
        BOOK_SCANNER_GOOGLE_BOOKS_API_KEY="",
        BOOK_SCANNER_METADATA_PROVIDERS=[
            "ebook_server.core.metadata_providers.GoogleBooksMetadataProvider"
        ],
    ):
        assert identification._provider_instances() == []


@override_settings(BOOK_SCANNER_GOOGLE_BOOKS_API_KEY=API_KEY)
def test_google_books_maps_volumes() -> None:
    body = json.dumps(
        {
            "items": [
                {
                    "id": "abc_123",
                    "volumeInfo": {
                        "title": "Dune",
                        "subtitle": "Deluxe Edition",
                        "authors": ["Frank Herbert"],
                        "publisher": "Ace",
                        "publishedDate": "1990-09-01",
                        "language": "en",
                        "categories": ["Fiction"],
                        "industryIdentifiers": [
                            {"type": "ISBN_10", "identifier": "0441172717"},
                            {"type": "ISBN_13", "identifier": "9780441172719"},
                        ],
                    },
                },
                {"id": "x", "volumeInfo": {}},
            ]
        }
    ).encode()
    seen: list[str] = []
    with patch.object(metadata_providers, "urlopen", _opener(body, seen)):
        records = GoogleBooksMetadataProvider().search("Dune", author="Frank Herbert")

    params = parse_qs(urlparse(seen[0]).query)
    assert params["q"] == ['intitle:"Dune" inauthor:"Frank Herbert"']
    assert params["key"] == [API_KEY]
    assert records == [
        {
            "key": "googlebooks:abc_123",
            "source_url": "https://books.google.com/books?id=abc_123",
            "title": "Dune",
            "subtitle": "Deluxe Edition",
            "author_name": ["Frank Herbert"],
            "isbn_10": ["0441172717"],
            "isbn_13": ["9780441172719"],
            "publishers": ["Ace"],
            "language": "en",
            "publish_date": "1990-09-01",
            "categories": ["Fiction"],
        }
    ]


@override_settings(BOOK_SCANNER_GOOGLE_BOOKS_API_KEY=API_KEY)
def test_google_books_errors_never_leak_the_api_key(caplog: pytest.LogCaptureFixture) -> None:
    seen: list[str] = []
    url = f"https://www.googleapis.com/books/v1/volumes?q=x&key={API_KEY}"
    error = HTTPError(url, 403, f"forbidden {url}", None, None)  # type: ignore[arg-type]
    provider = GoogleBooksMetadataProvider()
    with patch.object(metadata_providers, "urlopen", _failing(error, seen)):
        with pytest.raises(ProviderRequestError) as excinfo:
            provider.search("Dune")
    assert excinfo.value.reason == "HTTP 403"
    assert excinfo.value.__cause__ is None
    assert API_KEY not in repr(provider)
    assert API_KEY not in caplog.text


@override_settings(BOOK_SCANNER_GOOGLE_BOOKS_API_KEY=API_KEY)
def test_google_books_isbn_error_is_reported_when_nothing_else_is_found() -> None:
    seen: list[str] = []
    with patch.object(metadata_providers, "urlopen", _failing(TimeoutError(), seen)):
        with pytest.raises(ProviderRequestError, match="timed out"):
            GoogleBooksMetadataProvider().search(None, isbn="9780441172719")
    assert parse_qs(urlparse(seen[0]).query)["q"] == ["isbn:9780441172719"]


@override_settings(
    BOOK_SCANNER_METADATA_PROVIDERS=["ebook_server.core.metadata_providers.DnbMetadataProvider"],
    BOOK_SCANNER_METADATA_RATE_LIMITS={"dnb": 1000.0},
)
def test_dnb_records_score_through_the_shared_normaliser() -> None:
    signals = IdentificationInput(
        title_hints=["Die unendliche Geschichte"],
        author_hints=["Michael Ende"],
        isbns=["9783522128001"],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )
    seen: list[str] = []
    with patch.object(metadata_providers, "urlopen", _opener(DNB_RECORD.encode(), seen)):
        candidates, outcomes = identification.resolve_candidates_with_report(signals)

    assert [outcome.status for outcome in outcomes] == ["results"]
    best = candidates[0]
    assert best.provider_id == "dnb"
    assert best.isbn13 == "9783522128001"
    assert best.publication_year == 1979
    assert best.score >= identification.AUTO_ACCEPT_SCORE
