"""Title/author hints from the first lines of plain-text books (#43)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ebook_server.core import intake
from ebook_server.core.identification import (
    TEXT_HEADER_EVIDENCE_KEY,
    IdentificationInput,
    build_identification_input,
)
from ebook_server.core.text_header import parse_text_header

BODY = (
    "It was a dark and stormy night; the rain fell in torrents, except at occasional intervals.\n"
)


def _signals(filename: str, text: str) -> IdentificationInput:
    metadata: dict[str, object] = {"title": Path(filename).stem}
    header = parse_text_header(text, Path(filename).stem)
    if header is not None:
        metadata[TEXT_HEADER_EVIDENCE_KEY] = header
    return build_identification_input(
        source_path=f"/library/incoming/{filename}",
        original_filename=filename,
        extracted_metadata=metadata,
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
            "Red Star, Winter Orbit ",
            "by Bruce Sterling and William Gibson\n",
            None,
            ["Bruce Sterling", "William Gibson"],
        ),
    ],
)
def test_explicit_header_rules(
    stem: str, header: str, title: str | None, authors: list[str]
) -> None:
    result = parse_text_header(header + BODY * 3, stem)
    assert result is not None
    assert result["title"] == title
    assert result["authors"] == authors
    assert result["likely_authors"] == []
    assert result["rules"]


@pytest.mark.parametrize(
    ("stem", "header", "title", "likely"),
    [
        (
            "Count Zero - Unknown",
            "COUNT ZERO\nWilliam Gibson\n1986\nCount Zero\n",
            "Count Zero",
            ["William Gibson"],
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
        ("oliver twist", "CHARLES DICKENS'S\nOLIVER TWIST\n", "Oliver Twist", ["Charles Dickens"]),
    ],
)
def test_name_lines_beside_title_are_only_likely_authors(
    stem: str, header: str, title: str, likely: list[str]
) -> None:
    result = parse_text_header(header + BODY * 3, stem)
    assert result is not None
    assert result["title"] == title
    assert result["authors"] == []
    assert result["likely_authors"] == likely


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
        # Review of #5: header lines that became authors.
        ("Weber, David - On Basilisk Station", "Baen Books\nOn Basilisk Station\n", "Baen Books"),
        (
            "Robinson, Kim Stanley - Red Mars",
            "SCIENCE FICTION\nRed Mars\nKim Stanley Robinson\n",
            "Science Fiction",
        ),
        ("Robinson, Kim Stanley - Red Mars", "SCIENCE FICTION\nRed Mars\n", "Red Mars"),
        ("Hamlet", "HAMLET\nDramatis Personae\n", "Dramatis Personae"),
        ("Manhattan Transfer", "Manhattan Transfer\nNew York\n", "New York"),
        ("Dune", "DUNE\nCover designed by Jane Doe\n", "Jane Doe"),
    ],
)
def test_not_authors(stem: str, header: str, rejected: str) -> None:
    result = parse_text_header(header + BODY, stem) or {"authors": [], "likely_authors": []}
    assert rejected not in result["authors"] + result["likely_authors"]


@pytest.mark.parametrize(
    ("filename", "header", "first_author", "absent"),
    [
        ("Weber, David - On Basilisk Station.txt", "Baen Books\n", "Weber, David", "Baen Books"),
        (
            "Weber, David - On Basilisk Station.txt",
            "Baen Books\nOn Basilisk Station\nDavid Weber\n",
            "Weber, David",
            "Baen Books",
        ),
        (
            "Robinson, Kim Stanley - Red Mars.txt",
            "SCIENCE FICTION\nRed Mars\nKim Stanley Robinson\n",
            "Robinson, Kim Stanley",
            "Science Fiction",
        ),
        (
            "Robinson, Kim Stanley - Red Mars.txt",
            "SCIENCE FICTION\nRed Mars\n",
            "Robinson, Kim Stanley",
            "Red Mars",
        ),
    ],
)
def test_review_cases_keep_filename_author_first(
    filename: str, header: str, first_author: str, absent: str
) -> None:
    signals = _signals(filename, header + BODY)
    assert signals.best_author() == first_author
    assert absent not in signals.author_hints


@pytest.mark.parametrize(
    ("filename", "header"),
    [
        ("Hamlet.txt", "HAMLET\nDramatis Personae\n"),
        ("Manhattan Transfer.txt", "Manhattan Transfer\nNew York\n"),
        ("Dune.txt", "DUNE\nCover designed by Jane Doe\n"),
    ],
)
def test_review_cases_without_filename_author_yield_no_author(filename: str, header: str) -> None:
    assert _signals(filename, header + BODY).author_hints == []


def test_title_match_needs_whole_words_not_a_substring() -> None:
    assert parse_text_header("The city was quiet that night.\n" + BODY, "Night") is None
    assert parse_text_header("Knightfall descends on Gotham\n", "Night Fall") is None
    result = parse_text_header("NIGHT\n" + BODY, "Night")
    assert result is not None and result["title"] == "Night"


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


def test_explicit_header_authors_replace_filename_guesses() -> None:
    signals = _signals("Count Zero - Unknown.txt", "Author: William Gibson\n" + BODY)
    assert signals.author_hints == ["William Gibson"]


def test_likely_authors_follow_filename_authors() -> None:
    signals = _signals("Count Zero - Unknown.txt", "COUNT ZERO\nWilliam Gibson\n" + BODY)
    # "Count Zero" is the title and "Unknown" a placeholder, so neither is an author.
    assert signals.author_hints == ["William Gibson"]
    assert signals.title_hints[:2] == ["Count Zero - Unknown", "Count Zero"]

    signals = _signals(
        "Weber, David - On Basilisk Station.txt", "On Basilisk Station\nDavid Weber\n" + BODY
    )
    assert signals.author_hints == ["Weber, David"]  # same person, not repeated

    signals = _signals("Emma - Jane Austen.txt", "EMMA\nAnne Elliot\n" + BODY)
    assert signals.author_hints == ["Jane Austen", "Anne Elliot"]


def test_without_header_signals_are_unchanged() -> None:
    signals = build_identification_input(
        source_path="/x/Emma - Jane Austen.txt",
        original_filename="Emma - Jane Austen.txt",
        extracted_metadata={"title": "Emma - Jane Austen"},
    )
    assert signals.author_hints == ["Emma", "Jane Austen"]


def test_text_intake_records_hints_but_not_book_text(tmp_path: Path) -> None:
    path = tmp_path / "Crystal Singer - Unknown.txt"
    path.write_text(
        "Crystal Singer\nby:\nAnne McCaffrey\ncopyright 1982\n" + BODY * 5, encoding="utf-8"
    )
    metadata = intake.extract_metadata(str(path), "txt", original_filename=path.name)
    header = metadata[TEXT_HEADER_EVIDENCE_KEY]
    assert set(header) == {"title", "authors", "likely_authors", "rules"}
    assert header["authors"] == ["Anne McCaffrey"]
    assert header["title"] == "Crystal Singer"
    assert {rule["rule"] for rule in header["rules"]} == {"line matches file name", "by line"}
    serialized = json.dumps(metadata)
    assert "copyright 1982" not in serialized
    assert "stormy" not in serialized
    assert metadata["title"] == "Crystal Singer - Unknown"  # filename title stays as it was
