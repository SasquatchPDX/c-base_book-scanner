from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from ebook_server.core.identification import (
    AUTHOR_VARIANT_MATCH_SCORE,
    AUTO_ACCEPT_SCORE,
    STRONG_MATCH_EVIDENCE_KEY,
    IdentificationCandidate,
    IdentificationInput,
    _author_match_score,
    _author_name_similarity,
    _title_match_score,
    build_identification_input,
    parse_filename_signals,
    resolve_candidates,
)


def _stub_provider_from_candidates(
    payloads: list[dict[str, Any]],
) -> type[object]:
    class StubProvider:
        provider_id = "stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, Any]]:
            del title, author, folder_hint
            if isbn is not None:
                isbn_hits: list[dict[str, Any]] = []
                for entry in payloads:
                    raw_isbn_10 = entry.get("isbn_10")
                    if isinstance(raw_isbn_10, list) and isbn in raw_isbn_10:
                        isbn_hits.append(entry)
                        continue
                    raw_isbn_13 = entry.get("isbn_13")
                    if isinstance(raw_isbn_13, list) and isbn in raw_isbn_13:
                        isbn_hits.append(entry)
                if isbn_hits:
                    return isbn_hits
            return payloads

    return StubProvider


def test_parse_filename_signals_handles_title_author_patterns() -> None:
    title_hints, author_hints = parse_filename_signals(
        "Jules Verne - Around the World in 80 Days.epub",
    )

    assert "Jules Verne" in author_hints
    assert "Around the World in 80 Days" in title_hints


def test_build_identification_input_uses_folder_as_hint() -> None:
    payload = build_identification_input(
        source_path=str(Path("/incoming/Anne%20Rice/The%20Vampire%20Lestat.epub")),
        original_filename="The%20Vampire%20Lestat.epub",
        extracted_metadata={"title": "The Vampire Lestat", "authors": ["Anne Rice"]},
    )

    assert payload.title_hints[:2] == ["The Vampire Lestat", "The%20Vampire%20Lestat"]
    assert payload.author_hints[:1] == ["Anne Rice"]
    assert payload.folder_hints == ["Anne%20Rice"]


def test_exact_isbn_match_scores_high() -> None:
    signals = build_identification_input(
        source_path="/inbox/sample.pdf",
        original_filename="sample.pdf",
        extracted_metadata={"isbn": "9780306406157"},
    )
    candidate_payload = {
        "title": "The Definitive Edition",
        "author_name": ["Valid Author"],
        "isbn_13": ["9780306406157"],
    }
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [_stub_provider_from_candidates([candidate_payload])()],
    ):
        candidates = resolve_candidates(signals)

    assert candidates
    top = candidates[0]
    assert top.isbn13 == "9780306406157"
    assert top.score >= 0.9
    assert isinstance(top, IdentificationCandidate)


def test_filename_signal_title_author_score() -> None:
    signals = IdentificationInput(
        title_hints=["The Martian"],
        author_hints=["Andy Weir"],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )
    candidate_payload = {
        "title": "The Martian",
        "author_name": ["Andy Weir"],
        "publisher": "Crown Publishing",
    }
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [_stub_provider_from_candidates([candidate_payload])()],
    ):
        candidates = resolve_candidates(signals)

    assert len(candidates) == 1
    assert candidates[0].score > 0.7


def test_no_match_produces_no_candidates() -> None:
    signals = IdentificationInput(
        title_hints=["Completely Unknown Title"],
        author_hints=["Unknown Author"],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [_stub_provider_from_candidates([])()],
    ):
        candidates = resolve_candidates(signals)

    assert candidates == []


def test_failing_provider_does_not_abort_other_providers() -> None:
    signals = IdentificationInput(
        title_hints=["Resilient Metadata"],
        author_hints=[],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )

    class FailingProvider:
        provider_id = "failing"

        def search(self, *args: object, **kwargs: object) -> list[dict[str, Any]]:
            del args, kwargs
            raise OSError("provider unavailable")

    healthy_provider = _stub_provider_from_candidates(
        [{"title": "Resilient Metadata", "author_name": []}],
    )()
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [FailingProvider(), healthy_provider],
    ):
        candidates = resolve_candidates(signals)

    assert len(candidates) == 1
    assert candidates[0].provider_id == "stub"


