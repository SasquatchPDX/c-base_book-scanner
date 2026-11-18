"""Local consensus: a pre-filled review suggestion from agreeing independent evidence.

It never accepts a book (AGENTS.md: external verification is mandatory for intake).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.test import Client, override_settings

from ebook_server.core import identification, intake, local_identity
from ebook_server.core.identification import (
    TEXT_HEADER_EVIDENCE_KEY,
    IdentificationCandidate,
    ProviderOutcome,
    build_identification_input,
)
from ebook_server.core.models import (
    Book,
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    SourceLocation,
    get_or_create_profile,
)
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
    filename: str,
    candidates: list[IdentificationCandidate] | None = None,
    *,
    outcomes: list[ProviderOutcome] | None = None,
    **kwargs: Any,
) -> local_identity.LocalIdentity | None:
    metadata = _metadata(filename, **kwargs)
    signals = build_identification_input(
        source_path=f"/library/{filename}", original_filename=filename, extracted_metadata=metadata
    )
    return local_identity.local_consensus(
        metadata,
        signals,
        candidates or [],
        filename=filename,
        provider_outcomes=outcomes or [],
    )


def _candidate(title: str, authors: list[str], score: float = 0.6) -> IdentificationCandidate:
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
        score=score,
        raw={},
    )


OUTAGE = [
    ProviderOutcome("openlibrary", "error", 0, 5.0, "timeout"),
    ProviderOutcome("google_books", "error", 0, 5.0, "HTTP 503"),
]


def test_short_story_from_folder_name_and_labelled_header() -> None:
    # Gibson's story has no record of its own; folder + file name + a labelled header.
    local = _consensus(
        "Fragments of a Hologram Rose.txt",
        header_text="Title: Fragments of a Hologram Rose\nAuthor: William Gibson\n\nThat...\n",
        layout_authors=["William Gibson"],
    )
    assert local is not None
    assert local.title == "Fragments of a Hologram Rose"
    assert local.authors == ["William Gibson"]
    assert local.title_origins == ("content", "path")
    assert local.confirms is None
    assert local.provider_outage == ()
    assert not hasattr(local, "as_candidate")  # a suggestion, never a candidate


@pytest.mark.parametrize(
    "header_text",
    [
        "Fragments of a Hologram Rose\nby William Gibson\n\nThat summer...\n",
        "Fragments of a Hologram Rose by William Gibson\n\nThat summer...\n",
        "FRAGMENTS OF A HOLOGRAM ROSE\n\nThat summer...\n",
    ],
)
def test_a_header_line_that_echoes_the_file_name_is_no_second_witness(header_text: str) -> None:
    # text_header takes these lines as the title only because they repeat the file name:
    # file name and header are one witness (review of #153).
    assert (
        _consensus(
            "Fragments of a Hologram Rose.txt",
            header_text=header_text,
            layout_authors=["William Gibson"],
        )
        is None
    )


def test_gutenberg_header_is_an_independent_title() -> None:
    local = _consensus(
        "pg1342.txt",
        header_text=(
            "The Project Gutenberg EBook of Pride and Prejudice, by Jane Austen\n\n"
            "Title: Pride and Prejudice\n"
        ),
        layout_title="Pride and Prejudice",
    )
    assert local is not None
    assert local.title_origins == ("content", "path")


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
            header_text="Title: Harry Potter and the Sorcerer's Stone\nTHE BOY WHO LIVED\n",
            layout_authors=[folder],
        )
        is None
    )


@pytest.mark.parametrize(
    "folder",
    ["Fantasy", "Science Fiction", "Short Stories", "Kurzgeschichten", "Märchen", "Sci-Fi"],
)
def test_genre_folder_is_no_author(folder: str) -> None:
    # An "{authors}/**" library may still hold a genre folder (review of #153).
    assert not local_identity.plausible_person(folder)
    assert (
        _consensus(
            "The Tin Soldier.txt",
            header_text="Title: The Tin Soldier\n\nThere were once...\n",
            layout_authors=[folder],
        )
        is None
    )


@pytest.mark.parametrize("name", ["Neil Young", "Luke Short", "Jack London", "Terry Pratchett"])
def test_real_names_stay_plausible(name: str) -> None:
    assert local_identity.plausible_person(name)


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
        header_text="Title: Frustration\n\nHerman Gelb...\n",
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


# --- Provider records: confirm, never override ------------------------------------


def test_a_provider_record_for_the_same_book_is_confirmed_not_replaced() -> None:
    heinlein = _candidate("Starship Troopers", ["Robert A. Heinlein"])
    local = _consensus(
        "Starship Troopers.txt",
        [heinlein],
        header_text="Title: Starship Troopers\nAuthor: Robert A. Heinlein\n\nThe boys...\n",
        layout_authors=["Robert A Heinlein"],
    )
    assert local is not None
    assert local.confirms == {
        "provider_id": "openlibrary",
        "provider_ref": "/works/OL1W",
        "title": "Starship Troopers",
    }
    assert heinlein.score == 0.6  # its score is untouched


def test_tin_soldier_in_a_grimm_folder_does_not_override_andersen() -> None:
    # The reviewer's probe: a provider knows the title by another author.
    andersen = _candidate("The Tin Soldier", ["Hans Christian Andersen"])
    assert (
        _consensus(
            "The Tin Soldier.txt",
            [andersen],
            header_text="The Tin Soldier\n\nThere were once...\n",
            layout_authors=["Grimm, Jacob"],
        )
        is None
    )
    # Even with an independent (labelled) title, the provider's author contradicts.
    assert (
        _consensus(
            "The Tin Soldier.txt",
            [andersen],
            header_text="Title: The Tin Soldier\n\nThere were once...\n",
            layout_authors=["Grimm, Jacob"],
        )
        is None
    )


@pytest.mark.parametrize(
    "header_text",
    [
        "PRIDE AND PREJUDICE\nElizabeth Bennet\n\nIt is a truth...\n",
        "Title: Pride and Prejudice\nElizabeth Bennet\n\nIt is a truth...\n",
    ],
)
def test_pride_and_prejudice_header_character_is_not_the_author(header_text: str) -> None:
    # The reviewer's probe: a name-like line beside the title is a character, and the
    # provider names Jane Austen.
    austen = _candidate("Pride and Prejudice", ["Jane Austen"])
    assert (
        _consensus(
            "Pride and Prejudice.txt",
            [austen],
            header_text=header_text,
            layout_title="Pride and Prejudice",
        )
        is None
    )


def test_provider_outage_is_noted_in_the_suggestion() -> None:
    local = _consensus(
        "Fragments of a Hologram Rose.txt",
        outcomes=OUTAGE,
        header_text="Title: Fragments of a Hologram Rose\nAuthor: William Gibson\n",
        layout_authors=["William Gibson"],
    )
    assert local is not None
    assert local.provider_outage == ("openlibrary", "google_books")
    assert local.evidence["provider_outage"] == ["openlibrary", "google_books"]


@override_settings(BOOK_SCANNER_IDENTIFICATION_LOCAL_CONSENSUS=False)
def test_can_be_switched_off() -> None:
    assert (
        _consensus(
            "Fragments of a Hologram Rose.txt",
            header_text="Title: Fragments of a Hologram Rose\nAuthor: William Gibson\n",
            layout_authors=["William Gibson"],
        )
        is None
    )


# --- End to end through source intake: always review ------------------------------


@pytest.fixture
def app_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "attached"
    root.mkdir()
    monkeypatch.setattr(django_settings, "APP_DATA_DIR", root, raising=False)
    return root


def _import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    folder: str,
    filename: str,
    text: str,
    candidates: list[IdentificationCandidate] | None = None,
    outcomes: list[ProviderOutcome] | None = None,
) -> ImportJob:
    root = tmp_path / "eBooks"
    author_dir = root / folder
    author_dir.mkdir(parents=True)
    path = author_dir / filename
    path.write_text(text, encoding="utf-8")
    monkeypatch.setattr(
        identification,
        "resolve_candidates_with_report",
        lambda _s: (list(candidates or []), list(outcomes or [])),
    )
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
    return job


def _review_html(job: ImportJob) -> str:
    client = Client()
    client.force_login(get_or_create_profile("local-consensus-admin", role="admin").user)
    task = ReviewTask.objects.get(import_job=job)
    return client.get(f"/reviews/{task.id}/?view=html").content.decode()


@pytest.mark.django_db
def test_intake_suggests_a_story_no_provider_knows_but_does_not_accept(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del app_storage
    job = _import(
        tmp_path,
        monkeypatch,
        folder="Pratchett, Terry",
        filename="Turntables of the Night.txt",
        text="Title: Turntables of the Night\nAuthor: Terry Pratchett\n\n" + "There...\n" * 40,
    )

    # No provider record: external verification failed, so a person decides.
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert not Book.objects.exists()
    assert isinstance(job.evidence, dict)
    suggestion = job.evidence[local_identity.LOCAL_IDENTITY_KEY]
    assert suggestion["rule"] == "local_consensus"
    assert (suggestion["title"], suggestion["authors"]) == (
        "Turntables of the Night",
        ["Terry Pratchett"],
    )
    html = _review_html(job)
    assert 'data-local-suggestion="manual"' in html
    assert 'value="Turntables of the Night"' in html
    assert 'value="Terry Pratchett"' in html


@pytest.mark.django_db
def test_intake_meeting_notes_during_provider_outage_goes_to_review(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The reviewer's probe: every provider down, the first line repeats the file name.
    del app_storage
    job = _import(
        tmp_path,
        monkeypatch,
        folder="Smith, John",
        filename="Meeting Notes.txt",
        text="Meeting Notes\n\nAgenda for Tuesday...\n" * 3,
        outcomes=OUTAGE,
    )

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert not Book.objects.exists()
    assert isinstance(job.evidence, dict)
    # The header only echoes the file name: no second witness, so no suggestion either.
    assert local_identity.LOCAL_IDENTITY_KEY not in job.evidence


@pytest.mark.django_db
def test_intake_outage_with_agreeing_evidence_still_goes_to_review_with_outage_noted(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del app_storage
    job = _import(
        tmp_path,
        monkeypatch,
        folder="Smith, John",
        filename="Meeting Notes.txt",
        text="Title: Meeting Notes\nAuthor: John Smith\n\nAgenda for Tuesday...\n",
        outcomes=OUTAGE,
    )

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert not Book.objects.exists()
    assert isinstance(job.evidence, dict)
    suggestion = job.evidence[local_identity.LOCAL_IDENTITY_KEY]
    assert suggestion["provider_outage"] == ["openlibrary", "google_books"]
    html = _review_html(job)
    assert "data-provider-outage" in html
    assert "openlibrary, google_books failed" in html


@pytest.mark.django_db
def test_intake_confirms_a_weak_provider_record_without_raising_its_score(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del app_storage
    weak = _candidate("Turntables of the Night", ["Terry Pratchett"], score=0.6)
    job = _import(
        tmp_path,
        monkeypatch,
        folder="Pratchett, Terry",
        filename="Turntables of the Night.txt",
        text="Title: Turntables of the Night\nAuthor: Terry Pratchett\n\nThere...\n",
        candidates=[weak],
    )

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert not Book.objects.exists()
    assert isinstance(job.evidence, dict)
    suggestion = job.evidence[local_identity.LOCAL_IDENTITY_KEY]
    assert suggestion["confirms"]["provider_ref"] == "/works/OL1W"
    stored = MetadataCandidate.objects.get(import_job=job)
    assert float(stored.score) == pytest.approx(0.6)
    html = _review_html(job)
    assert 'data-local-suggestion="confirms-provider"' in html
    assert f'name="candidate_id" value="{stored.pk}"' in html


@pytest.mark.django_db
def test_intake_conflicting_provider_author_gives_no_suggestion(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    del app_storage
    andersen = _candidate("The Tin Soldier", ["Hans Christian Andersen"])
    job = _import(
        tmp_path,
        monkeypatch,
        folder="Grimm, Jacob",
        filename="The Tin Soldier.txt",
        text="Title: The Tin Soldier\n\nThere were once...\n",
        candidates=[andersen],
    )

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert not Book.objects.exists()
    assert isinstance(job.evidence, dict)
    assert local_identity.LOCAL_IDENTITY_KEY not in job.evidence
