from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ebook_server.core.identification import (
    AUTHOR_VARIANT_MATCH_SCORE,
    AUTO_ACCEPT_SCORE,
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

    assert author_hints == ["Jules Verne"]
    assert title_hints[0] == "Around the World in 80 Days"


# Review regressions on PR #34: these used to give both halves as hints and must keep
# doing so (never swapped, never dropped, never left unsplit).
@pytest.mark.parametrize(
    ("filename", "left", "right"),
    [
        ("Stephen King - Pet Sematary.epub", "Stephen King", "Pet Sematary"),
        ("Frank Herbert - Dune Messiah.epub", "Frank Herbert", "Dune Messiah"),
        ("Neil Gaiman - American Gods.epub", "Neil Gaiman", "American Gods"),
        ("Asimov - Foundation.epub", "Asimov", "Foundation"),
        ("Foundation - Asimov.epub", "Foundation", "Asimov"),
        ("tolkien - the hobbit.txt", "tolkien", "the hobbit"),
        ("Asimov- Foundation.txt", "Asimov", "Foundation"),
        ("Foundation -Asimov.txt", "Foundation", "Asimov"),
    ],
)
def test_parse_filename_signals_keeps_both_orientations_when_ambiguous(
    filename: str,
    left: str,
    right: str,
) -> None:
    title_hints, author_hints = parse_filename_signals(filename)

    assert set(title_hints[:2]) == {left, right}
    # "Title - Author" is the more common convention, so the right end leads.
    assert author_hints == [right, left]


def test_parse_filename_signals_commits_when_only_one_end_is_a_name() -> None:
    title_hints, author_hints = parse_filename_signals("Empire From the Ashes - David Weber.epub")

    assert author_hints == ["David Weber"]
    assert title_hints[0] == "Empire From the Ashes"


@pytest.mark.parametrize(
    ("filename", "author", "title"),
    [
        ("Carre, John Le - Absolute Freunde.rtf", "John Le Carre", "Absolute Freunde"),
        ("Phylogenesis - Foster, Alan Dean.epub", "Alan Dean Foster", "Phylogenesis"),
        # "Last, First" beats a name-shaped other end
        ("Stephen King - Foster, Alan Dean.epub", "Alan Dean Foster", "Stephen King"),
    ],
)
def test_parse_filename_signals_last_first_author_at_either_end(
    filename: str,
    author: str,
    title: str,
) -> None:
    title_hints, author_hints = parse_filename_signals(filename)

    assert author_hints == [author]
    assert title_hints[0] == title


def test_parse_filename_signals_series_prefix_is_only_a_fallback_title() -> None:
    title_hints, author_hints = parse_filename_signals(
        "Donaldson, Stephen R. - Covenant 01 - Der Fluch des Veraechters.pdf",
    )

    assert author_hints == ["Stephen R. Donaldson"]
    assert title_hints[0] == "Der Fluch des Veraechters"
    assert "Covenant 01 - Der Fluch des Veraechters" in title_hints


def test_parse_filename_signals_series_title_author_keeps_author_and_clean_title() -> None:
    title_hints, author_hints = parse_filename_signals(
        "Expanse 03 - Abaddon's Gate - James S. A. Corey.mobi",
    )

    assert author_hints[0] == "James S. A. Corey"
    assert title_hints[0] == "Abaddon's Gate"
    assert "Expanse 03 - Abaddon's Gate" in title_hints


@pytest.mark.parametrize(
    "filename",
    ["Much Ado About Nothing - Unknown.txt", "Brave New World - Unknown.txt"],
)
def test_parse_filename_signals_placeholder_author_is_not_an_author(filename: str) -> None:
    title_hints, author_hints = parse_filename_signals(filename)

    assert author_hints == []
    assert title_hints[0] == filename.rsplit(" - Unknown", 1)[0]


def test_parse_filename_signals_un_inverts_library_sort_titles() -> None:
    title_hints, author_hints = parse_filename_signals(
        "Butcher of Anderson Station, The - James S. A. Corey.mobi",
    )

    assert author_hints == ["James S. A. Corey"]
    assert "The Butcher of Anderson Station" in title_hints


def test_parse_filename_signals_splits_co_author_lists() -> None:
    _, author_hints = parse_filename_signals("Crown of Slaves - David Weber & Eric Flint.epub")

    assert author_hints == ["David Weber", "Eric Flint"]


def test_parse_filename_signals_no_separator_is_unaffected() -> None:
    assert parse_filename_signals("The Hobbit.txt") == (["The Hobbit"], [])
    assert parse_filename_signals("For Love of Mother-Not.epub") == (
        ["For Love of Mother-Not"],
        [],
    )


def test_build_identification_input_deprioritises_stem_fallback_title() -> None:
    """Extractors with nothing to say return the raw stem as "title" (MOBI, ...); that
    must not out-rank the parsed filename title."""
    payload = build_identification_input(
        source_path="/incoming/Cibola Burn (The Expanse) - James S. A. Corey.mobi",
        original_filename="Cibola Burn (The Expanse) - James S. A. Corey.mobi",
        extracted_metadata={"title": "Cibola Burn (The Expanse) - James S. A. Corey"},
    )

    assert payload.title_hints[0] == "Cibola Burn (The Expanse)"
    assert payload.author_hints[0] == "James S. A. Corey"


def test_ambiguous_filename_hints_score_like_a_committed_orientation() -> None:
    """Both-orientation hints must not cost the right candidate any author score (#15)."""
    ambiguous = build_identification_input(
        source_path="/incoming/Stephen King - Pet Sematary.epub",
        original_filename="Stephen King - Pet Sematary.epub",
        extracted_metadata={},
    )
    committed = IdentificationInput(
        title_hints=["Pet Sematary"],
        author_hints=["Stephen King"],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )
    payload = {"title": "Pet Sematary", "author_name": ["Stephen King"]}
    scores = []
    for signals in (ambiguous, committed):
        with patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [_stub_provider_from_candidates([payload])()],
        ):
            scores.append(resolve_candidates(signals)[0].score)

    assert scores[0] == pytest.approx(scores[1])
    assert scores[0] == pytest.approx(0.55 + 0.3)  # full title and full author credit


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
    assert _author_match_score([], ["Andy Weir"]) == 0.0


def test_author_match_score_treats_hints_as_alternatives() -> None:
    # #15: an ambiguous filename yields both halves as author hints; the title half
    # must not halve the score of the real author.
    assert _author_match_score(["The Martian", "Andy Weir"], ["Andy Weir"]) == 1.0
    assert _author_match_score(["Pet Sematary", "Stephen King"], ["Stephen King"]) == 1.0
    assert (
        _author_match_score(["Snow Crash", "Iain M. Banks"], ["Iain Banks"])
        == AUTHOR_VARIANT_MATCH_SCORE
    )
    # A shared surname alone still does not count (#67).
    assert _author_match_score(["Dune", "Brian Herbert"], ["Frank Herbert"]) == 0.0


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
