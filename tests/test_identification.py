from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

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


def test_conflicting_metadata_keeps_filename_signal() -> None:
    payload = build_identification_input(
        source_path="/incoming/series/The%20War%20of%20The%20Worlds.pdf",
        original_filename="The%20War%20of%20The%20Worlds.pdf",
        extracted_metadata={"title": "Wrong Title", "authors": ["Confident Author"]},
    )

    assert payload.title_hints[0] == "Wrong Title"
    assert payload.title_hints[1] == "The%20War%20of%20The%20Worlds"
