"""Language normalisation (#36) and edition-aware Open Library candidates (#37)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch
from zipfile import ZipFile

import pytest

from ebook_server.core import intake
from ebook_server.core.identification import (
    AUTO_ACCEPT_SCORE,
    IdentificationInput,
    OpenLibraryMetadataProvider,
    _normalize_candidate,
    build_identification_input,
    resolve_candidates,
)
from ebook_server.core.languages import normalize_language, normalize_languages
from tests.test_identification_providers import _fake_urlopen


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
        ("en_GB", "en"),
        ("eng", "en"),
        ("ger", "de"),
        ("deu", "de"),
        ("fre", "fr"),
        ("/languages/ger", "de"),
        ("German", "de"),
        ("tlh", "tlh"),  # no ISO 639-1 code: falls back to ISO 639-3
        ("UND", None),
        ("mul", None),
        ("zxx", None),
        ("", None),
        (None, None),
        ("xx", None),
        ("x" * 500, None),
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
    assert normalize_languages(None) == []


def test_signal_language_is_normalised_and_und_is_no_signal() -> None:
    declared = build_identification_input(
        source_path="/x/a.epub", original_filename="a.epub", extracted_metadata={"language": "EN"}
    )
    assert declared.language == "en"
    undetermined = build_identification_input(
        source_path="/x/a.epub", original_filename="a.epub", extracted_metadata={"language": "UND"}
    )
    assert undetermined.language is None
    repeated = build_identification_input(
        source_path="/x/a.epub",
        original_filename="a.epub",
        extracted_metadata={"language": ["", "de-DE"]},
    )
    assert repeated.language == "de"


SEARCH_DOC: dict[str, Any] = {
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


def test_search_candidate_uses_best_edition_and_keeps_work_lists_for_comparison() -> None:
    candidate = _normalize_candidate(
        "openlibrary", SEARCH_DOC, _signals(title_hints=["Absolution Gap"])
    )
    # Concrete values come from the best-matching edition, not the work's first entry.
    assert candidate.publisher == "ace books"
    assert candidate.isbn13 == "9780441011582"
    assert candidate.isbn10 == "0441011586"
    assert candidate.language == "eng"  # stored in the provider's own code form
    assert set(candidate.languages) == {"en", "fr"}
    assert "9780575074347" in candidate.work_isbns
    assert "Gollancz" in candidate.publishers


def test_work_without_edition_does_not_take_an_arbitrary_publisher_or_isbn() -> None:
    work_only = {key: value for key, value in SEARCH_DOC.items() if key != "editions"}
    candidate = _normalize_candidate("openlibrary", work_only, _signals(language="fr"))
    assert candidate.publisher is None
    assert candidate.isbn10 is None and candidate.isbn13 is None
    # The work's entry for the file's language, not simply the first ("und") entry.
    assert candidate.language == "fre"


def test_language_bonus_applies_across_code_systems() -> None:
    base = _signals(title_hints=["Absolution Gap"], author_hints=["Alastair Reynolds"])
    english = _signals(
        title_hints=["Absolution Gap"], author_hints=["Alastair Reynolds"], language="en"
    )
    without = _normalize_candidate("openlibrary", SEARCH_DOC, base).score
    with_language = _normalize_candidate("openlibrary", SEARCH_DOC, english).score
    assert with_language == pytest.approx(without + 0.05)


def test_hardcover_style_three_letter_language_matches_two_letter_signal() -> None:
    payload = {"title": "Momo", "language": "ger", "publisher": "Thienemann"}
    base = _normalize_candidate("hardcover", payload, _signals(title_hints=["Momo"]))
    german = _normalize_candidate(
        "hardcover", payload, _signals(title_hints=["Momo"], language="de")
    )
    assert german.score == pytest.approx(base.score + 0.05)
    assert german.publisher == "thienemann"


def test_isbn_edition_record_languages_and_publishers_are_read() -> None:
    edition = {
        "key": "/books/OL1M",
        "title": "Momo",
        "languages": [{"key": "/languages/ger"}],
        "publishers": ["Thienemann"],
    }
    candidate = _normalize_candidate("openlibrary", edition, _signals(title_hints=["Momo"]))
    assert candidate.language == "ger"
    assert candidate.languages == ("de",)
    assert candidate.publisher == "thienemann"


def test_file_isbn_of_the_matched_edition_is_near_proof() -> None:
    signals = _signals(title_hints=["Absolution Gap"], isbns=["9780441011582"])
    assert _normalize_candidate("openlibrary", SEARCH_DOC, signals).score >= AUTO_ACCEPT_SCORE


def test_file_isbn_of_another_edition_only_proves_the_work() -> None:
    other_edition = _signals(isbns=["9780575074347"])
    score = _normalize_candidate("openlibrary", SEARCH_DOC, other_edition).score
    assert 0 < score < AUTO_ACCEPT_SCORE
    # Together with matching title and author it is strong evidence.
    with_title = _signals(
        title_hints=["Absolution Gap"],
        author_hints=["Alastair Reynolds"],
        isbns=["9780575074347"],
    )
    assert _normalize_candidate("openlibrary", SEARCH_DOC, with_title).score >= AUTO_ACCEPT_SCORE


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


def test_search_requests_isbn_publisher_and_edition_fields() -> None:
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
    fields = seen[0].replace("%2C", ",")
    for name in ("isbn", "publisher", "editions", "editions.isbn", "editions.publisher"):
        assert f",{name}," in f",{fields.split('fields=', 1)[1].split('&', 1)[0]},"
    assert candidates[0].isbn13 == "9780441011582"
    assert candidates[0].score >= AUTO_ACCEPT_SCORE


def _write_epub(path: Path) -> None:
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
            "<dc:language>UND</dc:language>"
            "<dc:publisher>Insel Verlag</dc:publisher><dc:date>1857-01-01T00:00:00</dc:date>"
            "</metadata></package>",
        )


def test_epub_extracts_publisher_and_date(tmp_path: Path) -> None:
    path = tmp_path / "book.epub"
    _write_epub(path)
    metadata = intake.extract_metadata(str(path), "epub", original_filename="book.epub")
    assert metadata["publisher"] == "Insel Verlag"
    assert metadata["date"] == "1857-01-01T00:00:00"
    signals = build_identification_input(
        source_path=str(path), original_filename="book.epub", extracted_metadata=metadata
    )
    assert signals.publisher == "insel verlag"
    assert signals.language is None