def test_conflicting_metadata_keeps_filename_signal() -> None:
    payload = build_identification_input(
        source_path="/incoming/series/The%20War%20of%20The%20Worlds.pdf",
        original_filename="The%20War%20of%20The%20Worlds.pdf",
        extracted_metadata={"title": "Wrong Title", "authors": ["Confident Author"]},
    )

    assert payload.title_hints[0] == "Wrong Title"
    assert payload.title_hints[1] == "The%20War%20of%20The%20Worlds"


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("H P Lovecraft", "H. P. Lovecraft"),
        ("HP Lovecraft", "H. P. Lovecraft"),
        ("Banks, Iain", "Iain Banks"),
        ("Ursula K. Le Guin", "Ursula K. LeGuin"),
        ("ANDY WEIR", "Andy Weir"),
    ],
)
def test_author_name_similarity_treats_formatting_variants_as_equal(left: str, right: str) -> None:
    assert _author_name_similarity(left, right) == 1.0


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Iain M. Banks", "Iain Banks"),
        ("J. R. R. Tolkien", "John Ronald Reuel Tolkien"),
        ("Gebrüder Grimm", "Brüder Grimm"),
    ],
)
def test_author_name_similarity_gives_partial_score_to_name_variants(left: str, right: str) -> None:
    # Regression for #18: these scored 0 with exact string comparison.
    assert _author_name_similarity(left, right) == AUTHOR_VARIANT_MATCH_SCORE
    assert _author_name_similarity(right, left) == AUTHOR_VARIANT_MATCH_SCORE


@pytest.mark.parametrize(
    ("left", "right"),
    [
        ("Frank Herbert", "Brian Herbert"),
        ("Christopher Tolkien", "J. R. R. Tolkien"),
        ("Jane Smith", "John Smith"),
        ("Herbert", "Frank Herbert"),
        ("Neal Stephenson", "Neal Asher"),
        ("Snow Crash", "Neal Stephenson"),
    ],
)
def test_author_name_similarity_rejects_different_people(left: str, right: str) -> None:
    assert _author_name_similarity(left, right) == 0.0


def test_author_match_score_uses_best_variant_per_hint() -> None:
    assert _author_match_score(["Iain M. Banks"], ["Iain Banks"]) == AUTHOR_VARIANT_MATCH_SCORE
    assert _author_match_score(["Andy Weir"], ["Someone Else", "Andy Weir"]) == 1.0
    # Unmatched hints still dilute the score, as before.
    assert _author_match_score(["Andy Weir", "The Martian"], ["Andy Weir"]) == 0.5
    assert _author_match_score([], ["Andy Weir"]) == 0.0


def test_title_match_score_does_not_give_full_marks_to_subset_titles() -> None:
    # Regression for #8: token_set_ratio alone scored these 1.0.
    assert _title_match_score("Foundation", "Foundation and Earth") < 0.9
    assert _title_match_score("Dune", "Dune - House Harkonnen") < 0.75
    assert _title_match_score("Gold", "Gold - 03 - Frustration") < 0.75


def test_title_match_score_still_tolerates_case_punctuation_and_word_order() -> None:
    assert _title_match_score("Snow Crash", "snow crash") == 1.0
    assert _title_match_score("The Hobbit", "Hobbit, The") == 1.0
    assert _title_match_score("Dune", "") == 0.0


def test_subset_title_with_matching_author_stays_below_auto_accept() -> None:
    signals = IdentificationInput(
        title_hints=["Foundation"],
        author_hints=["Isaac Asimov"],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )
    payloads = [
        {"title": "Foundation and Earth", "author_name": ["Isaac Asimov"]},
        {"title": "Foundation", "author_name": ["Isaac Asimov"]},
    ]
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [_stub_provider_from_candidates(payloads)()],
    ):
        candidates = resolve_candidates(signals)

    assert [candidate.title for candidate in candidates] == ["Foundation", "Foundation and Earth"]
    assert candidates[1].score < AUTO_ACCEPT_SCORE
    assert candidates[0].score > candidates[1].score


def _signals(
    *,
    title: str,
    author: str | None,
    isbns: list[str] | None = None,
    language: str | None = None,
    publication_year: int | None = None,
) -> IdentificationInput:
    return IdentificationInput(
        title_hints=[title],
        author_hints=[author] if author else [],
        isbns=isbns or [],
        folder_hints=[],
        language=language,
        publisher=None,
        description=None,
        publication_year=publication_year,
    )


