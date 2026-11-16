"""Language normalisation/detection, edition-aware Open Library candidates, year field."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from django.test import Client

from ebook_server.core import intake
from ebook_server.core.identification import (
    IdentificationInput,
    OpenLibraryMetadataProvider,
    _normalize_candidate,
    build_identification_input,
    resolve_candidates,
)
from ebook_server.core.languages import detect_language, normalize_language, normalize_languages
from ebook_server.core.models import Book, clean_publication_year, get_or_create_profile
from tests.test_identification_providers import _fake_urlopen

GERMAN_ASCII = (
    "Es war einmal ein armer Holzhacker, der lebte mit seiner Frau und seinen zwei "
    "Kindern an einem grossen Walde. Das Buebchen hiess Haensel und das Maedchen "
    "Gretel. Er hatte wenig zu beissen und zu brechen, und einmal, als grosse "
    "Teuerung ins Land kam, konnte er auch das taegliche Brot nicht mehr schaffen. "
)
ENGLISH = (
    "It was the best of times, it was the worst of times, it was the age of wisdom, "
    "it was the age of foolishness, it was the epoch of belief, it was the epoch of "
    "incredulity, it was the season of Light, it was the season of Darkness. "
)


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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("en", "en"),
        ("EN-us", "en"),
        ("eng", "en"),
        ("ger", "de"),
        ("deu", "de"),
        ("fre", "fr"),
        ("/languages/ger", "de"),
        ("German", "de"),
        ("UND", None),
        ("mul", None),
        ("", None),
        (None, None),
        ("xx", None),
    ],
)
def test_normalize_language(raw: str | None, expected: str | None) -> None:
    assert normalize_language(raw) == expected


def test_normalize_languages_accepts_edition_keys_and_lists() -> None:
    assert normalize_languages([{"key": "/languages/eng"}, {"key": "/languages/ger"}]) == [
        "en",
        "de",
    ]
    assert normalize_languages(["und", "eng", "en"]) == ["en"]
    assert normalize_languages("fre") == ["fr"]


def test_detect_language_handles_ascii_german_and_short_text() -> None:
    detected = detect_language(GERMAN_ASCII * 3)
    assert detected is not None and detected[0] == "de"
    english = detect_language(ENGLISH * 3)
    assert english is not None and english[0] == "en"
    assert detect_language("Kurz.") is None


def test_signal_language_prefers_declared_then_detected() -> None:
    declared = build_identification_input(
        source_path="/x/a.epub",
        original_filename="a.epub",
        extracted_metadata={"language": "eng", "detected_language": {"code": "de"}},
    )
    assert declared.language == "en"
    fallback = build_identification_input(
        source_path="/x/a.epub",
        original_filename="a.epub",
        extracted_metadata={"language": "UND", "detected_language": {"code": "de"}},
    )
    assert fallback.language == "de"


SEARCH_DOC = {
    "key": "/works/OL5724839W",
    "title": "Absolution gap",
    "author_name": ["Alastair Reynolds"],
    "first_publish_year": 2003,
    "language": ["und", "eng", "fre"],
    "isbn": ["0441011586", "9780575074347"],
    "publisher": ["Gollancz", "Ace Books", "Orbit"],
    "editions": {
        "numFound": 10,
        "docs": [
            {
                "key": "/books/OL3301753M",
                "title": "Absolution gap",
                "language": ["eng"],
                "publisher": ["Ace Books"],
                "publish_date": ["2004"],
                "isbn": ["0441011586", "9780441011582"],
            }
        ],
    },
}


def test_search_candidate_uses_best_edition_and_keeps_work_lists() -> None:
    candidate = _normalize_candidate(
        "openlibrary", SEARCH_DOC, _signals(title_hints=["Absolution Gap"])
    )
    assert candidate.publisher == "Ace Books"
    assert candidate.isbn13 == "9780441011582"
    assert candidate.isbn10 == "0441011586"
    assert candidate.language == "en"
    assert set(candidate.languages) == {"en", "fr"}
    assert "9780575074347" in candidate.work_isbns
    assert "Gollancz" in candidate.publishers
    assert candidate.publication_year == 2003


def test_language_code_mismatch_no_longer_blocks_auto_accept() -> None:
    signals = _signals(
        title_hints=["Absolution Gap"], author_hints=["Alastair Reynolds"], language="en"
    )
    candidate = _normalize_candidate("openlibrary", SEARCH_DOC, signals)
    # 0.55 title + 0.30 author + 0.05 language
    assert candidate.score == pytest.approx(0.90)


def test_file_isbn_of_another_edition_matches_the_work() -> None:
    signals = _signals(title_hints=["Absolution Gap"], isbns=["9780575074347"])
    candidate = _normalize_candidate("openlibrary", SEARCH_DOC, signals)
    assert candidate.score >= 0.95


@pytest.mark.parametrize(
    ("file_publisher", "matches"),
    [
        ("Ace", True),
        ("ACE BOOKS", True),
        ("Gollancz Ltd.", True),
        ("Heyne", False),
        ("Books", False),
    ],
)
def test_publisher_bonus_is_fuzzy_but_not_trivial(file_publisher: str, matches: bool) -> None:
    base = _signals(title_hints=["Absolution Gap"])
    with_publisher = _signals(title_hints=["Absolution Gap"], publisher=file_publisher)
    delta = (
        _normalize_candidate("openlibrary", SEARCH_DOC, with_publisher).score
        - _normalize_candidate("openlibrary", SEARCH_DOC, base).score
    )
    assert (delta == pytest.approx(0.05)) is matches


def test_search_requests_fields_and_language() -> None:
    seen: list[str] = []
    with (
        patch(
            "ebook_server.core.identification.urlopen",
            _fake_urlopen({"search.json": {"docs": [SEARCH_DOC]}}, seen),
        ),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [OpenLibraryMetadataProvider()],
        ),
    ):
        candidates = resolve_candidates(
            _signals(
                title_hints=["Absolution Gap"],
                author_hints=["Alastair Reynolds"],
                language="en",
            )
        )
    assert "fields=" in seen[0] and "editions.publisher" in seen[0].replace("%2C", ",")
    assert "lang=en" in seen[0]
    assert candidates[0].score >= 0.86


def test_providers_without_language_keyword_still_work() -> None:
    class LegacyProvider:
        provider_id = "legacy"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, object]]:
            del author, isbn, folder_hint
            return [{"title": title or ""}]

    with patch("ebook_server.core.identification._provider_instances", lambda: [LegacyProvider()]):
        candidates = resolve_candidates(_signals(title_hints=["Dune"], language="en"))
    assert candidates[0].title == "Dune"


def test_isbn_edition_record_languages_are_read() -> None:
    edition = {
        "key": "/books/OL1M",
        "title": "Momo",
        "languages": [{"key": "/languages/ger"}],
        "publishers": ["Thienemann"],
    }
    candidate = _normalize_candidate("openlibrary", edition, _signals(title_hints=["Momo"]))
    assert candidate.language == "de"
    assert candidate.publisher == "Thienemann"


def _write_epub(path: Path, *, language: str, body: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>Haensel und Gretel</dc:title><dc:creator>Grimm</dc:creator>"
            f"<dc:language>{language}</dc:language>"
            "<dc:publisher>Insel Verlag</dc:publisher><dc:date>1857-01-01T00:00:00</dc:date>"
            "</metadata><manifest>"
            '<item id="c1" href="Text/chapter%201.xhtml" media-type="application/xhtml+xml"/>'
            '</manifest><spine><itemref idref="c1"/></spine></package>',
        )
        archive.writestr("OEBPS/Text/chapter 1.xhtml", f"<html><body><p>{body}</p></body></html>")


def test_epub_extracts_publisher_year_and_detects_undetermined_language(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _write_epub(path, language="UND", body=GERMAN_ASCII * 3)
    metadata = intake.extract_metadata(str(path), "epub", original_filename="book.epub")
    assert metadata["publisher"] == "Insel Verlag"
    assert metadata["publication_year"] == 1857
    assert metadata["detected_language"]["code"] == "de"
    assert metadata["detected_language"]["tool"] == "py3langid"


def test_epub_with_declared_language_skips_detection(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _write_epub(path, language="de", body=ENGLISH * 3)
    metadata = intake.extract_metadata(str(path), "epub", original_filename="book.epub")
    assert metadata["language"] == "de"
    assert "detected_language" not in metadata


def test_text_file_records_detected_language(tmp_path: Path) -> None:
    path = tmp_path / "Haensel.txt"
    path.write_text(GERMAN_ASCII * 3)
    metadata = intake.extract_metadata(str(path), "txt", original_filename="Haensel.txt")
    assert metadata["detected_language"]["code"] == "de"


@pytest.mark.parametrize(
    ("value", "expected"),
    [(2003, 2003), ("1857", 1857), (0, None), (10000, None), ("n/a", None), (None, None)],
)
def test_clean_publication_year(value: object, expected: int | None) -> None:
    assert clean_publication_year(value) == expected


@pytest.mark.django_db
def test_book_detail_exposes_year_not_date() -> None:
    profile = get_or_create_profile("reader")
    book = Book.objects.create(title="Absolution Gap", publication_year=2003)
    client = Client()
    client.force_login(profile.user)
    payload = client.get(f"/library/books/{book.id}/").json()
    assert payload["publication_year"] == 2003
    assert "publication_date" not in payload
    page = client.get(f"/library/books/{book.id}/?view=html").content.decode()
    assert "2003" in page
    assert "Jan" not in page
