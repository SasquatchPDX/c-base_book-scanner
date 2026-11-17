"""Language normalisation (#36) and edition-aware Open Library candidates (#37)."""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any
from unittest.mock import patch
from zipfile import ZipFile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core import intake
from ebook_server.core.identification import (
    AUTO_ACCEPT_SCORE,
    EDITION_ISBN_MISMATCH,
    EDITION_ISBN_UNCONFIRMED,
    EDITION_LANGUAGE_MISMATCH,
    EDITION_PUBLISHER_MISMATCH,
    IdentificationInput,
    OpenLibraryMetadataProvider,
    _normalize_candidate,
    build_identification_input,
    edition_conflicts,
    resolve_candidates,
)
from ebook_server.core.languages import normalize_language, normalize_languages
from ebook_server.core.models import (
    Book,
    Identifier,
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage
from tests.browser_fixtures import BrowserMetadataProvider
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
    assert candidate.edition_languages == ("en",)
    assert set(candidate.work_languages) == {"en", "fr"}
    assert candidate.edition_isbns == ("9780441011582",)
    assert "9780575074347" in candidate.work_isbns
    assert candidate.edition_publishers == ("Ace Books",)
    assert "Gollancz" in candidate.work_publishers


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
    assert candidate.edition_languages == ("de",)
    assert candidate.publisher == "thienemann"


def test_file_isbn_of_the_matched_edition_is_near_proof() -> None:
    signals = _signals(title_hints=["Absolution Gap"], isbns=["9780441011582"])
    assert _normalize_candidate("openlibrary", SEARCH_DOC, signals).score >= AUTO_ACCEPT_SCORE


def test_file_isbn_of_another_edition_only_proves_the_work() -> None:
    other_edition = _signals(isbns=["9780575074347"])
    score = _normalize_candidate("openlibrary", SEARCH_DOC, other_edition).score
    assert 0 < score < AUTO_ACCEPT_SCORE
    # With matching title and author the *work* is identified, but the selected edition
    # (Ace, 9780441011582) is not the file's (Gollancz, 9780575074347).
    with_title = _signals(
        title_hints=["Absolution Gap"],
        author_hints=["Alastair Reynolds"],
        isbns=["9780575074347"],
    )
    candidate = _normalize_candidate("openlibrary", SEARCH_DOC, with_title)
    assert candidate.score >= AUTO_ACCEPT_SCORE
    assert edition_conflicts(candidate, with_title) == [EDITION_ISBN_MISMATCH]


@pytest.mark.parametrize(
    ("file_publisher", "matches"),
    [
        ("Ace", True),
        ("ACE BOOKS", True),
        # Gollancz published another edition of the work, not the selected Ace one.
        ("Gollancz Ltd.", False),
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


# --- Reviewer probes through the auto-accept decision (#37) ----------------------
#
# The work-level ISBN/language/publisher lists prove the *work*. Accepting a candidate
# stores its edition (ISBN Identifiers, language, publisher) on the Book, so a file
# whose evidence contradicts the selected edition must not be accepted automatically.

ABSOLUTION_GAP: dict[str, Any] = {
    "title_hints": ["Absolution Gap"],
    "author_hints": ["Alastair Reynolds"],
}


def _resolve_through_open_library(signals: IdentificationInput) -> intake.AutoAcceptDecision:
    with (
        patch(
            "ebook_server.core.identification.urlopen",
            _fake_urlopen({"search.json": {"docs": [SEARCH_DOC]}}, []),
        ),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [OpenLibraryMetadataProvider()],
        ),
    ):
        candidates = resolve_candidates(signals)
    return intake.decide_auto_accept(candidates, ["Alastair Reynolds"], file_evidence=signals)


def test_probe_file_isbn_of_another_edition_is_not_auto_accepted() -> None:
    # The /isbn/ lookup misses; the search hit's best edition is Ace 9780441011582.
    signals = _signals(**ABSOLUTION_GAP, isbns=["9780575074347"])
    decision = _resolve_through_open_library(signals)
    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_EDITION_CONFLICT
    assert decision.details["score"] == 1.0
    assert decision.details["edition_conflicts"] == [EDITION_ISBN_MISMATCH]
    assert decision.details["file_isbns"] == ["9780575074347"]
    assert decision.details["edition_isbns"] == ["9780441011582"]


def test_probe_isbn10_form_of_the_selected_edition_is_accepted() -> None:
    signals = _signals(**ABSOLUTION_GAP, isbns=["0441011586"])
    decision = _resolve_through_open_library(signals)
    assert decision.candidate is not None
    assert decision.candidate.isbn13 == "9780441011582"


def test_probe_french_file_is_not_matched_to_the_english_edition() -> None:
    signals = _signals(**ABSOLUTION_GAP, language="fr")
    decision = _resolve_through_open_library(signals)
    assert decision.candidate is None
    candidate = _normalize_candidate("openlibrary", SEARCH_DOC, signals)
    # "fre" in the work's list no longer earns the language bonus...
    assert candidate.score < AUTO_ACCEPT_SCORE
    # ...and even at a higher score the edition's language contradicts the file.
    assert edition_conflicts(candidate, signals) == [EDITION_LANGUAGE_MISMATCH]
    boosted = intake.decide_auto_accept(
        [dataclasses.replace(candidate, score=1.0)], [], file_evidence=signals
    )
    assert boosted.blocked_reason == intake.BLOCK_EDITION_CONFLICT


def test_probe_gollancz_file_is_not_matched_to_the_ace_edition() -> None:
    signals = _signals(**ABSOLUTION_GAP, publisher="gollancz")
    decision = _resolve_through_open_library(signals)
    assert decision.candidate is None
    candidate = _normalize_candidate("openlibrary", SEARCH_DOC, signals)
    assert candidate.score < AUTO_ACCEPT_SCORE
    assert edition_conflicts(candidate, signals) == [EDITION_PUBLISHER_MISMATCH]
    boosted = intake.decide_auto_accept(
        [dataclasses.replace(candidate, score=1.0)], [], file_evidence=signals
    )
    assert boosted.blocked_reason == intake.BLOCK_EDITION_CONFLICT


def test_english_ace_file_still_auto_accepts_the_matching_edition() -> None:
    signals = _signals(**ABSOLUTION_GAP, language="en", publisher="ace")
    decision = _resolve_through_open_library(signals)
    assert decision.candidate is not None
    assert decision.candidate.publisher == "ace books"


def test_matching_edition_among_several_is_selected() -> None:
    gollancz = {
        "key": "/books/OL2M",
        "language": ["eng"],
        "publisher": ["Gollancz"],
        "isbn": ["9780575074347"],
    }
    french = {"key": "/books/OL3M", "language": ["fre"], "publisher": ["Presses de la Cite"]}
    docs = [*SEARCH_DOC["editions"]["docs"], gollancz, french]
    doc = {**SEARCH_DOC, "editions": {"docs": docs}}
    by_isbn = _signals(**ABSOLUTION_GAP, isbns=["9780575074347"])
    candidate = _normalize_candidate("openlibrary", doc, by_isbn)
    assert candidate.isbn13 == "9780575074347"
    assert candidate.publisher == "gollancz"
    assert edition_conflicts(candidate, by_isbn) == []
    by_language = _signals(**ABSOLUTION_GAP, language="fr")
    candidate = _normalize_candidate("openlibrary", doc, by_language)
    assert candidate.language == "fre"
    assert candidate.score >= AUTO_ACCEPT_SCORE
    assert edition_conflicts(candidate, by_language) == []


def test_candidate_without_isbn_does_not_confirm_a_file_isbn() -> None:
    work_only = {key: value for key, value in SEARCH_DOC.items() if key not in {"editions", "isbn"}}
    signals = _signals(**ABSOLUTION_GAP, isbns=["9780575074347"], language="en")
    candidate = _normalize_candidate("openlibrary", work_only, signals)
    assert candidate.score >= AUTO_ACCEPT_SCORE
    assert edition_conflicts(candidate, signals) == [EDITION_ISBN_UNCONFIRMED]


def test_isbn_confirmed_edition_tolerates_an_imprint_publisher() -> None:
    signals = _signals(**ABSOLUTION_GAP, isbns=["9780441011582"], publisher="penguin random house")
    candidate = _normalize_candidate("openlibrary", SEARCH_DOC, signals)
    assert edition_conflicts(candidate, signals) == []


class _StubProvider:
    def __init__(self, provider_id: str, payload: dict[str, Any]) -> None:
        self.provider_id = provider_id
        self.payload = payload

    def search(self, title: str | None = None, **_: Any) -> list[dict[str, Any]]:
        del title
        return [self.payload]


def test_tied_matching_edition_ranks_before_the_conflicting_one() -> None:
    edition_record = {
        "key": "/books/OL2M",
        "title": "Absolution gap",
        "author_name": ["Alastair Reynolds"],
        "isbn_13": ["9780575074347"],
        "publishers": ["Gollancz"],
        "_queried_isbn": "9780575074347",
    }
    signals = _signals(**ABSOLUTION_GAP, isbns=["9780575074347"])
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [_StubProvider("works", SEARCH_DOC), _StubProvider("editions", edition_record)],
    ):
        candidates = resolve_candidates(signals)
    assert [candidate.score for candidate in candidates] == [1.0, 1.0]
    decision = intake.decide_auto_accept(candidates, [], file_evidence=signals)
    assert decision.candidate is not None
    assert decision.candidate.provider_id == "editions"


def test_browser_fixture_digital_scenario_is_held_for_edition_review() -> None:
    """Mirrors tests/browser/digital-library.spec.ts with the pandoc-built EPUB."""
    signals = _signals(
        title_hints=["Browser Digital Library"],
        author_hints=["Ada Digital"],
        language=normalize_language("en-US"),
        publisher="ada digital self-publishing",
    )
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [BrowserMetadataProvider()],
    ):
        candidates = resolve_candidates(signals)
    assert candidates[0].score >= AUTO_ACCEPT_SCORE
    decision = intake.decide_auto_accept(candidates, ["Ada Digital"], file_evidence=signals)
    assert decision.blocked_reason == intake.BLOCK_EDITION_CONFLICT
    assert decision.details["edition_conflicts"] == [EDITION_PUBLISHER_MISMATCH]


