"""Identify a book from agreeing independent local evidence (local consensus)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.test import override_settings

from ebook_server.core import identification, intake, local_identity
from ebook_server.core.identification import (
    TEXT_HEADER_EVIDENCE_KEY,
    IdentificationCandidate,
    build_identification_input,
)
from ebook_server.core.models import Book, ImportJob, SourceLocation
from ebook_server.core.text_header import parse_text_header


def _metadata(
    filename: str,
    *,
    header_text: str | None = None,
    layout_authors: list[str] | None = None,
    layout_title: str | None = None,
    embedded: dict[str, Any] | None = None,
    sidecar: dict[str, Any] | None = None,
) -> dict[str, Any]:
    stem = Path(filename).stem
    metadata: dict[str, Any] = {"title": stem, "title_source": "filename"}
    if embedded:
        metadata.update(embedded)
        metadata["title_source"] = "embedded"
    if header_text is not None:
        header = parse_text_header(header_text, stem)
        if header is not None:
            metadata[TEXT_HEADER_EVIDENCE_KEY] = header
    if layout_authors is not None or layout_title is not None:
        metadata["path_layout"] = {"authors": layout_authors or [], "title": layout_title}
    if sidecar:
        metadata["sidecar_metadata"] = {"fields": sidecar, "source": "metadata.opf"}
    return metadata


def _consensus(
    filename: str, candidates: list[IdentificationCandidate] | None = None, **kwargs: Any
) -> local_identity.LocalIdentity | None:
    metadata = _metadata(filename, **kwargs)
    signals = build_identification_input(
        source_path=f"/library/{filename}", original_filename=filename, extracted_metadata=metadata
    )
    return local_identity.local_consensus(metadata, signals, candidates or [], filename=filename)


def _candidate(title: str, authors: list[str]) -> IdentificationCandidate:
    return IdentificationCandidate(
        provider_id="openlibrary",
        provider_ref="/works/OL1W",
        title=title,
        authors=authors,
        isbn10=None,
        isbn13=None,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=0.6,
        raw={},
    )


def test_short_story_from_folder_name_and_header() -> None:
    # Gibson's story has no record of its own; folder + file name + first lines agree.
    local = _consensus(
        "Fragments of a Hologram Rose.txt",
        header_text="Fragments of a Hologram Rose\nby William Gibson\n\nThat summer...\n",
        layout_authors=["William Gibson"],
    )
    assert local is not None
    assert local.title == "Fragments of a Hologram Rose"
    assert local.authors == ["William Gibson"]
    assert local.title_origins == ("content", "path")
    candidate = local.as_candidate()
    assert candidate.provider_id == "local"
    assert candidate.isbn13 is None


def test_header_spelling_of_the_author_wins_over_a_folder_typo() -> None:
    local = _consensus(
        "Madness From Mars.txt",
        header_text="Title: Madness from Mars\nAuthor: Clifford D. Simak\n\nIt was...\n",
        layout_authors=["Cliffard D Simak"],
    )
    assert local is not None
    assert local.authors == ["Clifford D. Simak"]


def test_epub_embedded_title_confirms_the_file_name() -> None:
    local = _consensus(
        "Broken Mirror - T. A. Pratt.epub",
        embedded={"title": "Broken Mirror", "authors": ["T. A. Pratt"]},
        layout_authors=["T. A. Pratt"],
        layout_title="Broken Mirror",
    )
    assert local is not None
    assert (local.title, local.authors) == ("Broken Mirror", ["T. A. Pratt"])
    assert local.title_origins == ("embedded", "path")


def test_file_name_folder_and_calibre_sidecar_are_one_witness() -> None:
    # Calibre names the folder and writes metadata.opf from one record.
    assert (
        _consensus(
            "Overclocked -I Robot - Cory Doctorow.txt",
            layout_authors=["Cory Doctorow"],
            layout_title="Overclocked -I Robot",
            sidecar={"title": "Overclocked -I Robot", "authors": ["Cory Doctorow"]},
        )
        is None
    )


def test_title_from_one_origin_only_is_not_enough() -> None:
    assert _consensus("Little Pear Girl.txt", header_text="Once upon a time...\n") is None


@pytest.mark.parametrize("folder", ["CHAPTER ONE", "Appendices", "Love"])
def test_folder_that_names_no_person_is_no_author(folder: str) -> None:
    assert (
        _consensus(
            "Harry Potter and the Sorcerer's Stone.txt",
            header_text="Harry Potter and the Sorcerer's Stone\nTHE BOY WHO LIVED\n",
            layout_authors=[folder],
        )
        is None
    )


def test_swapped_title_and_author_folders_are_refused() -> None:
    assert (
        _consensus(
            "Kohl, Walter - Leben oder gelebt werden.epub",
            embedded={"title": "Kohl, Walter", "authors": ["Leben oder gelebt werden"]},
            layout_authors=["Leben oder gelebt werden"],
            layout_title="Kohl, Walter",
        )
        is None
    )


def test_file_name_author_contradicting_the_embedded_author_is_no_consensus() -> None:
    assert (
        _consensus(
            "John Roe - Borderline Match.epub",
            embedded={"title": "Borderline Match", "authors": ["Jane Doe"]},
        )
        is None
    )


def test_file_name_author_agreeing_with_the_embedded_author_is_fine() -> None:
    local = _consensus(
        "Absolution Gap - Alastair Reynolds.epub",
        embedded={"title": "Absolution Gap", "authors": ["Alastair Reynolds"]},
    )
    assert local is not None


def test_single_word_file_name_parts_are_no_author() -> None:
    local = _consensus(
        "Gold - 03 - Frustration.txt",
        header_text="Frustration\n\nHerman Gelb...\n",
        layout_authors=["Isaac Asimov"],
    )
    assert local is not None
    assert (local.title, local.authors) == ("Frustration", ["Isaac Asimov"])


def test_disagreeing_authors_are_no_consensus() -> None:
    assert (
        _consensus(
            "Intruder - Robert Thurston.epub",
            embedded={"title": "Intruder", "authors": ["Isaac Asimov"]},
            layout_authors=["Robert Thurston"],
            layout_title="Intruder",
        )
        is None
    )


def test_a_provider_candidate_for_the_same_book_keeps_the_decision() -> None:
    assert (
        _consensus(
            "Starship Troopers.txt",
            [_candidate("Starship Troopers", ["Robert A. Heinlein"])],
            header_text="Starship Troopers by Robert A. Heinlein\n\nThe boys...\n",
            layout_authors=["Robert A Heinlein"],
        )
        is None
    )


def test_a_provider_book_with_the_same_title_by_someone_else_does_not_block() -> None:
    local = _consensus(
        "Broken Mirror - T. A. Pratt.epub",
        [_candidate("Broken Mirror", ["Davide Sernicoli"])],
        embedded={"title": "Broken Mirror", "authors": ["T. A. Pratt"]},
        layout_authors=["T. A. Pratt"],
        layout_title="Broken Mirror",
    )
    assert local is not None


@override_settings(BOOK_SCANNER_IDENTIFICATION_LOCAL_CONSENSUS=False)
def test_can_be_switched_off() -> None:
    assert (
        _consensus(
            "Fragments of a Hologram Rose.txt",
            header_text="Fragments of a Hologram Rose\nby William Gibson\n",
            layout_authors=["William Gibson"],
        )
        is None
    )


# --- End to end through source intake ------------------------------------------------


@pytest.fixture
def app_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "attached"
    root.mkdir()
    monkeypatch.setattr(django_settings, "APP_DATA_DIR", root, raising=False)
    return root


@pytest.mark.django_db
def test_intake_identifies_a_story_no_provider_knows(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del app_storage
    root = tmp_path / "eBooks"
    author_dir = root / "Pratchett, Terry"
    author_dir.mkdir(parents=True)
    path = author_dir / "Turntables of the Night.txt"
    path.write_text(
        "Turntables of the Night\nby Terry Pratchett\n\n" + "There was this...\n" * 40,
        encoding="utf-8",
    )
    monkeypatch.setattr(identification, "resolve_candidates_with_report", lambda _s: ([], []))
    source = SourceLocation.objects.create(
        name="ebooks",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
        path_layouts=["{authors}/**"],
    )

    job = intake.create_source_import_job(path, source)
    intake.process_import_job(str(job.id))
    job.refresh_from_db()

    assert job.state == ImportJob.State.COMPLETE
    assert isinstance(job.evidence, dict)
    assert job.evidence[local_identity.LOCAL_IDENTITY_KEY]["rule"] == "local_consensus"
    book = Book.objects.get(pk=job.book_id)
    assert book.title == "Turntables of the Night"
    assert [author.full_name for author in book.authors.all()] == ["Terry Pratchett"]


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_IDENTIFICATION_LOCAL_CONSENSUS=False)
def test_intake_without_local_consensus_goes_to_review(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del app_storage
    root = tmp_path / "eBooks"
    author_dir = root / "Pratchett, Terry"
    author_dir.mkdir(parents=True)
    path = author_dir / "Turntables of the Night.txt"
    path.write_text("Turntables of the Night\nby Terry Pratchett\n\nThere...\n", encoding="utf-8")
    monkeypatch.setattr(identification, "resolve_candidates_with_report", lambda _s: ([], []))
    source = SourceLocation.objects.create(
        name="ebooks",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
        path_layouts=["{authors}/**"],
    )

    job = intake.create_source_import_job(path, source)
    intake.process_import_job(str(job.id))
    job.refresh_from_db()

    assert job.state == ImportJob.State.LOW_CONFIDENCE
