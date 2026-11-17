"""Tie-break among equally scored candidates during auto-accept (#46).

With several metadata providers, two candidates often share the top score (an ISBN
match scores 1.0 everywhere) and their order is only the configured provider order.
The author checks must not stop at whichever came first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

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


@pytest.fixture(autouse=True)
def _thresholds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.86)
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_TIE_MARGIN", 0.02)


def test_tied_placeholder_candidate_falls_through_to_non_conflicting_one() -> None:
    openlibrary = _candidate("openlibrary", authors=["Multiple Authors"])
    google = _candidate("google_books", authors=["Eric Flint"])

    decision = intake.decide_auto_accept([openlibrary, google], ["Eric Flint"])

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

    decision = intake.decide_auto_accept([first, second], ["Eric Flint"])

    assert decision.candidate is second
    assert decision.tie_break is not None
    assert decision.tie_break["passed_over"][0]["reason"] == intake.BLOCK_AUTHOR_CONFLICT


def test_candidate_outside_tie_margin_is_not_used() -> None:
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Eric Flint"], score=0.95)

    decision = intake.decide_auto_accept([first, second], ["Eric Flint"])

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AUTHOR_CONFLICT
    assert set(map(id, decision.passed_over)) == {id(first), id(second)}


def test_tie_margin_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_TIE_MARGIN", 0.1)
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Eric Flint"], score=0.95)

    decision = intake.decide_auto_accept([first, second], ["Eric Flint"])

    assert decision.candidate is second


def test_tied_candidates_below_threshold_are_never_accepted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_TIE_MARGIN", 0.5)
    first = _candidate("a", authors=["Kevin J. Anderson"])
    second = _candidate("b", authors=["Eric Flint"], score=0.8)

    decision = intake.decide_auto_accept([first, second], ["Eric Flint"])

    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AUTHOR_CONFLICT


def test_first_candidate_is_kept_when_it_passes() -> None:
    first = _candidate("a", authors=["Eric Flint"])
    second = _candidate("b", authors=["Kevin J. Anderson"])

    decision = intake.decide_auto_accept([first, second], ["Eric Flint"])

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
        decision = intake.decide_auto_accept(candidates, ["Eric Flint"])
        assert decision.candidate is candidates[0]
        assert decision.passed_over == ()


def test_several_different_tied_books_go_to_review() -> None:
    candidates = [
        _candidate("a", authors=["Eric Flint"], title="Ring of Fire"),
        _candidate("b", authors=["Eric Flint"], title="Ring of Fire II"),
    ]

    decision = intake.decide_auto_accept(candidates, ["Eric Flint"])

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
