"""Typed author/title evidence: alternatives vs coauthors, and title provenance (#15, #9).

The end-to-end tests go through the real upload view, format extractor, provider
boundary (a catalogue stub), auto-accept decision and Book creation. They reproduce the
reviewer probes on PR #96 and PR #81:

* "Python Development, Second Edit - Erik Westra.pdf" and "Windows Programming, Third
  Edit - Charles Petzold.pdf" must not be read as "Last, First" author names.
* Good Omens embeds two genuine coauthors; a provider naming only one of them must not
  get full author credit.
* An EPUB embedding "The Dragon in the Sea" uploaded as "Dune - Frank Herbert.epub" must
  not be imported as Dune.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings
from pypdf import PdfWriter

from ebook_server.core import intake
from ebook_server.core.identification import (
    AUTHORS_ALTERNATIVES,
    AUTHORS_COAUTHORS,
    EVIDENCE_EMBEDDED,
    EVIDENCE_FILENAME,
    EVIDENCE_LAYOUT,
    PATH_LAYOUT_EVIDENCE_KEY,
    AuthorEvidence,
    IdentificationCandidate,
    IdentificationInput,
    author_evidence_score,
    build_identification_input,
    invert_last_first_name,
    is_edition_phrase,
    missing_coauthors,
    parse_filename_signals,
    same_book,
    strongest_title_evidence,
)
from ebook_server.core.models import (
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

# --- unit: "Last, First" vs edition phrases ---------------------------------------------


@pytest.mark.parametrize(
    "value",
    [
        "Python Development, Second Edit",
        "Windows Programming, Third Edit",
        "Windows Programming, Fifth Edition",
        "Clean Code, 2nd Ed.",
        "Faust, Zweite Auflage",
        "Foundation, Volume Two",
        "Dune Messiah, Part One",
    ],
)
def test_edition_comma_phrases_are_not_last_first_names(value: str) -> None:
    assert is_edition_phrase(value)
    assert invert_last_first_name(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Corey, James S. A.", "James S. A. Corey"),
        ("McBain, Ed", "Ed McBain"),  # "Ed" alone is a given name, not "edition"
        ("Le Guin, Ursula K.", "Ursula K. Le Guin"),
    ],
)
def test_real_last_first_names_still_invert(value: str, expected: str) -> None:
    assert not is_edition_phrase(value)
    assert invert_last_first_name(value) == expected


@pytest.mark.parametrize(
    ("filename", "title", "author"),
    [
        (
            "Python Development, Second Edit - Erik Westra.pdf",
            "Python Development, Second Edit",
            "Erik Westra",
        ),
        (
            "Windows Programming, Third Edit - Charles Petzold.pdf",
            "Windows Programming, Third Edit",
            "Charles Petzold",
        ),
    ],
)
def test_edition_filenames_keep_title_and_author_orientation(
    filename: str, title: str, author: str
) -> None:
    title_hints, author_hints = parse_filename_signals(filename)

    assert title_hints[0] == title
    assert author_hints == [author]


# --- unit: typed author groups ------------------------------------------------------------


def _signals(*groups: AuthorEvidence) -> IdentificationInput:
    return IdentificationInput(
        title_hints=["Good Omens"],
        author_hints=[name for group in groups for name in group.names],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
        author_evidence=groups,
    )


def _candidate(title: str, authors: list[str]) -> IdentificationCandidate:
    return IdentificationCandidate(
        provider_id="stub",
        provider_ref=title,
        title=title,
        authors=authors,
        isbn10=None,
        isbn13=None,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=0.0,
        raw={},
    )


PRATCHETT_GAIMAN = AuthorEvidence(
    AUTHORS_COAUTHORS, EVIDENCE_EMBEDDED, ("Terry Pratchett", "Neil Gaiman")
)
FILENAME_GUESSES = AuthorEvidence(
    AUTHORS_ALTERNATIVES, EVIDENCE_FILENAME, ("Good Omens", "Terry Pratchett")
)


def test_coauthor_group_is_scored_by_coverage() -> None:
    signals = _signals(PRATCHETT_GAIMAN)

    assert author_evidence_score(signals, ["Terry Pratchett", "Neil Gaiman"]) == 1.0
    assert author_evidence_score(signals, ["Terry Pratchett"]) == pytest.approx(0.5)
    assert missing_coauthors(signals, ["Terry Pratchett"]) == ["Neil Gaiman"]
    assert missing_coauthors(signals, ["Neil Gaiman", "Terry Pratchett"]) == []


def test_filename_guess_cannot_restore_credit_for_a_missing_coauthor() -> None:
    signals = _signals(PRATCHETT_GAIMAN, FILENAME_GUESSES)

    assert author_evidence_score(signals, ["Terry Pratchett"]) == pytest.approx(0.5)


def test_alternatives_take_the_best_single_match() -> None:
    signals = _signals(FILENAME_GUESSES)

    assert author_evidence_score(signals, ["Terry Pratchett"]) == 1.0
    assert missing_coauthors(signals, ["Terry Pratchett"]) == []


def test_unmatched_coauthor_group_is_left_to_the_conflict_guard() -> None:
    # Embedded "Brian Herbert" vs candidate "Frank Herbert": an author conflict, which
    # the #92 guard blocks at accept time; it is not reported as a missing coauthor.
    embedded = AuthorEvidence(AUTHORS_COAUTHORS, EVIDENCE_EMBEDDED, ("Brian Herbert",))
    guesses = AuthorEvidence(AUTHORS_ALTERNATIVES, EVIDENCE_FILENAME, ("Dune", "Frank Herbert"))
    signals = _signals(embedded, guesses)

    assert author_evidence_score(signals, ["Frank Herbert"]) == 1.0
    assert missing_coauthors(signals, ["Frank Herbert"]) == []


def test_hand_built_inputs_score_hints_as_alternatives() -> None:
    signals = IdentificationInput(
        title_hints=["Pet Sematary"],
        author_hints=["Pet Sematary", "Stephen King"],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )

    assert author_evidence_score(signals, ["Stephen King"]) == 1.0


def test_same_book_requires_every_author_of_the_first_book() -> None:
    both = _candidate("Good Omens", ["Terry Pratchett", "Neil Gaiman"])
    one = _candidate("Good Omens", ["Terry Pratchett"])

    assert same_book(both, both)
    assert not same_book(both, one)


def test_build_input_types_embedded_layout_and_filename_evidence() -> None:
    signals = build_identification_input(
        source_path="/incoming/Good Omens - Terry Pratchett.epub",
        original_filename="Good Omens - Terry Pratchett.epub",
        extracted_metadata={
            "title": "Good Omens",
            "authors": ["Terry Pratchett & Neil Gaiman", "Unknown"],
        },
    )

    assert signals.author_evidence[0] == PRATCHETT_GAIMAN
    assert signals.author_evidence[1].kind == AUTHORS_ALTERNATIVES
    assert signals.author_evidence[1].source == EVIDENCE_FILENAME
    assert [t.source for t in strongest_title_evidence(signals)] == [EVIDENCE_EMBEDDED]


def test_layout_authors_replace_filename_guesses_as_coauthors() -> None:
    signals = build_identification_input(
        source_path="/lib/Pratchett/Mort.epub",
        original_filename="Mort - Terry Pratchett.epub",
        extracted_metadata={
            "title": "Mort - Terry Pratchett",
            PATH_LAYOUT_EVIDENCE_KEY: {"title": "Mort", "authors": ["Terry Pratchett"]},
        },
    )

    assert signals.author_evidence == (
        AuthorEvidence(AUTHORS_COAUTHORS, EVIDENCE_LAYOUT, ("Terry Pratchett",)),
    )
    strongest = strongest_title_evidence(signals)
    assert [(t.title, t.source) for t in strongest] == [("Mort", EVIDENCE_LAYOUT)]


def test_embedded_title_outranks_contradicting_filename_title() -> None:
    signals = build_identification_input(
        source_path="/incoming/Dune - Frank Herbert.epub",
        original_filename="Dune - Frank Herbert.epub",
        extracted_metadata={"title": "The Dragon in the Sea", "authors": ["Frank Herbert"]},
    )

    sources = {t.title: t.source for t in signals.title_evidence}
    assert sources["The Dragon in the Sea"] == EVIDENCE_EMBEDDED
    assert sources["Dune"] == EVIDENCE_FILENAME
    assert [t.title for t in strongest_title_evidence(signals)] == ["The Dragon in the Sea"]


# --- end to end: upload -> extractor -> provider -> decision -> Book ----------------------


def _catalogue_provider(
    monkeypatch: pytest.MonkeyPatch,
    books: list[tuple[str, list[str]]],
    *,
    any_title: bool = False,
) -> list[str | None]:
    """A provider that answers like a catalogue: only books whose title was queried.

    ``any_title`` returns every book for any query, like a provider whose fuzzy search
    hands back a popular book for a non-matching query. Returns the queried titles.
    """
    queries: list[str | None] = []

    class CatalogueProvider:
        provider_id = "catalogue"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, Any]]:
            del author, isbn, folder_hint
            queries.append(title)
            wanted = " ".join((title or "").lower().split())
            return [
                {"title": book_title, "author_name": authors, "key": f"/works/{index}"}
                for index, (book_title, authors) in enumerate(books)
                if any_title or " ".join(book_title.lower().split()) == wanted
            ]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [CatalogueProvider()],
    )
    return queries


def _epub_bytes(title: str, authors: list[str], *, extra_metadata: str = "") -> bytes:
    stream = io.BytesIO()
    creators = "".join(f"<dc:creator>{escape(author)}</dc:creator>" for author in authors)
    creators += extra_metadata
    with ZipFile(stream, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            f"<dc:title>{escape(title)}</dc:title>{creators}"
            "</metadata></package>",
        )
    return stream.getvalue()


def test_epub_extractor_keeps_every_author_creator_but_not_other_roles(tmp_path: Path) -> None:
    path = tmp_path / "omens.epub"
    translator = (
        '<dc:creator xmlns:opf="http://www.idpf.org/2007/opf" opf:role="trl">'
        "Some Translator</dc:creator>"
    )
    path.write_bytes(
        _epub_bytes("Good Omens", ["Terry Pratchett", "Neil Gaiman"], extra_metadata=translator)
    )

    metadata = intake.extract_metadata(str(path), "epub", original_filename=path.name)

    assert metadata["authors"] == ["Terry Pratchett", "Neil Gaiman"]


def _blank_pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


def _upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    payload: bytes,
    content_type: str,
) -> ImportJob:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage = LocalFilesystemStorage(str(tmp_path / "storage" / "library-artifacts"))
    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: storage)
    profile = get_or_create_profile("evidence-uploader")
    client = Client()
    client.force_login(profile.user)
    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        response = client.post(
            "/imports/upload/",
            {"file": SimpleUploadedFile(filename, payload, content_type=content_type)},
        )
    assert response.status_code == 201
    return ImportJob.objects.get(pk=response.json()["job_id"])


def _book_authors(job: ImportJob) -> list[str]:
    assert job.book is not None
    return [author.full_name for author in job.book.authors.all()]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("filename", "title", "author"),
    [
        (
            "Python Development, Second Edit - Erik Westra.pdf",
            "Python Development, Second Edit",
            "Erik Westra",
        ),
        (
            "Windows Programming, Third Edit - Charles Petzold.pdf",
            "Windows Programming, Third Edit",
            "Charles Petzold",
        ),
    ],
)
def test_edition_filename_upload_creates_book_with_correct_title_and_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    filename: str,
    title: str,
    author: str,
) -> None:
    queries = _catalogue_provider(monkeypatch, [(title, [author])])
    # Title + author alone score 0.85; accept at that score so a Book is created.
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.85)

    job = _upload(tmp_path, monkeypatch, filename, _blank_pdf_bytes(), "application/pdf")

    assert queries[0] == title
    assert author not in queries
    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert job.book.title == title
    assert _book_authors(job) == [author]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "creators",
    [["Terry Pratchett", "Neil Gaiman"], ["Terry Pratchett & Neil Gaiman"]],
    ids=["two-creators", "joint-creator"],
)
def test_good_omens_provider_missing_a_coauthor_is_not_auto_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    creators: list[str],
) -> None:
    _catalogue_provider(monkeypatch, [("Good Omens", ["Terry Pratchett"])])

    job = _upload(
        tmp_path,
        monkeypatch,
        "Good Omens - Terry Pratchett.epub",
        _epub_bytes("Good Omens", creators),
        "application/epub+zip",
    )

    candidate = MetadataCandidate.objects.get(import_job=job)
    # Title 0.55 + half of the author weight: only one of two coauthors is covered.
    assert float(candidate.score) == pytest.approx(0.70)
    assert candidate.state == MetadataCandidate.CandidateState.REVIEW
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert ReviewTask.objects.get(import_job=job).state == ReviewTask.State.OPEN


@pytest.mark.django_db(transaction=True)
def test_good_omens_provider_naming_both_coauthors_gets_full_author_credit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _catalogue_provider(monkeypatch, [("Good Omens", ["Neil Gaiman", "Terry Pratchett"])])
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.85)

    job = _upload(
        tmp_path,
        monkeypatch,
        "Good Omens - Terry Pratchett.epub",
        _epub_bytes("Good Omens", ["Terry Pratchett", "Neil Gaiman"]),
        "application/epub+zip",
    )

    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert job.book.title == "Good Omens"
    assert sorted(_book_authors(job)) == ["Neil Gaiman", "Terry Pratchett"]


@pytest.mark.django_db(transaction=True)
def test_embedded_title_contradicting_filename_title_is_not_imported_as_filename_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The provider hands back Dune whatever is asked; only the filename says "Dune".
    _catalogue_provider(monkeypatch, [("Dune", ["Frank Herbert"])], any_title=True)

    job = _upload(
        tmp_path,
        monkeypatch,
        "Dune - Frank Herbert.epub",
        _epub_bytes("The Dragon in the Sea", ["Frank Herbert"]),
        "application/epub+zip",
    )

    candidate = MetadataCandidate.objects.get(import_job=job)
    assert candidate.title == "Dune"
    assert candidate.state == MetadataCandidate.CandidateState.REVIEW
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert ReviewTask.objects.get(import_job=job).inferred_title == "The Dragon in the Sea"


@pytest.mark.django_db
def test_explicit_text_header_keeps_joint_author_evidence(tmp_path: Path) -> None:
    path = tmp_path / "Good Omens - Terry Pratchett.txt"
    path.write_text(
        "Title: Good Omens\nAuthors: Terry Pratchett & Neil Gaiman\n\nBook text.",
        encoding="utf-8",
    )
    metadata = intake.extract_metadata(str(path), "txt", original_filename=path.name)
    signals = build_identification_input(
        source_path=str(path), original_filename=path.name, extracted_metadata=metadata
    )

    assert metadata[intake.TITLE_SOURCE_KEY] == intake.TITLE_SOURCE_FILENAME
    header_groups = [group for group in signals.author_groups() if group.source == "header"]
    assert len(header_groups) == 1
    assert header_groups[0].kind == AUTHORS_COAUTHORS
    assert header_groups[0].names == ("Terry Pratchett", "Neil Gaiman")
    assert author_evidence_score(signals, ["Terry Pratchett"]) == pytest.approx(0.5)
    assert author_evidence_score(signals, ["Terry Pratchett", "Neil Gaiman"]) == 1.0
    assert {title.source for title in strongest_title_evidence(signals)} == {"header"}


@pytest.mark.parametrize(
    ("source", "expected_title", "expected_source"),
    [
        ("embedded", "Midworld", "embedded"),
        ("sidecar", "Midworld", "sidecar"),
        ("filename", "Phylogenesis", "layout"),
    ],
)
def test_explicit_title_provenance_beats_stem_equality(
    source: str, expected_title: str, expected_source: str
) -> None:
    signals = build_identification_input(
        source_path="/incoming/Midworld.epub",
        original_filename="Midworld.epub",
        extracted_metadata={
            "title": "Midworld",
            intake.TITLE_SOURCE_KEY: source,
            PATH_LAYOUT_EVIDENCE_KEY: {"title": "Phylogenesis"},
        },
    )

    assert signals.best_title() == expected_title
    assert {title.source for title in strongest_title_evidence(signals)} == {expected_source}


def test_layout_author_keeps_priority_over_weak_header_hints() -> None:
    signals = build_identification_input(
        source_path="/incoming/Midworld.txt",
        original_filename="Midworld.txt",
        extracted_metadata={
            "title": "Midworld",
            intake.TITLE_SOURCE_KEY: "filename",
            "text_header": {
                "title": "Midworld",
                "authors": [],
                "likely_authors": ["Someone Else"],
                "rules": [],
            },
            PATH_LAYOUT_EVIDENCE_KEY: {"authors": ["Alan Dean Foster"]},
        },
    )

    assert signals.best_author() == "Alan Dean Foster"
    assert author_evidence_score(signals, ["Someone Else"]) == 0.0
    assert author_evidence_score(signals, ["Alan Dean Foster"]) == 1.0