def _resolve(
    signals: IdentificationInput, payloads: list[dict[str, Any]]
) -> list[IdentificationCandidate]:
    with patch(
        "ebook_server.core.identification._provider_instances",
        lambda: [_stub_provider_from_candidates(payloads)()],
    ):
        return resolve_candidates(signals)


def _rule_evidence(candidate: IdentificationCandidate) -> dict[str, Any]:
    evidence = candidate.raw[STRONG_MATCH_EVIDENCE_KEY]
    assert isinstance(evidence, dict)
    return evidence


def test_pre_isbn_book_with_exact_title_and_author_auto_accepts() -> None:
    # Regression for #9: title + author alone top out at 0.85, below auto-accept.
    candidates = _resolve(
        _signals(title="The Hobbit", author="J.R.R. Tolkien"),
        [{"title": "The Hobbit", "author_name": ["J.R.R. Tolkien"], "first_publish_year": 1937}],
    )

    top = candidates[0]
    assert top.score >= AUTO_ACCEPT_SCORE
    evidence = _rule_evidence(top)
    assert evidence["rule"] == "exact_title_strong_author"
    assert evidence["applied"] is True
    assert evidence["blocked_by"] == []
    assert evidence["base_score"] == pytest.approx(0.85)
    assert evidence["title_score"] == 1.0
    assert evidence["author_score"] == 1.0


def test_author_dash_title_filename_auto_accepts_despite_split_author_hints() -> None:
    # "Author - Title" puts both halves into the author hints, which halves the
    # list-level author score; the rule scores each hint on its own.
    signals = build_identification_input(
        source_path="/incoming/Isaac Asimov - Foundation.txt",
        original_filename="Isaac Asimov - Foundation.txt",
        extracted_metadata={},
    )
    candidates = _resolve(signals, [{"title": "Foundation", "author_name": ["Isaac Asimov"]}])

    assert candidates[0].score >= AUTO_ACCEPT_SCORE
    evidence = _rule_evidence(candidates[0])
    assert evidence["title_hint"] == "Foundation"
    assert evidence["author_hint"] == "Isaac Asimov"


@pytest.mark.parametrize(
    ("filename", "payload"),
    [
        ("Foundation.txt", {"title": "Foundation and Earth", "author_name": ["Isaac Asimov"]}),
        ("Dune - House Harkonnen.txt", {"title": "Dune", "author_name": ["Frank Herbert"]}),
        ("Gold - 03 - Frustration.txt", {"title": "Gold", "author_name": ["Isaac Asimov"]}),
    ],
)
def test_partial_title_or_missing_author_does_not_auto_accept(
    filename: str, payload: dict[str, Any]
) -> None:
    # The wrong matches from the #9 sample re-import must still go to review.
    signals = build_identification_input(
        source_path=f"/incoming/{filename}",
        original_filename=filename,
        extracted_metadata={},
    )
    candidates = _resolve(signals, [payload])

    assert candidates[0].score < AUTO_ACCEPT_SCORE
    assert _rule_evidence(candidates[0])["applied"] is False


def test_subset_title_with_matching_author_is_blocked_as_not_exact() -> None:
    candidates = _resolve(
        _signals(title="Foundation", author="Isaac Asimov"),
        [{"title": "Foundation and Earth", "author_name": ["Isaac Asimov"]}],
    )

    assert candidates[0].score < AUTO_ACCEPT_SCORE
    assert _rule_evidence(candidates[0])["blocked_by"] == ["title_not_exact"]


def test_title_match_without_author_match_does_not_auto_accept() -> None:
    candidates = _resolve(
        _signals(title="Dune", author="Brian Herbert"),
        [{"title": "Dune", "author_name": ["Frank Herbert"]}],
    )

    assert candidates[0].score < AUTO_ACCEPT_SCORE
    assert "author_not_strong" in _rule_evidence(candidates[0])["blocked_by"]