def _write_isbn_epub(path: Path, *, isbn: str) -> None:
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
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:opf="http://www.idpf.org/2007/opf">'
            "<dc:title>Absolution Gap</dc:title><dc:creator>Alastair Reynolds</dc:creator>"
            f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>'
            "</metadata></package>",
        )


def _upload_isbn_epub(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, client: Client, *, isbn: str
) -> ImportJob:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(tmp_path / "storage")),
    )
    monkeypatch.setattr(
        "ebook_server.core.identification.urlopen",
        _fake_urlopen({"search.json": {"docs": [SEARCH_DOC]}}, []),
    )
    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [OpenLibraryMetadataProvider()],
    )
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.86)
    client.force_login(get_or_create_profile("edition-user").user)
    epub = tmp_path / "absolution.epub"
    _write_isbn_epub(epub, isbn=isbn)
    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "Absolution Gap.epub", epub.read_bytes(), content_type="application/epub+zip"
            )
        },
    )
    assert response.status_code == 201
    return ImportJob.objects.get(pk=response.json()["job_id"])


@pytest.mark.django_db(transaction=True)
def test_intake_routes_other_edition_isbn_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = Client()
    job = _upload_isbn_epub(monkeypatch, tmp_path, client, isbn="9780575074347")
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert job.evidence[intake.AUTO_ACCEPT_BLOCKED_KEY] == intake.BLOCK_EDITION_CONFLICT
    details = job.evidence[intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY]
    assert details["edition_conflicts"] == [EDITION_ISBN_MISMATCH]
    assert set(job.metadata_candidates.values_list("state", flat=True)) == {
        MetadataCandidate.CandidateState.REVIEW
    }
    assert not Book.objects.exists()
    assert not Identifier.objects.filter(normalized_value="9780441011582").exists()
    task = ReviewTask.objects.get(import_job=job)
    html = client.get(f"/reviews/{task.id}/?view=html").content.decode()
    assert 'data-block-reason="edition_conflict"' in html
    assert "describes another edition than the file" in html
    assert "edition_isbn_mismatch" in html


@pytest.mark.django_db(transaction=True)
def test_intake_accepts_the_files_own_edition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _upload_isbn_epub(monkeypatch, tmp_path, Client(), isbn="9780441011582")
    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert intake.AUTO_ACCEPT_BLOCKED_KEY not in job.evidence
    assert "9780441011582" in set(job.book.identifiers.values_list("normalized_value", flat=True))
