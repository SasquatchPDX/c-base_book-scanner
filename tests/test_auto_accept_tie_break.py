"""Tie-break among equally scored candidates during auto-accept (#46).

With several metadata providers, two candidates often share the top score (an ISBN
match scores 1.0 everywhere) and their order is only the configured provider order.
The author checks must not stop at whichever came first.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from django.test import Client

from ebook_server.core import identification, intake
from ebook_server.core.models import Book, ImportJob, MetadataCandidate, ReviewTask
from tests.test_isbn_match_authors import ISBN13, _upload


def _candidate(
    provider_id: str,
    *,
    authors: list[str],
    score: float = 1.0,
    title: str = "Ring of Fire",
    isbn13: str | None = None,
) -> identification.IdentificationCandidate:
    return identification.IdentificationCandidate(
        provider_id=provider_id,
        provider_ref=f"{provider_id}-ref",
        title=title,
        authors=authors,
        isbn10=None,
        isbn13=isbn13,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=score,
        raw={},
    )


def _evidence(**overrides: Any) -> identification.IdentificationInput:
    values: dict[str, Any] = {
        "title_hints": ["Ring of Fire"],
        "author_hints": ["Eric Flint"],
        "isbns": [],
        "folder_hints": [],
        "language": None,
        "publisher": None,
        "description": None,
    }
    values.update(overrides)
    return identification.IdentificationInput(**values)


# A file without ISBN, language or publisher: only the author checks apply.
NO_FILE_EVIDENCE = _evidence()


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.86)
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_TIE_MARGIN", 0.02)


def test_tied_placeholder_candidate_falls_through_to_non_conflicting_one() -> None:
    openlibrary = _candidate("openlibrary", authors=["Multiple Authors"])
    google = _candidate("google_books", authors=["Eric Flint"])

    decision = intake.decide_auto_accept(
        [openlibrary, google], ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is google
    assert decision.blocked_reason is None
    assert decision.tie_break is not None
    assert decision.tie_break["chosen"]["provider_id"] == "google_books"
    assert decision.tie_break["passed_over"] == [
        {
            "provider_id": "openlibrary",
            "provider_ref": "openlibrary-ref",
            "title": "Ring of Fire",
            "authors": ["Multiple Authors"],
            "score": 1.0,
            "reason": intake.BLOCK_NO_PROVIDER_AUTHORS,
        }
    ]
    assert decision.passed_over == (openlibrary,)


def test_tied_conflicting_candidate_falls_through() -> None:
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Flint, Eric"], score=0.99)

    decision = intake.decide_auto_accept(
        [first, second], ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is second
    assert decision.tie_break is not None
    assert decision.tie_break["passed_over"][0]["reason"] == intake.BLOCK_AUTHOR_CONFLICT


def test_candidate_outside_tie_margin_is_not_used() -> None:
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Eric Flint"], score=0.95)

    decision = intake.decide_auto_accept(
        [first, second], ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AUTHOR_CONFLICT
    assert set(map(id, decision.passed_over)) == {id(first), id(second)}


def test_tie_margin_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_TIE_MARGIN", 0.1)
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Eric Flint"], score=0.95)

    decision = intake.decide_auto_accept(
        [first, second], ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is second


def test_tied_candidates_below_threshold_are_never_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_TIE_MARGIN", 0.5)
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Eric Flint"], score=0.8)

    decision = intake.decide_auto_accept(
        [first, second], ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AUTHOR_CONFLICT


def test_first_candidate_is_kept_when_it_passes() -> None:
    first = _candidate("a", authors=["Eric Flint"])
    second = _candidate("b", authors=["Kevin J. Anderson"])

    decision = intake.decide_auto_accept(
        [first, second], ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is first
    assert decision.tie_break is None
    assert decision.passed_over == (second,)


def test_tied_records_of_the_same_book_are_not_ambiguous() -> None:
    by_isbn = [
        _candidate("a", authors=["Eric Flint"], isbn13=ISBN13, title="Ring of Fire"),
        _candidate("b", authors=["Eric Flint"], isbn13=ISBN13, title="1632: Ring of Fire"),
    ]
    by_title = [
        _candidate("a", authors=["Eric Flint"]),
        _candidate("b", authors=["Flint, Eric"]),
    ]
    for candidates in (by_isbn, by_title):
        decision = intake.decide_auto_accept(
            candidates, ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
        )
        assert decision.candidate is candidates[0]
        assert decision.passed_over == ()


def test_several_different_tied_books_go_to_review() -> None:
    candidates = [
        _candidate("a", authors=["Eric Flint"], title="Ring of Fire"),
        _candidate("b", authors=["Eric Flint"], title="Ring of Fire II"),
    ]

    decision = intake.decide_auto_accept(candidates, ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE)

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AMBIGUOUS_TIE
    assert [entry["title"] for entry in decision.details["tied_candidates"]] == [
        "Ring of Fire",
        "Ring of Fire II",
    ]


def test_same_title_with_conflicting_authors_is_a_different_book() -> None:
    left = _candidate("a", authors=["Frank Herbert"], title="Dune")
    right = _candidate("b", authors=["Brian Herbert"], title="Dune")
    assert not identification.candidates_describe_same_book(left, right)


# --- End to end with two providers ------------------------------------------------


def _two_providers(monkeypatch: pytest.MonkeyPatch, records: list[dict[str, Any]]) -> None:
    def make(provider_id: str, payload: dict[str, Any]) -> Any:
        class StubProvider:
            def search(
                self,
                title: str | None = None,
                *,
                author: str | None = None,
                isbn: str | None = None,
                folder_hint: str | None = None,
            ) -> list[dict[str, Any]]:
                del title, author, isbn, folder_hint
                return [dict(payload)]

        provider = StubProvider()
        provider.provider_id = provider_id  # type: ignore[attr-defined]
        return provider

    providers = [make(record.pop("provider_id"), record) for record in records]
    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: providers,
    )


@pytest.mark.django_db(transaction=True)
def test_import_accepts_tied_candidate_that_matches_the_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_providers(
        monkeypatch,
        [
            {
                "provider_id": "openlibrary",
                "key": "/books/OL1M",
                "title": "Phylogenesis",
                "author_name": ["Multiple Authors"],
                "isbn_13": [ISBN13],
            },
            {
                "provider_id": "google_books",
                "key": "gb-1",
                "title": "Phylogenesis",
                "author_name": ["Alan Dean Foster"],
                "isbn_13": [ISBN13],
            },
        ],
    )

    job = _upload(monkeypatch, tmp_path, Client())

    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Alan Dean Foster"]
    tie_break = job.evidence[intake.AUTO_ACCEPT_TIE_BREAK_KEY]
    assert tie_break["chosen"]["provider_id"] == "google_books"
    assert tie_break["passed_over"][0]["provider_id"] == "openlibrary"
    states = dict(job.metadata_candidates.values_list("provider_id", "state"))
    assert states == {
        "google_books": MetadataCandidate.CandidateState.MATCH,
        "openlibrary": MetadataCandidate.CandidateState.REVIEW,
    }
    assert not ReviewTask.objects.filter(import_job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_import_with_ambiguous_tie_goes_to_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_providers(
        monkeypatch,
        [
            {
                "provider_id": "openlibrary",
                "key": "/books/OL1M",
                "title": "Phylogenesis",
                "author_name": ["Alan Dean Foster"],
                "isbn_13": [ISBN13],
            },
            {
                "provider_id": "google_books",
                "key": "gb-2",
                "title": "Phylogenesis: The Commonwealth",
                "author_name": ["Alan Dean Foster"],
                "isbn_13": [ISBN13],
            },
        ],
    )
    # Both stub records carry the file's ISBN (that is what makes both score 1.0), which
    # would make them one book; treat them as different books for this test.
    monkeypatch.setattr(
        identification,
        "candidates_describe_same_book",
        lambda left, right: False,
    )
    client = Client()

    job = _upload(monkeypatch, tmp_path, client)

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.evidence[intake.AUTO_ACCEPT_BLOCKED_KEY] == intake.BLOCK_AMBIGUOUS_TIE
    assert not Book.objects.exists()
    task = ReviewTask.objects.get(import_job=job)
    html = client.get(f"/reviews/{task.id}/?view=html").content.decode()
    assert 'data-block-reason="ambiguous_tie"' in html
    assert "Several different books tied" in html


# --- Tie eligibility keeps the edition/identifier constraints (review of #94) -----

CONFLICTING_ISBN13 = "9780451524935"
CONFLICTING_ISBN13_2 = "9780140283297"
# The reviewer's probe: the file's ISBN, title, author, language, publisher, description.
RING_OF_FIRE_FILE = _evidence(
    isbns=[ISBN13], language="en", publisher="baen", description="An alternate history"
)


def _ring_of_fire_records() -> list[dict[str, Any]]:
    return [
        # Matching ISBN, but only a placeholder author: blocked by the author checks.
        {
            "provider_id": "openlibrary",
            "key": "/books/OL1M",
            "title": "Ring of Fire",
            "author_name": ["Multiple Authors"],
            "isbn_13": [ISBN13],
            "_queried_isbn": ISBN13,
        },
        # Everything else matches, but it is another edition's ISBN.
        {
            "provider_id": "google_books",
            "key": "gb-ring",
            "title": "Ring of Fire",
            "author_name": ["Eric Flint"],
            "isbn_13": [CONFLICTING_ISBN13],
            "language": "en",
            "publisher": "Baen",
            "subtitle": "An alternate history",
        },
    ]


def test_probe_tied_fallback_with_conflicting_isbn_is_not_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_providers(monkeypatch, _ring_of_fire_records())
    candidates = identification.resolve_candidates(RING_OF_FIRE_FILE)
    assert [(c.provider_id, c.score) for c in candidates] == [
        ("openlibrary", 1.0),
        ("google_books", 1.0),
    ]

    decision = intake.decide_auto_accept(
        candidates, ["Eric Flint"], file_evidence=RING_OF_FIRE_FILE
    )

    # As before #46 (#92): the matching-ISBN record's missing authors send it to review.
    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_NO_PROVIDER_AUTHORS
    assert set(map(id, decision.passed_over)) == set(map(id, candidates))
    assert identification.edition_conflicts(candidates[1], RING_OF_FIRE_FILE) == [
        identification.EDITION_ISBN_MISMATCH
    ]


def test_tied_fallback_without_any_isbn_does_not_replace_the_files_isbn() -> None:
    top = _candidate("a", authors=["Multiple Authors"], isbn13=ISBN13)
    fallback = _candidate("b", authors=["Eric Flint"])

    decision = intake.decide_auto_accept(
        [top, fallback], ["Eric Flint"], file_evidence=RING_OF_FIRE_FILE
    )

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_NO_PROVIDER_AUTHORS


def test_tied_fallback_carrying_the_files_isbn_replaces_a_conflicting_top() -> None:
    top = _candidate("a", authors=["Eric Flint"], isbn13=CONFLICTING_ISBN13)
    fallback = _candidate("b", authors=["Eric Flint"], isbn13=ISBN13)

    decision = intake.decide_auto_accept(
        [top, fallback], ["Eric Flint"], file_evidence=_evidence(isbns=[ISBN13])
    )

    assert decision.candidate is fallback
    assert decision.tie_break is not None
    passed = decision.tie_break["passed_over"][0]
    assert passed["reason"] == intake.BLOCK_EDITION_CONFLICT
    assert passed["edition_conflicts"] == [identification.EDITION_ISBN_MISMATCH]
    assert decision.passed_over == (top,)


def test_single_top_with_conflicting_isbn_reports_edition_details() -> None:
    top = _candidate("a", authors=["Eric Flint"], isbn13=CONFLICTING_ISBN13)

    decision = intake.decide_auto_accept(
        [top], ["Eric Flint"], file_evidence=_evidence(isbns=[ISBN13])
    )

    assert decision.blocked_reason == intake.BLOCK_EDITION_CONFLICT
    assert decision.details["edition_conflicts"] == [identification.EDITION_ISBN_MISMATCH]
    assert decision.details["file_isbns"] == [ISBN13]
    assert decision.details["edition_isbns"] == [CONFLICTING_ISBN13]


def test_same_title_and_authors_with_disjoint_isbns_are_different_editions() -> None:
    left = _candidate("a", authors=["Eric Flint"], isbn13=ISBN13)
    right = _candidate("b", authors=["Eric Flint"], isbn13=CONFLICTING_ISBN13)
    assert not identification.candidates_describe_same_book(left, right)
    # One side without ISBNs: title and authors decide.
    assert identification.candidates_describe_same_book(
        left, _candidate("c", authors=["Flint, Eric"])
    )
    # Disjoint languages are different editions too.
    english = dataclasses.replace(_candidate("d", authors=["Eric Flint"]), language="eng")
    german = dataclasses.replace(_candidate("e", authors=["Eric Flint"]), language="ger")
    assert not identification.candidates_describe_same_book(english, german)


def test_tied_editions_without_file_isbn_accept_the_work_only() -> None:
    # Two editions of one work fit a file that names no ISBN: the work is identified,
    # its edition is not. It is accepted without either edition's ISBN.
    candidates = [
        _candidate("a", authors=["Eric Flint"], isbn13=ISBN13),
        _candidate("b", authors=["Eric Flint"], isbn13=CONFLICTING_ISBN13),
    ]

    decision = intake.decide_auto_accept(candidates, ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE)

    assert decision.candidate is not None
    assert decision.candidate.title == "Ring of Fire"
    assert decision.candidate.isbn13 is None
    assert decision.blocked_reason is None
    assert decision.tie_break is None  # the top record was picked, only stripped


def test_tied_translations_without_file_language_drop_the_language() -> None:
    candidates = [
        dataclasses.replace(_candidate("a", authors=["Eric Flint"]), language="eng"),
        dataclasses.replace(_candidate("b", authors=["Eric Flint"]), language="ger"),
    ]

    decision = intake.decide_auto_accept(candidates, ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE)

    assert decision.candidate is not None
    assert decision.candidate.language is None


def test_tied_editions_with_a_file_isbn_still_go_to_review() -> None:
    # With a file ISBN the edition is knowable; the existing edition checks decide.
    candidates = [
        _candidate("a", authors=["Eric Flint"], isbn13=CONFLICTING_ISBN13),
        _candidate("b", authors=["Eric Flint"], isbn13=CONFLICTING_ISBN13_2),
    ]

    decision = intake.decide_auto_accept(
        candidates, ["Eric Flint"], file_evidence=_evidence(isbns=[ISBN13])
    )

    assert decision.candidate is None


def test_tied_different_books_without_file_isbn_still_go_to_review() -> None:
    candidates = [
        _candidate("a", authors=["Eric Flint"], title="Ring of Fire", isbn13=ISBN13),
        _candidate("b", authors=["Eric Flint"], title="Ring of Fire II", isbn13=CONFLICTING_ISBN13),
    ]

    decision = intake.decide_auto_accept(candidates, ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE)

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AMBIGUOUS_TIE


def test_tie_margin_boundary_is_inclusive_despite_float_rounding() -> None:
    assert 1.0 - 0.98 > 0.02  # the floating-point trap this guards against
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Eric Flint"], score=0.98)

    decision = intake.decide_auto_accept(
        [first, second], ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is second


def _build_ring_of_fire_epub(path: Path, *, creator: str) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:opf="http://www.idpf.org/2007/opf">'
            "<dc:title>Ring of Fire</dc:title>"
            f"<dc:creator>{creator}</dc:creator>"
            f'<dc:identifier opf:scheme="ISBN">{ISBN13}</dc:identifier>'
            "<dc:language>en</dc:language><dc:publisher>Baen</dc:publisher>"
            "<dc:description>An alternate history</dc:description>"
            "</metadata></package>",
        )


@pytest.mark.django_db(transaction=True)
def test_probe_import_does_not_accept_tied_conflicting_isbn(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _two_providers(monkeypatch, _ring_of_fire_records())
    monkeypatch.setattr("tests.test_isbn_match_authors._build_epub", _build_ring_of_fire_epub)

    job = _upload(monkeypatch, tmp_path, Client(), creator="Eric Flint")

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert job.evidence[intake.AUTO_ACCEPT_BLOCKED_KEY] == intake.BLOCK_NO_PROVIDER_AUTHORS
    assert intake.AUTO_ACCEPT_TIE_BREAK_KEY not in job.evidence
    scores = dict(job.metadata_candidates.values_list("provider_id", "score"))
    assert scores == {"openlibrary": 1, "google_books": 1}
    assert set(job.metadata_candidates.values_list("state", flat=True)) == {
        MetadataCandidate.CandidateState.REVIEW
    }
    assert not Book.objects.exists()


# --- Same work: volumes, subtitles and surname-only credits (review of #150) -------


def _with_subtitle(
    candidate: identification.IdentificationCandidate, subtitle: str
) -> identification.IdentificationCandidate:
    return dataclasses.replace(candidate, description=subtitle, raw={"subtitle": subtitle})


def test_tied_volumes_of_one_title_go_to_review_without_file_isbn() -> None:
    candidates = [
        _with_subtitle(
            _candidate("a", authors=["Ralph Waldo Emerson"], title="Essays"), "First Series"
        ),
        _with_subtitle(
            _candidate("b", authors=["Ralph Waldo Emerson"], title="Essays"), "Second Series"
        ),
    ]

    assert not identification.candidates_describe_same_work(*candidates)
    assert not identification.candidates_describe_same_book(*candidates)
    decision = intake.decide_auto_accept(
        candidates, ["Ralph Waldo Emerson"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AMBIGUOUS_TIE


def test_tied_editions_drop_the_subtitle_from_the_work() -> None:
    # One edition's subtitle is not the work's: the accepted work carries none.
    candidates = [
        _with_subtitle(_candidate("a", authors=["Eric Flint"], isbn13=ISBN13), "Assiti Shards"),
        _with_subtitle(
            _candidate("b", authors=["Eric Flint"], isbn13=CONFLICTING_ISBN13), "Assiti Shards"
        ),
    ]

    decision = intake.decide_auto_accept(candidates, ["Eric Flint"], file_evidence=NO_FILE_EVIDENCE)

    assert decision.candidate is not None
    assert decision.candidate.isbn13 is None
    assert decision.candidate.description is None


def test_surname_only_credit_does_not_link_two_people() -> None:
    herbert = _candidate("a", authors=["Herbert"], title="Dune")
    brian = _candidate("b", authors=["Brian Herbert"], title="Dune")
    frank = _candidate("c", authors=["Frank Herbert"], title="Dune")
    frank_variant = _candidate("d", authors=["Herbert, Frank"], title="Dune")

    assert not identification.candidates_describe_same_work(herbert, brian)
    assert not identification.candidates_describe_same_work(frank, brian)
    assert identification.candidates_describe_same_work(frank, frank_variant)
    decision = intake.decide_auto_accept(
        [herbert, brian], ["Herbert"], file_evidence=NO_FILE_EVIDENCE
    )

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AMBIGUOUS_TIE
