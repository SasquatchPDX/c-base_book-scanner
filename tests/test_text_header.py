"""Title/author hints from the first lines of plain-text books."""

from __future__ import annotations

from pathlib import Path

import pytest

from ebook_server.core import intake
from ebook_server.core.identification import TEXT_HEADER_EVIDENCE_KEY, build_identification_input
from ebook_server.core.text_header import parse_text_header

BODY = (
    "It was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\n"
)


@pytest.mark.parametrize(
    ("stem", "header", "title", "authors"),
    [
        (
            "Sitters, The - Unknown",
            "Title: The Sitters\nAuthor: Clifford D. Simak\nOriginal copyright year: 1962\n",
            "The Sitters",
            ["Clifford D. Simak"],
        ),
        (
            "Alchemist, The - Unknown",
            "The Alchemist by H.P. Lovecraft\nThe Alchemist\nby H.P. Lovecraft\n1908\n",
            "The Alchemist",
            ["H.P. Lovecraft"],
        ),
        (
            "Crystal Singer - Unknown",
            "Crystal Singer\nby:\nAnne McCaffrey\ncopyright 1982\n",
            "Crystal Singer",
            ["Anne McCaffrey"],
        ),
        (
            "Count Zero - Unknown",
            "COUNT ZERO\nWilliam Gibson\n1986\nCount Zero\n",
            "Count Zero",
            ["William Gibson"],
        ),
        (
            "All You Zombies - Unknown",
            "Robert A. Heinlein. All you zombies\n2217 Time Zone V (EST)\n",
            "All You Zombies",
            ["Robert A. Heinlein"],
        ),
        (
            "Barnaby Rudge - Unknown",
            "The Project Gutenberg Etext of Barnaby Rudge, by Charles Dickens\n"
            "#25 in our series by Charles Dickens\n",
            "Barnaby Rudge",
            ["Charles Dickens"],
        ),
        (
            "Cricket on the Hearth, The - Unknown",
            "****The Project Gutenberg Etext of The Cricket on the Hearth****\n"
            "#10 in our series by Charles Dickens\n",
            "The Cricket on the Hearth",
            ["Charles Dickens"],
        ),
        (
            "Ringworld - Unknown",
            "RINGWORLD\n  by Larry Niven\n  (c) 1970 by Larry Niven\n",
            "Ringworld",
            ["Larry Niven"],
        ),
        (
            "The Lost Worlds of 2001 - Arthur Charles Clarke",
            "BLAST OFF!\nTHE LOST WORLDS OF 2001\nARTHUR C. CLARKE\nA SIGNET BOOK from\n",
            "The Lost Worlds Of 2001",
            ["Arthur C. Clarke"],
        ),
        (
            "Ende, Michael - Jim Knopf und Lukas der Lokomotivfuehrer",
            "Michael Ende\nJim Knopf und Lukas der Lokomotivführer\n",
            "Jim Knopf und Lukas der Lokomotivführer",
            ["Michael Ende"],
        ),
        (
            "Red Star, Winter Orbit ",
            "by Bruce Sterling and William Gibson\n",
            None,
            ["Bruce Sterling", "William Gibson"],
        ),
        ("oliver twist", "CHARLES DICKENS'S\nOLIVER TWIST\n", "Oliver Twist", ["Charles Dickens"]),
    ],
)
def test_header_patterns(stem: str, header: str, title: str | None, authors: list[str]) -> None:
    result = parse_text_header(header + BODY * 3, stem)
    assert result is not None
    assert result["title"] == title
    assert result["authors"] == authors
    assert result["rules"]
    assert result["lines"][0] == header.splitlines()[0].strip()


@pytest.mark.parametrize(
    ("stem", "header", "rejected"),
    [
        (
            "Final Gentleman - Unknown",
            "Title: Final Gentleman\nAuthor: Clifford D. Simak\n"
            "Source: scanned, proofread in MS Word by Anada Sucka\n",
            "Anada Sucka",
        ),
        (
            "Gold - 03 - Frustration",
            "Gold, The Final Science Fiction Collection\nIsaac Asimov\nTyped by Bateau\n",
            "Bateau",
        ),
        (
            "Dragonlance Tales",
            "Tales I\nVolume 3\nLOVE and WAR\nEdited by MARGARET WEIS AND TRACY HICKMAN\n",
            "Margaret Weis",
        ),
        (
            "From Beyond",
            "From Beyond\nHorrible was the change by driving me from his laboratory\n",
            "driving me from his laboratory",
        ),
        (
            "Slave Girl Of Gor",
            "I did not understand this\nWhy had I come\n",
            "I did not understand this",
        ),
        ("Dune - House Harkonnen", "Dune: House Harkonnen\nPrologue\n", "House Harkonnen"),
    ],
)
def test_not_authors(stem: str, header: str, rejected: str) -> None:
    result = parse_text_header(header + BODY, stem) or {"authors": []}
    assert rejected not in result["authors"]


def test_story_without_header_and_path_lines() -> None:
    assert (
        parse_text_header(
            "   Once upon a time . . . a peasant lost all his crops.\n", "Amin and Eggs - Unknown"
        )
        is None
    )
    result = parse_text_header("[obi/Doyle/valley.of.fear.txt]\nVALLEY OF FEAR\n", "Valley Of Fear")
    assert result is not None and result["title"] == "Valley Of Fear"
    assert parse_text_header("", "x") is None


def test_only_first_lines_are_used() -> None:
    late = (
        "\n".join(f"Line number {i} of the story goes on." for i in range(20))
        + "\nby Hidden Author\n"
    )
    assert parse_text_header(late, "Something") is None


def test_header_hints_feed_identification_signals() -> None:
    signals = build_identification_input(
        source_path="/x/Unknown/Count Zero - Unknown.txt",
        original_filename="Count Zero - Unknown.txt",
        extracted_metadata={
            "title": "Count Zero - Unknown",
            TEXT_HEADER_EVIDENCE_KEY: {"title": "Count Zero", "authors": ["William Gibson"]},
        },
    )
    assert signals.author_hints == ["William Gibson"]  # replaces "Count Zero"/"Unknown" guesses
    assert signals.title_hints[:2] == ["Count Zero - Unknown", "Count Zero"]


def test_without_header_signals_are_unchanged() -> None:
    signals = build_identification_input(
        source_path="/x/Emma - Jane Austen.txt",
        original_filename="Emma - Jane Austen.txt",
        extracted_metadata={"title": "Emma - Jane Austen"},
    )
    assert signals.author_hints == ["Emma", "Jane Austen"]


def test_text_intake_records_header_evidence(tmp_path: Path) -> None:
    path = tmp_path / "Crystal Singer - Unknown.txt"
    path.write_text("Crystal Singer\nby:\nAnne McCaffrey\ncopyright 1982\n" + BODY * 5)
    metadata = intake.extract_metadata(str(path), "txt", original_filename=path.name)
    header = metadata[TEXT_HEADER_EVIDENCE_KEY]
    assert header["authors"] == ["Anne McCaffrey"]
    assert header["title"] == "Crystal Singer"
    assert metadata["title"] == "Crystal Singer - Unknown"  # filename title stays as it was