def test_near_tied_candidates_do_not_auto_accept() -> None:
    candidates = _resolve(
        _signals(title="Poems", author="Emily Dickinson"),
        [
            {"key": "/works/OL1W", "title": "Poems", "author_name": ["Emily Dickinson"]},
            {"key": "/works/OL2W", "title": "Poems", "author_name": ["Emily Dickinson"]},
        ],
    )

    assert all(candidate.score < AUTO_ACCEPT_SCORE for candidate in candidates)
    evidence = _rule_evidence(candidates[0])
    assert evidence["applied"] is False
    assert evidence["blocked_by"] == ["near_tied_candidate"]
    assert evidence["runner_up_score"] == pytest.approx(0.85)


def test_clear_winner_over_weaker_candidate_auto_accepts() -> None:
    candidates = _resolve(
        _signals(title="Foundation", author="Isaac Asimov"),
        [
            {"title": "Foundation and Earth", "author_name": ["Isaac Asimov"]},
            {"title": "Foundation", "author_name": ["Isaac Asimov"]},
        ],
    )

    assert candidates[0].title == "Foundation"
    assert candidates[0].score >= AUTO_ACCEPT_SCORE
    assert candidates[1].score < AUTO_ACCEPT_SCORE


def test_conflicting_isbn_blocks_strong_match() -> None:
    candidates = _resolve(
        _signals(title="Nineteen Eighty-Four", author="George Orwell", isbns=["9780306406157"]),
        [
            {
                "title": "Nineteen Eighty-Four",
                "author_name": ["George Orwell"],
                "isbn": ["9780451524935"],
            }
        ],
    )

    assert candidates[0].score < AUTO_ACCEPT_SCORE
    assert _rule_evidence(candidates[0])["blocked_by"] == ["conflicting_isbn"]


def test_unconfirmed_file_isbn_blocks_strong_match() -> None:
    candidates = _resolve(
        _signals(title="Nineteen Eighty-Four", author="George Orwell", isbns=["9780306406157"]),
        [{"title": "Nineteen Eighty-Four", "author_name": ["George Orwell"]}],
    )

    assert candidates[0].score < AUTO_ACCEPT_SCORE
    assert _rule_evidence(candidates[0])["blocked_by"] == ["file_isbn_unconfirmed"]


def test_conflicting_language_blocks_strong_match() -> None:
    candidates = _resolve(
        _signals(title="Der Process", author="Franz Kafka", language="de"),
        [{"title": "Der Process", "author_name": ["Franz Kafka"], "language": ["eng"]}],
    )

    assert candidates[0].score < AUTO_ACCEPT_SCORE
    assert _rule_evidence(candidates[0])["blocked_by"] == ["conflicting_language"]


def test_equivalent_language_codes_are_not_a_conflict() -> None:
    candidates = _resolve(
        _signals(title="Der Process", author="Franz Kafka", language="de-DE"),
        [{"title": "Der Process", "author_name": ["Franz Kafka"], "language": ["eng", "ger"]}],
    )

    assert candidates[0].score >= AUTO_ACCEPT_SCORE


def test_file_dated_before_first_publication_blocks_strong_match() -> None:
    candidates = _resolve(
        _signals(title="Dune", author="Frank Herbert", publication_year=1920),
        [{"title": "Dune", "author_name": ["Frank Herbert"], "first_publish_year": 1965}],
    )

    assert candidates[0].score < AUTO_ACCEPT_SCORE
    assert _rule_evidence(candidates[0])["blocked_by"] == ["publication_year_mismatch"]


def test_later_reprint_year_is_not_a_conflict() -> None:
    candidates = _resolve(
        _signals(title="Dune", author="Frank Herbert", publication_year=2005),
        [{"title": "Dune", "author_name": ["Frank Herbert"], "first_publish_year": 1965}],
    )

    assert candidates[0].score >= AUTO_ACCEPT_SCORE


def test_strong_match_rule_can_be_disabled() -> None:
    with override_settings(BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_ENABLED=False):
        candidates = _resolve(
            _signals(title="The Hobbit", author="J.R.R. Tolkien"),
            [{"title": "The Hobbit", "author_name": ["J.R.R. Tolkien"]}],
        )

    assert candidates[0].score == pytest.approx(0.85)
    assert STRONG_MATCH_EVIDENCE_KEY not in candidates[0].raw


def test_build_identification_input_reads_publication_year() -> None:
    signals = build_identification_input(
        source_path="/incoming/book.epub",
        original_filename="book.epub",
        extracted_metadata={"title": "Book", "date": "1951-06-01"},
    )

    assert signals.publication_year == 1951
