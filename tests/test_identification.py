from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ebook_server.core.identification import (
    IdentificationCandidate,
    IdentificationInput,
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


def test_parse_filename_signals_title_first_author_last() -> None:
    title_hints, author_hints = parse_filename_signals("Snow Crash - Neal Stephenson.epub")

    assert author_hints == ["Neal Stephenson"]
    assert title_hints[0] == "Snow Crash"


def test_parse_filename_signals_does_not_dilute_author_with_the_title_half() -> None:
    """Regression for #15: both split halves used to land in author_hints too, so an
    unrelated title fragment (e.g. a series/volume prefix) diluted author scoring even
    when the real author was also present."""
    title_hints, author_hints = parse_filename_signals(
        "Cibola Burn (The Expanse) - James S. A. Corey.mobi",
    )

    assert author_hints == ["James S. A. Corey"]
    assert "Cibola Burn (The Expanse)" in title_hints
    assert "Cibola Burn" in title_hints  # trailing "(Series)" annotation also offered alone


def test_parse_filename_signals_series_volume_prefix_does_not_poison_primary_title() -> None:
    """Regression for #15: "Expanse 03 - Abaddon's Gate - James S. A. Corey" previously
    searched the raw, unsplit filename as "the" title and the wrong half as "the" author.
    Live-tested against the real Open Library API: leaving the series/volume prefix
    attached to the primary title hint can return zero results outright, not just a
    lower score, so the clean last segment has to be hints[0], not just "a" hint."""
    title_hints, author_hints = parse_filename_signals(
        "Expanse 03 - Abaddon's Gate - James S. A. Corey.mobi",
    )

    assert author_hints == ["James S. A. Corey"]
    assert title_hints[0] == "Abaddon's Gate"
    assert "Expanse 03 - Abaddon's Gate" in title_hints  # kept as a fallback hint


def test_parse_filename_signals_un_inverts_library_sort_titles() -> None:
    """Regression for #15 (reported via GitHub comment): "Title, The" is a common library
    sorting convention, not part of the real title."""
    title_hints, author_hints = parse_filename_signals(
        "Butcher of Anderson Station, The - James S. A. Corey.mobi",
    )

    assert author_hints == ["James S. A. Corey"]
    assert "Butcher of Anderson Station, The" in title_hints
    assert "The Butcher of Anderson Station" in title_hints


def test_parse_filename_signals_last_first_author() -> None:
    title_hints, author_hints = parse_filename_signals("Phylogenesis - Foster, Alan Dean.epub")

    assert author_hints == ["Alan Dean Foster"]
    assert title_hints[0] == "Phylogenesis"


def test_parse_filename_signals_co_authors_with_ampersand() -> None:
    title_hints, author_hints = parse_filename_signals(
        "Hells Gate - David Weber & Linda Evans.epub",
    )

    assert author_hints == ["David Weber & Linda Evans"]
    assert title_hints[0] == "Hells Gate"


def test_parse_filename_signals_does_not_guess_a_role_other_than_author() -> None:
    """ "edited By" names an editor, not an author - must not end up as author_hints, and
    must not falsely make the whole right-hand side "a plausible author list" either."""
    title_hints, author_hints = parse_filename_signals(
        "Odyssey - Keith Laumer & edited By Eric Flint.epub",
    )

    assert author_hints == []
    assert title_hints == ["Odyssey - Keith Laumer & edited By Eric Flint"]


@pytest.mark.parametrize(
    "filename",
    [
        "Much Ado About Nothing - Unknown.txt",
        "As You Like It - Unknown.txt",
        "I Am Legend - Unknown.txt",
        "Brave New World - Unknown.txt",
    ],
)
def test_parse_filename_signals_unknown_placeholder_is_not_an_author(filename: str) -> None:
    """Regression found via a 1,577-file real-world batch check (#15): "Unknown" isn't
    name-shaped, but rejecting it used to fall through to checking the *other* end - and
    several short, plain-English titles (e.g. 4 capitalised words, no stray words) happen
    to pass the same permissive shape check a real name would, flipping title and author
    entirely. "Unknown"/"anonymous" etc. must stop the lookup, not redirect it."""
    title_hints, author_hints = parse_filename_signals(filename)

    assert author_hints == []
    assert title_hints[0] == filename.rsplit(" - Unknown", 1)[0]


def test_parse_filename_signals_explicit_last_first_wins_over_a_title_shaped_last_segment() -> None:
    """Regression found via the same batch check: an explicit "Last, First" form is a much
    stronger signal than the generic name-shape check, and has to win even when it's on
    the *first* segment and the last segment merely happens to look name-shaped too (a
    short title can). Real example from the batch: "Absolute Freunde" (the title) was
    being picked as the author, before "Carre, John Le" was ever considered."""
    title_hints, author_hints = parse_filename_signals("Carre, John Le - Absolute Freunde.rtf")

    assert author_hints == ["John Le Carre"]
    assert title_hints[0] == "Absolute Freunde"


def test_parse_filename_signals_ambiguous_split_guesses_no_author() -> None:
    """ "Gold - 03 - Frustration": "Frustration" is one story within the anthology "Gold",
    not an author - neither split half looks name-shaped, so this must stay unsplit
    rather than injecting a wrong author or a too-generic primary title (regression
    guard: "Gold" alone is an extremely common word at the real Open Library API and
    returns unrelated books in the top results)."""
    title_hints, author_hints = parse_filename_signals("Gold - 03 - Frustration.txt")

    assert author_hints == []
    assert title_hints == ["Gold - 03 - Frustration"]


def test_parse_filename_signals_no_separator_is_unaffected() -> None:
    title_hints, author_hints = parse_filename_signals("The Hobbit.txt")

    assert author_hints == []
    assert title_hints == ["The Hobbit"]


def test_build_identification_input_deprioritises_stem_fallback_title() -> None:
    """Regression for #15: when a format extractor has nothing real (e.g. MOBI, see
    intake.extract_metadata), it falls back to the raw filename stem as "title". That
    must not out-rank the filename parser's own, better split title hint."""
    payload = build_identification_input(
        source_path="/incoming/Cibola Burn (The Expanse) - James S. A. Corey.mobi",
        original_filename="Cibola Burn (The Expanse) - James S. A. Corey.mobi",
        extracted_metadata={"title": "Cibola Burn (The Expanse) - James S. A. Corey"},
    )

    assert payload.title_hints[0] == "Cibola Burn (The Expanse)"
    assert payload.author_hints == ["James S. A. Corey"]


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
