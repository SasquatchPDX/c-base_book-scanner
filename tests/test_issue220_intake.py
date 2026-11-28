"""Named failing cases of the intake evaluation in issue #220, and the G2 lookup costs.

Each test names the finding it covers (F1-F3 placeholders, F4 authorless records, F5
name order, F6 edition vs. work, F7 grouping, F9 header credits, F10 title-only files,
the path addendum, and G2 concurrency/early exit/budget/circuit breaker/reuse).
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest
from django.test import override_settings

from ebook_server.core import identification, intake, provider_runtime, text_header
from ebook_server.core.identification import (
    AUTO_ACCEPT_SCORE,
    EVIDENCE_PATH,
    STRONG_MATCH_EVIDENCE_KEY,
    build_identification_input,
    parse_filename_signals,
    resolve_candidates,
    resolve_candidates_with_report,
)
from ebook_server.core.models import ImportJob, MetadataSearch, SourceLocation, StorageLocation

# --- helpers ------------------------------------------------------------------------------


class RecordingProvider:
    """Returns fixed records and remembers every query it received."""

    def __init__(self, provider_id: str, records: list[dict[str, Any]]) -> None:
        self.provider_id = provider_id
        self.records = records
        self.queries: list[dict[str, Any]] = []

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        self.queries.append(
            {"title": title, "author": author, "isbn": isbn, "folder_hint": folder_hint}
        )
        return [dict(record) for record in self.records]


def _use_providers(monkeypatch: pytest.MonkeyPatch, *providers: Any) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: list(providers))


def _source_location(root: Path) -> SourceLocation:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return SourceLocation.objects.create(
        name=f"issue220-{root.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        recursive=True,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
        is_active=True,
    )


def _import_all(root: Path, tmp_path: Path) -> list[ImportJob]:
    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        jobs = sorted(
            intake.discover_source_jobs_for_location(_source_location(root)),
            key=lambda job: job.source_path,
        )
        for job in jobs:
            intake.process_import_job(str(job.id))
    return [ImportJob.objects.get(pk=job.pk) for job in jobs]


def _opf(title: str, authors: list[str], extra: str = "") -> str:
    creators = "".join(f'<dc:creator opf:role="aut">{name}</dc:creator>' for name in authors)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">'
        f"<dc:title>{title}</dc:title>{creators}{extra}"
        "</metadata></package>"
    )


def _write_epub(path: Path, *, title: str, creator: str, extra: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        archive.writestr(
            "content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:opf="http://www.idpf.org/2007/opf">'
            f"<dc:title>{escape(title)}</dc:title><dc:creator>{escape(creator)}</dc:creator>{extra}"
            "</metadata></package>",
        )


def _all_queries(*providers: RecordingProvider) -> list[dict[str, Any]]:
    return [query for provider in providers for query in provider.queries]


# --- F1-F3: placeholders ------------------------------------------------------------------


def test_trailing_placeholder_in_a_file_name_is_no_author_and_no_title_part() -> None:
    titles, authors = parse_filename_signals("2001 A Space Odissey - Unknown.txt")
    assert titles[0] == "2001 A Space Odissey"
    assert all("unknown" not in title.casefold() for title in titles)
    assert authors == []
    assert identification.strip_placeholder_segments("Dune - Unknown") == "Dune"
    assert identification.strip_placeholder_segments("Unknown - Dune") == "Dune"
    assert identification.strip_placeholder_segments("Unknown") == ""
    assert identification.strip_placeholder_segments("Dune - House Atreides") == (
        "Dune - House Atreides"
    )


def test_sidecar_placeholders_are_dropped_once_where_the_sidecar_is_read() -> None:
    sidecar = intake.normalize_sidecar_metadata(
        {"title": "2001 A Space Odissey - Unknown", "authors": ["Unknown"], "language": "en"}
    )
    assert sidecar is not None
    assert "authors" not in sidecar
    assert sidecar["title"] == "2001 A Space Odissey"
    assert sidecar[intake.SIDECAR_IGNORED_KEY] == [
        {"field": "authors", "value": ["Unknown"], "reason": intake.SIDECAR_IGNORED_PLACEHOLDER}
    ]
    # Idempotent: normalising again changes nothing.
    assert intake.normalize_sidecar_metadata(sidecar) == sidecar
    # One placeholder vocabulary for intake and identification.
    assert intake.PLACEHOLDER_METADATA_VALUES is identification.PLACEHOLDER_METADATA_VALUES


@pytest.mark.django_db(transaction=True)
def test_2001_a_space_odissey_unknown_header_credit_is_used_and_no_placeholder_is_sent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # F1/F2/F3: Calibre's "Unknown" author in folder, file name and sidecar; the text
    # header credits Arthur C. Clarke.
    provider = RecordingProvider(
        "stub",
        [
            {
                "key": "/works/2001",
                "title": "2001 A Space Odissey",
                "author_name": ["Arthur C. Clarke"],
            }
        ],
    )
    _use_providers(monkeypatch, provider)
    root = tmp_path / "calibre"
    book_dir = root / "Unknown" / "2001 A Space Odissey (12)"
    book_dir.mkdir(parents=True)
    (book_dir / "metadata.opf").write_text(
        _opf("2001 A Space Odissey", ["Unknown"]), encoding="utf-8"
    )
    (book_dir / "2001 A Space Odissey - Unknown.txt").write_text(
        "2001 A Space Odissey\n\nby Arthur C. Clarke\n\nChapter one.\n", encoding="utf-8"
    )

    (job,) = _import_all(root, tmp_path)

    sidecar = job.evidence[intake.SIDECAR_EVIDENCE_KEY]
    assert sidecar["conflicts"] == []  # F1: no "authors" contradiction
    assert "authors" not in sidecar["applied_fields"]  # F2
    assert "Unknown" not in (job.evidence.get("authors") or [])
    for query in _all_queries(provider):  # F3
        assert query["author"] != "Unknown"
        assert "unknown" not in (query["title"] or "").casefold()
        assert (query["folder_hint"] or "") != "Unknown"
    for search in MetadataSearch.objects.filter(import_job=job):
        assert "Unknown" not in str(search.query)
    path = job.evidence[identification.SOURCE_PATH_EVIDENCE_KEY]
    assert path["authors"] == []  # the Calibre "Unknown/" folder is never an author
    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Arthur C. Clarke"]


def test_12_beasts_of_gor_is_a_numbered_title_without_author() -> None:
    titles, authors = parse_filename_signals("12 - Beasts Of Gor.txt")
    assert titles[0] == "Beasts Of Gor"
    assert authors == []
    # Calibre copied that split into its record: title "12", author "Beasts Of Gor".
    sidecar = intake.normalize_sidecar_metadata({"title": "12", "authors": ["Beasts Of Gor"]})
    assert sidecar is not None
    assert "authors" not in sidecar and "title" not in sidecar
    reasons = {item["field"]: item["reason"] for item in sidecar[intake.SIDECAR_IGNORED_KEY]}
    assert reasons == {
        "authors": intake.SIDECAR_IGNORED_FILENAME_SPLIT,
        "title": intake.SIDECAR_IGNORED_FILENAME_SPLIT,
    }
    # An enriched record (ISBN, publisher, ...) is curated and kept; real names too.
    kept = intake.normalize_sidecar_metadata({"title": "Beasts of Gor", "authors": ["John Norman"]})
    assert kept == {"title": "Beasts of Gor", "authors": ["John Norman"]}
    # "1984 - George Orwell" keeps its author.
    assert parse_filename_signals("1984 - George Orwell.txt")[1] == ["George Orwell"]


# --- F4: an edition record without authors ------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_nightfall_authorless_isbn_record_takes_authors_from_a_same_title_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    isbn = "9780306406157"
    edition = RecordingProvider(
        "openlibrary",
        [
            {
                "key": "/books/OL1M",
                "title": "Nightfall and Other Stories",
                "isbn_13": [isbn],
                "_queried_isbn": isbn,
            }
        ],
    )
    other = RecordingProvider(
        "dnb",
        [{"key": "dnb-1", "title": "Nightfall and Other Stories", "author_name": ["Isaac Asimov"]}],
    )
    _use_providers(monkeypatch, edition, other)
    root = tmp_path / "share"
    _write_epub(
        root / "Isaac Asimov" / "Nightfall & Other Stories.epub",
        title="Nightfall & Other Stories",
        creator="Isaac Asimov",
        extra=f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>',
    )

    (job,) = _import_all(root, tmp_path)

    assert job.state == ImportJob.State.COMPLETE, job.evidence
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Isaac Asimov"]
    completed = job.evidence[intake.AUTO_ACCEPT_AUTHORS_COMPLETED_KEY]
    assert completed["provider_id"] == "dnb"
    assert completed["authors"] == ["Isaac Asimov"]


def test_placeholder_or_contradicting_authors_are_never_completed() -> None:
    signals = build_identification_input(
        source_path="/in/Ring of Fire.epub",
        original_filename="Ring of Fire.epub",
        extracted_metadata={"title": "Ring of Fire", "authors": ["Eric Flint"]},
    )
    base = identification.IdentificationCandidate(
        provider_id="a",
        provider_ref="a",
        title="Ring of Fire",
        authors=["Multiple Authors"],
        isbn10=None,
        isbn13=None,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=1.0,
        raw={},
    )
    other = identification.IdentificationCandidate(
        **{**base.__dict__, "provider_id": "b", "provider_ref": "b", "authors": ["Eric Flint"]}
    )
    other = other.__class__(**{**other.__dict__, "score": 0.85})
    decision = intake.decide_auto_accept([base, other], ["Eric Flint"], file_evidence=signals)
    assert decision.candidate is None  # "Multiple Authors" is an anthology, not a gap
    assert decision.blocked_reason == intake.BLOCK_NO_PROVIDER_AUTHORS


# --- F5: name order -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("Stross, Charles", "Stross Charles", 1.0),
        ("Charles Stross", "Stross Charles", 1.0),
        ("Stross Charles M.", "Charles M. Stross", 1.0),
        ("Stross Charles", "Charles M. Stross", identification.AUTHOR_VARIANT_MATCH_SCORE),
        ("Frank Herbert", "Brian Herbert", 0.0),
        ("Herbert Frank", "Brian Herbert", 0.0),
    ],
)
def test_author_names_match_regardless_of_comma_and_order(
    left: str, right: str, expected: float
) -> None:
    assert identification.author_similarity([left], [right]) == pytest.approx(expected)


@pytest.mark.django_db(transaction=True)
def test_accelerando_google_books_name_order_does_not_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = RecordingProvider(
        "google_books",
        [
            {
                "key": "gb-accelerando",
                "title": "Accelerando",
                "author_name": ["Stross Charles"],
                "language": "en",
            }
        ],
    )
    _use_providers(monkeypatch, provider)
    root = tmp_path / "share"
    _write_epub(
        root / "Accelerando.epub",
        title="Accelerando",
        creator="Stross, Charles",
        extra="<dc:language>en</dc:language>",
    )

    (job,) = _import_all(root, tmp_path)

    assert intake.AUTO_ACCEPT_BLOCKED_KEY not in job.evidence
    assert job.state == ImportJob.State.COMPLETE


# --- F6: edition mismatch vs. work conflict -----------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_flinx_in_flux_other_printing_is_accepted_as_the_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = RecordingProvider(
        "google_books",
        [
            {
                "key": "gb-flinx",
                "title": "Flinx in Flux",
                "author_name": ["Alan Dean Foster"],
                "publisher": "New English Library",
                "language": "en",
                "isbn_13": ["9780450048432"],
            }
        ],
    )
    _use_providers(monkeypatch, provider)
    root = tmp_path / "share"
    _write_epub(
        root / "Flinx in Flux.epub",
        title="Flinx in Flux",
        creator="Alan Dean Foster",
        extra="<dc:language>en</dc:language><dc:publisher>Ballantine Books</dc:publisher>",
    )

    (job,) = _import_all(root, tmp_path)

    assert job.state == ImportJob.State.COMPLETE
    mismatch = job.evidence[intake.AUTO_ACCEPT_EDITION_MISMATCH_KEY]
    assert mismatch["edition_conflicts"] == [identification.EDITION_PUBLISHER_MISMATCH]
    assert mismatch["edition_publishers"] == ["New English Library"]
    # The other printing's ISBN is not written onto the Book.
    assert job.book is not None
    assert not job.book.identifiers.filter(normalized_value="9780450048432").exists()


def test_a_work_conflict_still_blocks_even_with_a_publisher_mismatch() -> None:
    signals = build_identification_input(
        source_path="/in/Flinx in Flux.epub",
        original_filename="Flinx in Flux.epub",
        extracted_metadata={
            "title": "Flinx in Flux",
            "authors": ["Alan Dean Foster"],
            "publisher": "Ballantine Books",
        },
    )
    candidate = identification.score_candidate(
        signals,
        identification.IdentificationCandidate(
            provider_id="gb",
            provider_ref="gb",
            title="Flinx in Flux",
            authors=["Someone Else"],
            isbn10=None,
            isbn13=None,
            language=None,
            description=None,
            publisher="new english library",
            edition=None,
            publication_year=None,
            score=0.0,
            raw={},
            edition_publishers=("New English Library",),
            concrete_edition=True,
        ),
    )
    candidate = candidate.__class__(**{**candidate.__dict__, "score": 0.9})
    decision = intake.decide_auto_accept([candidate], ["Alan Dean Foster"], file_evidence=signals)
    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AUTHOR_CONFLICT


# --- F7: grouping records of one work -----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_brave_new_world_study_guide_and_duplicates_are_no_rivals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    huxley = {"key": "dnb-bnw", "title": "Brave New World", "author_name": ["Aldous Huxley"]}
    dnb = RecordingProvider(
        "dnb",
        [
            {
                "key": "dnb-sparknotes",
                "title": "Brave New World",
                "author_name": ["SparkNotes", "Aldous Huxley"],
            },
            huxley,
            huxley,
            huxley,
        ],
    )
    google = RecordingProvider(
        "google_books",
        [{"key": "gb-bnw", "title": "Brave New World", "author_name": ["Aldous Huxley"]}] * 2,
    )
    _use_providers(monkeypatch, dnb, google)
    root = tmp_path / "share"
    root.mkdir()
    (root / "Brave New World.txt").write_text(
        "Brave New World\n\nby Aldous Huxley\n\nChapter One\n", encoding="utf-8"
    )

    (job,) = _import_all(root, tmp_path)

    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Aldous Huxley"]
    candidate = job.metadata_candidates.filter(
        raw_payload__has_key=STRONG_MATCH_EVIDENCE_KEY
    ).first()
    assert candidate is not None
    rule = candidate.raw_payload[STRONG_MATCH_EVIDENCE_KEY]
    assert rule["applied"] is True
    assert "near_tied_candidate" not in rule["blocked_by"]
    # The three copies of one DNB record collapse into one candidate.
    refs = list(job.metadata_candidates.values_list("provider_ref", flat=True))
    assert refs.count("dnb-bnw") == 1


def test_a_study_guide_by_another_author_still_rivals_the_work() -> None:
    signals = build_identification_input(
        source_path="/in/Brave New World.txt",
        original_filename="Brave New World.txt",
        extracted_metadata={
            identification.TEXT_HEADER_EVIDENCE_KEY: {"authors": ["Aldous Huxley"], "rules": []}
        },
    )

    def candidate(ref: str, authors: list[str]) -> identification.IdentificationCandidate:
        return identification.IdentificationCandidate(
            provider_id="p",
            provider_ref=ref,
            title="Brave New World",
            authors=authors,
            isbn10=None,
            isbn13=None,
            language=None,
            description=None,
            publisher=None,
            edition=None,
            publication_year=None,
            score=0.85,
            raw={},
        )

    huxley = candidate("a", ["Aldous Huxley"])
    assert identification.candidates_group_as_one_work(
        huxley, candidate("b", ["SparkNotes", "Aldous Huxley"])
    )
    assert not identification.candidates_group_as_one_work(huxley, candidate("c", ["Jane Guide"]))
    del signals


@pytest.mark.parametrize("filename", ["cry, the beloved country.txt", "Hamlet.txt"])
def test_title_only_files_are_never_promoted(
    monkeypatch: pytest.MonkeyPatch, filename: str
) -> None:
    # F10: no author evidence at all; the best candidate is a study guide by another
    # author at the same score as the real work. Neither reaches auto-accept.
    title = Path(filename).stem.replace("cry,", "Cry,")
    provider = RecordingProvider(
        "stub",
        [
            {
                "key": "guide",
                "title": title,
                "author_name": ["Study Guide Author"],
                "language": "en",
            },
            {"key": "real", "title": title, "author_name": ["Real Author"], "language": "en"},
        ],
    )
    _use_providers(monkeypatch, provider)
    signals = build_identification_input(
        source_path=f"/in/{filename}", original_filename=filename, extracted_metadata={}
    )
    candidates = resolve_candidates(signals)
    assert all(candidate.score < AUTO_ACCEPT_SCORE for candidate in candidates)
    assert candidates[0].raw[STRONG_MATCH_EVIDENCE_KEY]["applied"] is False
    assert intake.decide_auto_accept(candidates, [], file_evidence=signals).candidate is None
    assert identification.decisive_candidate(signals, candidates) is None


# --- F9: header credits -------------------------------------------------------------------


def test_the_last_battle_site_mark_by_line_is_not_an_author() -> None:
    stem = "The Chronicles of Narnia 7 - The Last Battle"
    header = text_header.parse_text_header(
        "The Last Battle\n\nby Caldron Pool\n\nChapter One\n", stem
    )
    assert header is not None
    assert header["authors"] == []
    assert header["rejected_authors"] == [
        {"name": "Caldron Pool", "reason": text_header.CREDIT_SITE_MARK}
    ]
    signals = build_identification_input(
        source_path=f"/in/{stem}.txt",
        original_filename=f"{stem}.txt",
        extracted_metadata={identification.TEXT_HEADER_EVIDENCE_KEY: header},
    )
    assert "Caldron Pool" not in signals.author_hints
    assert all(group.source != "header" for group in signals.author_groups())


@pytest.mark.parametrize(
    ("name", "title", "explicit", "reason"),
    [
        ("Gutenberg Org", None, False, None),
        ("Scans Www.Example.Com", None, False, text_header.CREDIT_DOMAIN),
        ("Free Ebooks Library", None, False, text_header.CREDIT_SITE_MARK),
        ("House Atreides", "Dune: House Atreides", False, text_header.CREDIT_TITLE_SEGMENT),
        ("Jane Eyre", "Jane Eyre", True, text_header.CREDIT_TITLE_SEGMENT),
        ("Stephen King", "Stephen King", False, None),
        ("Brian Herbert", "Dune: House Atreides", False, None),
    ],
)
def test_header_credit_plausibility(
    name: str, title: str | None, explicit: bool, reason: str | None
) -> None:
    assert text_header.credit_rejection(name, title, explicit_title=explicit) == reason


# --- path addendum ------------------------------------------------------------------------


def test_last_first_folder_is_a_path_author_candidate_ahead_of_file_name_guesses() -> None:
    evidence = identification.path_evidence("Herbert, Frank/Dune - House Atreides.txt")
    assert evidence["authors"] == ["Frank Herbert"]
    assert evidence["author_folders"] == ["Herbert, Frank"]
    signals = build_identification_input(
        source_path="/share/Herbert, Frank/Dune - House Atreides.txt",
        original_filename="Dune - House Atreides.txt",
        extracted_metadata={identification.SOURCE_PATH_EVIDENCE_KEY: evidence},
    )
    assert signals.author_hints[0] == "Frank Herbert"
    assert signals.best_author() == "Frank Herbert"  # passed to the providers
    path_groups = [g for g in signals.author_groups() if g.source == EVIDENCE_PATH]
    assert [g.names for g in path_groups] == [("Frank Herbert",)]
    assert signals.folder_hints == ["Herbert, Frank"]


def test_path_author_alone_never_accepts_a_record_of_other_people() -> None:
    evidence = identification.path_evidence("Herbert, Frank/Dune - House Atreides.txt")
    signals = build_identification_input(
        source_path="/share/Herbert, Frank/Dune - House Atreides.txt",
        original_filename="Dune - House Atreides.txt",
        extracted_metadata={identification.SOURCE_PATH_EVIDENCE_KEY: evidence},
    )
    provider = RecordingProvider(
        "stub",
        [
            {
                "key": "atreides",
                "title": "Dune: House Atreides",
                "author_name": ["Brian Herbert", "Kevin J. Anderson"],
            },
            {"key": "dune", "title": "Dune", "author_name": ["Frank Herbert"]},
        ],
    )
    with pytest.MonkeyPatch.context() as patcher:
        _use_providers(patcher, provider)
        candidates = resolve_candidates(signals)
    assert intake.decide_auto_accept(candidates, [], file_evidence=signals).candidate is None
    dune = next(c for c in candidates if c.provider_ref == "dune")
    # "Dune" is only one part of the file name: never promoted.
    rule = dune.raw.get(STRONG_MATCH_EVIDENCE_KEY)
    assert dune.score < AUTO_ACCEPT_SCORE
    assert rule is None or rule["applied"] is False


@pytest.mark.parametrize(
    ("relative", "authors", "folders"),
    [
        ("Unknown/2001 (12)/2001 - Unknown.txt", [], ["2001 (12)", "Unknown"]),
        ("Asimov, Isaac/Gold - 02 - Left To Right.txt", ["Isaac Asimov"], ["Asimov, Isaac"]),
        ("Shakespeare, William/Hamlet.txt", ["William Shakespeare"], ["Shakespeare, William"]),
        ("Dragonlance/Kendermore.txt", [], ["Dragonlance"]),
        ("Hobbit, The/The Hobbit.txt", [], ["Hobbit, The"]),
        ("Grimm, Gebruder/Fables/Wolf.txt", ["Gebruder Grimm"], ["Fables", "Grimm, Gebruder"]),
    ],
)
def test_path_evidence_reads_only_last_first_folders_as_authors(
    relative: str, authors: list[str], folders: list[str]
) -> None:
    evidence = identification.path_evidence(relative)
    assert evidence["authors"] == authors
    assert evidence["folders"] == folders


# --- G2: concurrency, early exit, budget, circuit breaker, reuse ---------------------------


def _signals_with_embedded_author(*, isbn: str | None = None) -> identification.IdentificationInput:
    return build_identification_input(
        source_path="/in/Example Book.epub",
        original_filename="Example Book.epub",
        extracted_metadata={"title": "Example Book", "authors": ["Author Name"], "isbn": isbn},
    )


class SlowProvider(RecordingProvider):
    def __init__(self, provider_id: str, release: threading.Event) -> None:
        super().__init__(provider_id, [{"key": "slow", "title": "Other", "author_name": ["X Y"]}])
        self.release = release

    def search(self, title: str | None = None, **terms: Any) -> list[dict[str, Any]]:
        self.release.wait(timeout=5)
        return super().search(title, **terms)


@override_settings(BOOK_SCANNER_IDENTIFICATION_EARLY_EXIT=True)
def test_early_exit_stops_waiting_once_a_provider_confirms_the_source_isbn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    isbn = "9780306406157"
    release = threading.Event()
    fast = RecordingProvider(
        "fast",
        [
            {
                "key": "hit",
                "title": "Example Book",
                "author_name": ["Author Name"],
                "isbn_13": [isbn],
            }
        ],
    )
    slow = SlowProvider("slow", release)
    _use_providers(monkeypatch, slow, fast)
    try:
        candidates, outcomes = resolve_candidates_with_report(
            _signals_with_embedded_author(isbn=isbn)
        )
    finally:
        release.set()
    assert [c.provider_ref for c in candidates] == ["hit"]
    assert [(o.provider_id, o.status) for o in outcomes] == [
        ("slow", "skipped"),
        ("fast", "results"),
    ]
    assert outcomes[0].error == identification.PROVIDER_SKIPPED_REASON


@override_settings(
    BOOK_SCANNER_IDENTIFICATION_EARLY_EXIT=True,
    BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_ENABLED=True,
)
def test_same_work_different_editions_wait_for_the_slower_provider(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = threading.Event()
    record = {"title": "Example Book", "author_name": ["Author Name"]}
    fast = RecordingProvider(
        "fast", [{**record, "key": "fast-edition", "isbn_13": ["9780306406157"]}]
    )
    slow = SlowProvider("slow", release)
    slow.records = [{**record, "key": "slow-edition", "isbn_13": ["9780141187761"]}]
    _use_providers(monkeypatch, fast, slow)
    signals = _signals_with_embedded_author()
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        candidates, outcomes = resolve_candidates_with_report(signals)
    finally:
        release.set()
        timer.cancel()

    assert [outcome.status for outcome in outcomes] == ["results", "results"]
    decision = intake.decide_auto_accept(candidates, ["Author Name"], file_evidence=signals)
    assert decision.candidate is not None
    assert decision.candidate.title == "Example Book"
    assert decision.candidate.isbn13 is None


@override_settings(BOOK_SCANNER_IDENTIFICATION_EARLY_EXIT=True)
def test_a_title_only_hit_is_not_decisive(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    fast = RecordingProvider(
        "fast", [{"key": "hit", "title": "Example Book", "author_name": ["Author Name"]}]
    )
    slow = SlowProvider("slow", release)
    _use_providers(monkeypatch, slow, fast)
    signals = build_identification_input(
        source_path="/in/Example Book.txt",
        original_filename="Example Book.txt",
        extracted_metadata={},
    )
    timer = threading.Timer(0.2, release.set)
    timer.start()
    try:
        _, outcomes = resolve_candidates_with_report(signals)
    finally:
        release.set()
        timer.cancel()
    assert [o.status for o in outcomes] == ["results", "results"]


@override_settings(BOOK_SCANNER_METADATA_PROVIDER_TIMEOUT_SECONDS=0.2)
def test_provider_over_budget_is_reported_and_counted_by_its_breaker(
    monkeypatch: pytest.MonkeyPatch, provider_state: provider_runtime.LocalProviderStateStore
) -> None:
    release = threading.Event()
    fast = RecordingProvider("fast", [])
    slow = SlowProvider("slow", release)
    _use_providers(monkeypatch, slow, fast)
    try:
        _, outcomes = resolve_candidates_with_report(_signals_with_embedded_author())
    finally:
        release.set()
    assert (outcomes[0].status, outcomes[0].error) == (
        "error",
        identification.PROVIDER_BUDGET_REASON,
    )
    assert provider_state.get("failures:slow") == "1"


@override_settings(BOOK_SCANNER_METADATA_PROVIDER_BREAKER_FAILURES=2)
def test_circuit_breaker_skips_a_provider_that_failed_repeatedly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Failing:
        provider_id = "down"
        calls = 0

        def search(self, title: str | None = None, **_: Any) -> list[dict[str, Any]]:
            Failing.calls += 1
            raise TimeoutError("down")

    _use_providers(monkeypatch, Failing())
    for number in range(2):
        signals = build_identification_input(
            source_path=f"/in/Book {number}.epub",
            original_filename=f"Book {number}.epub",
            extracted_metadata={"title": f"Book {number}"},
        )
        _, outcomes = resolve_candidates_with_report(signals)
        assert outcomes[0].error == "timed out"
    assert provider_runtime.breaker_open("down")
    _, outcomes = resolve_candidates_with_report(_signals_with_embedded_author())
    assert (outcomes[0].status, outcomes[0].error) == (
        "error",
        identification.PROVIDER_CIRCUIT_OPEN_REASON,
    )
    assert Failing.calls == 2  # one failed request per lookup, then skipped


def test_identical_queries_reuse_the_provider_result(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = RecordingProvider(
        "stub", [{"key": "hit", "title": "Example Book", "author_name": ["Author Name"]}]
    )
    _use_providers(monkeypatch, provider)
    first, first_outcomes = resolve_candidates_with_report(_signals_with_embedded_author())
    second, second_outcomes = resolve_candidates_with_report(_signals_with_embedded_author())
    assert len(provider.queries) == 1
    assert [c.provider_ref for c in first] == [c.provider_ref for c in second] == ["hit"]
    assert first_outcomes[0].cached is False
    assert second_outcomes[0].cached is True
    assert second_outcomes[0].as_dict()["cached"] is True


@override_settings(BOOK_SCANNER_METADATA_PROVIDER_CACHE_SECONDS=0)
def test_result_reuse_can_be_switched_off(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = RecordingProvider("stub", [])
    _use_providers(monkeypatch, provider)
    resolve_candidates_with_report(_signals_with_embedded_author())
    resolve_candidates_with_report(_signals_with_embedded_author())
    assert len(provider.queries) == 2 * 1  # one title hint, asked again each time
