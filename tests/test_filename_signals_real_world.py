"""Regression test for filename signal parsing against real-world filenames (#15).

Every filename below is taken verbatim from the published real-world library listings in
``tests/fixtures/real_world_samples/`` and hand-labelled with the book's actual title and
author. The expectations are deliberately about *outcomes*, not exact hint lists:

* ``AUTHOR_LAST`` / ``AUTHOR_FIRST``: the parser should commit to one orientation, so the
  primary title hint is the title and the author hints are exactly the author(s).
* ``BOTH``: the filename alone cannot say which end is the author ("Stephen King - Pet
  Sematary" vs "Snow Crash - Neal Stephenson"), so both orientations must be kept: the
  real title is one of the first two title hints and the real author is an author hint.
  Never committing to the wrong orientation is the point of this category. An empty
  author list means the filename names no author at all ("Ender's Saga 1 - Ender's
  Game"); both ends must still be kept rather than one guessed as the author.
* ``NO_AUTHOR``: placeholder authors ("- Unknown") and unsplit names give no author hint.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from ebook_server.core.identification import parse_filename_signals

SAMPLES_DIR = Path(__file__).parent / "fixtures" / "real_world_samples"

AUTHOR_LAST = "author_last"
AUTHOR_FIRST = "author_first"
BOTH = "both"
NO_AUTHOR = "no_author"

# (filename, orientation, real title, real author(s))
CASES: list[tuple[str, str, str, list[str]]] = [
    # "Title - First Last" with an unambiguous title side
    (
        "Empire From the Ashes - David Weber.epub",
        AUTHOR_LAST,
        "Empire From the Ashes",
        ["David Weber"],
    ),
    (
        "For Love of Mother-Not - Alan Dean Foster.epub",
        AUTHOR_LAST,
        "For Love of Mother-Not",
        ["Alan Dean Foster"],
    ),
    (
        "Lucky Starr and the Big Sun of Mercury - Isaac Asimov.epub",
        AUTHOR_LAST,
        "Lucky Starr and the Big Sun of Mercury",
        ["Isaac Asimov"],
    ),
    ("More Than Honor - David Weber.epub", AUTHOR_LAST, "More Than Honor", ["David Weber"]),
    (
        "Der Spender Planet - Orson Scott Card.pdf",
        AUTHOR_LAST,
        "Der Spender Planet",
        ["Orson Scott Card"],
    ),
    (
        "Das Herz der Schlange - Iwan Antonowitsch Jefremow.pdf",
        AUTHOR_LAST,
        "Das Herz der Schlange",
        ["Iwan Antonowitsch Jefremow"],
    ),
    (
        "Insel der Freibeuter - Alberto Vazquez Figueroa.pdf",
        AUTHOR_LAST,
        "Insel der Freibeuter",
        ["Alberto Vazquez Figueroa"],
    ),
    (
        "The Selfish Gene - RICHARD DAWKINS.pdf",
        AUTHOR_LAST,
        "The Selfish Gene",
        ["RICHARD DAWKINS"],
    ),
    (
        "Python Geospatial Development, Second Edit - Erik Westra.pdf",
        AUTHOR_LAST,
        "Python Geospatial Development, Second Edit",
        ["Erik Westra"],
    ),
    (
        "Hermann Hesse _ sein Leben und sein Werk - Hugo Ball.pdf",
        AUTHOR_LAST,
        "Hermann Hesse sein Leben und sein Werk",
        ["Hugo Ball"],
    ),
    # Calibre writes ":" as "_": the full title is primary, the main title is offered too
    (
        "Amerika_ The Missing Person_ A New Transla - Franz Kafka.pdf",
        AUTHOR_LAST,
        "Amerika: The Missing Person: A New Transla",
        ["Franz Kafka"],
    ),
    (
        "Mord auf der Leviathan_ Fandorin ermittelt - Boris Akunin.pdf",
        AUTHOR_LAST,
        "Mord auf der Leviathan: Fandorin ermittelt",
        ["Boris Akunin"],
    ),
    (
        "Rabenseelen_ Roman - Bo R. Holmberg.pdf",
        AUTHOR_LAST,
        "Rabenseelen: Roman",
        ["Bo R. Holmberg"],
    ),
    (
        "Born to Run_ A Novel of the Serrated Edge - Mercedes Lackey.epub",
        AUTHOR_LAST,
        "Born to Run: A Novel of the Serrated Edge",
        ["Mercedes Lackey"],
    ),
    ("Cory_Doctorow_-_For_the_Win.txt", AUTHOR_FIRST, "For the Win", ["Cory Doctorow"]),
    # library-sort titles and trailing annotations
    (
        "Butcher of Anderson Station, The - James S. A. Corey.mobi",
        AUTHOR_LAST,
        "Butcher of Anderson Station, The",
        ["James S. A. Corey"],
    ),
    ("Big U, The - Neal Stephenson.txt", AUTHOR_LAST, "Big U, The", ["Neal Stephenson"]),
    (
        "Cibola Burn (The Expanse) - James S. A. Corey.mobi",
        BOTH,
        "Cibola Burn (The Expanse)",
        ["James S. A. Corey"],
    ),
    # "Last, First" at either end, including co-author lists and annotations
    (
        "Dahl, Roald - Sophiechen und der Riese.pdf",
        AUTHOR_FIRST,
        "Sophiechen und der Riese",
        ["Roald Dahl"],
    ),
    (
        "Boell, Heinrich - Haus ohne Hueter.pdf",
        AUTHOR_FIRST,
        "Haus ohne Hueter",
        ["Heinrich Boell"],
    ),
    (
        "Doyle, Arthur Conan - Die Schreckenskammer.rtf",
        AUTHOR_FIRST,
        "Die Schreckenskammer",
        ["Arthur Conan Doyle"],
    ),
    (
        "Schmidt, K.O. - Die geheimen Herren-Worte des Thomas-Evangeliums.pdf",
        AUTHOR_FIRST,
        "Die geheimen Herren-Worte des Thomas-Evangeliums",
        ["K.O. Schmidt"],
    ),
    (
        "Weiler, Jan - Maria, ihm schmeckts nicht!.pdf",
        AUTHOR_FIRST,
        "Maria, ihm schmeckts nicht!",
        ["Jan Weiler"],
    ),
    (
        "Weber, Stefan - Theorien der Medien.djvu",
        AUTHOR_FIRST,
        "Theorien der Medien",
        ["Stefan Weber"],
    ),
    (
        "Lovecraft, H.P_ - Stadt ohne Namen.pdf",
        AUTHOR_FIRST,
        "Stadt ohne Namen",
        ["H.P. Lovecraft"],
    ),
    ("Carre, John Le - Absolute Freunde.rtf", AUTHOR_FIRST, "Absolute Freunde", ["John Le Carre"]),
    ("Carre,John le - Der Nachtmanager.rtf", AUTHOR_FIRST, "Der Nachtmanager", ["John le Carre"]),
    (
        "Loyola, Ignatius von - Geistliche Uebungen.pdf",
        AUTHOR_FIRST,
        "Geistliche Uebungen",
        ["Ignatius von Loyola"],
    ),
    ("Hoffmann, E. T. A. - Die Raeuber.pdf", AUTHOR_FIRST, "Die Raeuber", ["E. T. A. Hoffmann"]),
    ("Sartre, Jean-Paul - Die Fliegen.pdf", AUTHOR_FIRST, "Die Fliegen", ["Jean-Paul Sartre"]),
    (
        "Amorth, Gabriele + McAll, Kenneth - Wenn Verstorbene nach Befreiung rufen.pdf",
        AUTHOR_FIRST,
        "Wenn Verstorbene nach Befreiung rufen",
        ["Gabriele Amorth", "Kenneth McAll"],
    ),
    (
        "The Adventures of Tom Sawyer - Twain, Mark.epub",
        AUTHOR_LAST,
        "The Adventures of Tom Sawyer",
        ["Mark Twain"],
    ),
    ("Phylogenesis - Foster, Alan Dean.epub", AUTHOR_LAST, "Phylogenesis", ["Alan Dean Foster"]),
    (
        "The Wild's Call - Smith-Ready, Jeri.epub",
        AUTHOR_LAST,
        "The Wild's Call",
        ["Jeri Smith-Ready"],
    ),
    (
        "CONTENT_ Selected Essays on Technology, Cr - Doctorow, Cory.epub",
        AUTHOR_LAST,
        "CONTENT: Selected Essays on Technology, Cr",
        ["Cory Doctorow"],
    ),
    (
        "FIRST TO FIGHT - Sherman, David & Cragg, Dan.epub",
        AUTHOR_LAST,
        "FIRST TO FIGHT",
        ["David Sherman", "Dan Cragg"],
    ),
    (
        "Der Graf von Monte Christo I (I von II) - Dumas, Alexandre (d.A.).pdf",
        AUTHOR_LAST,
        "Der Graf von Monte Christo I (I von II)",
        ["Alexandre Dumas"],
    ),
    # co-author lists and "By" prefixes
    (
        "The Rapture of the Nerds_ A Tale of the Si - Cory Doctorow;Charles Stross.prc",
        AUTHOR_LAST,
        "The Rapture of the Nerds: A Tale of the Si",
        ["Cory Doctorow", "Charles Stross"],
    ),
    (
        "Marooned on Eden - Robert L. Forward & Martha Dodson Forward.epub",
        AUTHOR_LAST,
        "Marooned on Eden",
        ["Robert L. Forward", "Martha Dodson Forward"],
    ),
    (
        "Graphics and GUIs with MATLAB - Patrick Marchand and O. Thomas Holland .pdf",
        AUTHOR_LAST,
        "Graphics and GUIs with MATLAB",
        ["Patrick Marchand", "O. Thomas Holland"],
    ),
    (
        "CONTENT_ Selected Essays on Technology, Cr - By Cory Doctorow.pdb",
        AUTHOR_LAST,
        "CONTENT: Selected Essays on Technology, Cr",
        ["Cory Doctorow"],
    ),
    # series / volume prefixes: the title is the last non-author segment
    (
        "Donaldson, Stephen R. - Covenant 01 - Der Fluch des Veraechters.pdf",
        AUTHOR_FIRST,
        "Der Fluch des Veraechters",
        ["Stephen R. Donaldson"],
    ),
    (
        "McGough, Scott - Magic Kamigawa Zyklus 03 - Die Hueterin.pdf",
        AUTHOR_FIRST,
        "Die Hueterin",
        ["Scott McGough"],
    ),
    (
        "Tolkien, J.R.R. - Der Herr der Ringe 01 - Die Gefaehrten.pdf",
        AUTHOR_FIRST,
        "Die Gefaehrten",
        ["J.R.R. Tolkien"],
    ),
    (
        "Gaymann, Peter & Hauffe, Andreas - Ein Elch packt aus - Der Weihnachtsmann kann einpacken.pdf",  # noqa: E501
        AUTHOR_FIRST,
        "Der Weihnachtsmann kann einpacken",
        ["Peter Gaymann", "Andreas Hauffe"],
    ),
    ("Brust, Steven - Taltos - Taltos.pdf", AUTHOR_FIRST, "Taltos", ["Steven Brust"]),
    ("Card, Orson Scott - Xenocide - Xenocide.txt", AUTHOR_FIRST, "Xenocide", ["Orson Scott Card"]),
    (
        "Die Fliessende Konigin - 02 - Das steinern - Kai Meyer.pdf",
        AUTHOR_LAST,
        "Das steinern",
        ["Kai Meyer"],
    ),
    (
        "Scheibenwelt - 02 - Das Licht der Phantasi - Pratchett, Terry.pdf",
        AUTHOR_LAST,
        "Das Licht der Phantasi",
        ["Terry Pratchett"],
    ),
    (
        "HARRY HARRISON - STAINLESS STEEL RAT 1 - THE STAINLESS STEEL RAT.zip",
        BOTH,
        "THE STAINLESS STEEL RAT",
        ["HARRY HARRISON"],
    ),
    # ambiguous: both ends name-shaped, a single word, or a "Series N" prefix
    ("Robot Visions - Isaac Asimov.epub", BOTH, "Robot Visions", ["Isaac Asimov"]),
    ("Snow Crash - Neal Stephenson.mobi", BOTH, "Snow Crash", ["Neal Stephenson"]),
    ("Poison Sleep - T. A. Pratt.docx", BOTH, "Poison Sleep", ["T. A. Pratt"]),
    ("Exodus - Setve White.epub", BOTH, "Exodus", ["Setve White"]),
    ("Excession - Iain M. Banks.epub", BOTH, "Excession", ["Iain M. Banks"]),
    ("Matter - Iain M. Banks.mobi", BOTH, "Matter", ["Iain M. Banks"]),
    ("Todesschrein - Clive Cussler.rar", BOTH, "Todesschrein", ["Clive Cussler"]),
    (
        "Hells Gate - David Weber & Linda Evans.epub",
        BOTH,
        "Hells Gate",
        ["David Weber", "Linda Evans"],
    ),
    (
        "Hutzschenreuter - Allgemeine Betriebswirtschaftslehre.djvu",
        BOTH,
        "Allgemeine Betriebswirtschaftslehre",
        ["Hutzschenreuter"],
    ),
    (
        "Advanced Mathematics and Mechanics Applications Using MATLAB - Howard B. Wilson.pdf",
        AUTHOR_LAST,
        "Advanced Mathematics and Mechanics Applications Using MATLAB",
        ["Howard B. Wilson"],
    ),
    (
        "Robust Control Design with Matlab - Gu Petkov and Konstantinov.pdf",
        BOTH,
        "Robust Control Design with Matlab",
        ["Gu Petkov and Konstantinov"],
    ),
    (
        "Die Ruckkehr des Tanzlehrers - Henning Mankell_2279.pdf",
        BOTH,
        "Die Ruckkehr des Tanzlehrers",
        ["Henning Mankell"],
    ),
    ("Weltraumreporter 01 - Kurt Brand.pdf", BOTH, "Weltraumreporter 01", ["Kurt Brand"]),
    ("Ender's Saga 1 - Ender's Game.lit", BOTH, "Ender's Game", []),
    ("Ender's Saga 2 - Speaker for the Dead.lit", BOTH, "Speaker for the Dead", []),
    (
        "Expanse 03 - Abaddon's Gate - James S. A. Corey.mobi",
        BOTH,
        "Abaddon's Gate",
        ["James S. A. Corey"],
    ),
    (
        "Kommissar Wallander 09 - Wallanders erster Fall - Henning Mankell_2018.pdf",
        BOTH,
        "Wallanders erster Fall",
        ["Henning Mankell"],
    ),
    (
        "Harry Harrison - StainlessSteelRat 2-TheSSRRevenge(txt).zip",
        AUTHOR_FIRST,
        "StainlessSteelRat 2-TheSSRRevenge(txt)",
        ["Harry Harrison"],
    ),
    (
        "LDAP Directories Explained - An Introduction And Analysis (2003).chm",
        BOTH,
        "LDAP Directories Explained",
        [],
    ),
    ("OpenSCAD - Usermanual.pdf", BOTH, "OpenSCAD", []),
    # placeholder authors
    ("Doctor Marigold - Unknown.txt", NO_AUTHOR, "Doctor Marigold", []),
    ("A Study In Scarlet - Unknown.txt", NO_AUTHOR, "A Study In Scarlet", []),
    ("Much Ado About Nothing - Unknown.txt", NO_AUTHOR, "Much Ado About Nothing", []),
    ("Our children's children - Unknown.txt", NO_AUTHOR, "Our children's children", []),
    ("Priest-Kings of Gor - Unknown.txt", NO_AUTHOR, "Priest-Kings of Gor", []),
    ("Beauty and the Beast - Anonymous.epub", NO_AUTHOR, "Beauty and the Beast", []),
    ("Hobbit, The - Unknown.txt", NO_AUTHOR, "Hobbit, The", []),
    ("Quest of Iranon, The - Unknown.txt", NO_AUTHOR, "Quest of Iranon, The", []),
    ("Call Of The Wild, The - Unknown.txt", NO_AUTHOR, "Call Of The Wild, The", []),
    # same text on both sides, and no separator at all
    ("Die Kandidaten - Die Kandidaten.rtf", NO_AUTHOR, "Die Kandidaten", []),
    ("Superiority.txt", NO_AUTHOR, "Superiority", []),
    ("Tell-Tale Heart, The.txt", NO_AUTHOR, "Tell-Tale Heart, The", []),
]

# Expected extra title hints for library-sort / Calibre forms.
EXTRA_TITLE_HINTS = {
    "Butcher of Anderson Station, The - James S. A. Corey.mobi": "The Butcher of Anderson Station",
    "Big U, The - Neal Stephenson.txt": "The Big U",
    "Hobbit, The - Unknown.txt": "The Hobbit",
    "Tell-Tale Heart, The.txt": "The Tell-Tale Heart",
    "Cibola Burn (The Expanse) - James S. A. Corey.mobi": "Cibola Burn",
    "Rabenseelen_ Roman - Bo R. Holmberg.pdf": "Rabenseelen",
    "Born to Run_ A Novel of the Serrated Edge - Mercedes Lackey.epub": "Born to Run",
}


def _sample_filenames() -> set[str]:
    names: set[str] = set()
    for path in SAMPLES_DIR.glob("*.csv"):
        with path.open(encoding="utf-8", newline="") as handle:
            names.update(row["filename"] for row in csv.DictReader(handle))
    return names


def test_curated_filenames_come_from_the_real_world_sample() -> None:
    sample = _sample_filenames()
    missing = [filename for filename, *_ in CASES if filename not in sample]
    assert not missing
    assert len(CASES) >= 60


@pytest.mark.parametrize(
    ("filename", "orientation", "title", "authors"),
    CASES,
    ids=[case[0] for case in CASES],
)
def test_real_world_filename_orientation(
    filename: str,
    orientation: str,
    title: str,
    authors: list[str],
) -> None:
    title_hints, author_hints = parse_filename_signals(filename)

    if orientation in (AUTHOR_LAST, AUTHOR_FIRST):
        assert title_hints[0] == title
        assert author_hints == authors
    elif orientation == BOTH:
        assert title in title_hints[:2]
        assert all(author in author_hints for author in authors)
        assert len(author_hints) >= 2
    else:
        assert title_hints[0] == title
        assert author_hints == []

    extra = EXTRA_TITLE_HINTS.get(filename)
    if extra:
        assert extra in title_hints
