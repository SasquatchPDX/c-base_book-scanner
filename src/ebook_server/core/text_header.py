"""Title/author hints from the first lines of plain-text books (#43).

Plain-text e-books carry no metadata container, but their first lines very often
name title and author::

    Title: The Sitters            The Alchemist by H.P. Lovecraft
    Author: Clifford D. Simak     Crystal Singer / by: / Anne McCaffrey
    COUNT ZERO / William Gibson   Robert A. Heinlein. All you zombies
    The Project Gutenberg Etext of Barnaby Rudge, by Charles Dickens

Only the first non-empty lines are inspected. A line matching the file name is taken
as the title. Authors come in two strengths:

* ``authors`` -- from explicit rules: a labelled ``Author:`` line, a Project Gutenberg
  header, a ``by ...`` line and a copyright line. These may replace the guesses made
  from splitting the file name.
* ``likely_authors`` -- capitalised name-like lines right next to the title line, used
  only when no explicit rule fired. They are weak and may only be appended after
  other author hints, never put in front of them.

The result holds only the extracted hints and the rules that produced them, never the
raw lines: book contents must not be persisted or logged (AGENTS.md).
"""

from __future__ import annotations

import re
from typing import Any

from .identification import _title_match_score

HEADER_LINES = 8  # non-empty lines inspected
NAME_LINE_WINDOW = 5  # bare name-like lines are only trusted this close to the top
NAME_LINE_TITLE_DISTANCE = 1  # ... and only directly above or below the title line
MAX_LINE_LENGTH = 200
TITLE_SIMILARITY = 0.85  # identification._title_match_score scale
MAX_TITLE_WORDS = 14  # longer lines are prose, not a title
MAX_EXTRA_TITLE_LINE_TOKENS = 4  # "Robert A. Heinlein. All you zombies" has 3

_DECORATION = re.compile(r"^[\W_]+$")
_PATH_LIKE = re.compile(r"[/\\]|\.(?:txt|htm|html|doc|rtf)\b|^\[.*\]$", re.IGNORECASE)
_LABELLED_TITLE = re.compile(r"^(?:title|titel)\s*[:=]\s*(.+)$", re.IGNORECASE)
_LABELLED_AUTHOR = re.compile(
    r"^(?:author|authors|autor|autorin|verfasser|written by)\s*[:=]\s*(.+)$", re.IGNORECASE
)
_GUTENBERG = re.compile(
    r"project gutenberg(?:'s)?\s+(?:e-?text|e-?book)\s+of\s+(.+?)(?:,?\s+by\s+(.+))?$",
    re.IGNORECASE,
)
_COPYRIGHT_BY = re.compile(
    r"^(?:\(c\)|©|copyright)\s*(?:\(c\)\s*)?(?:\d{4}\s*)?,?\s*by\s+(.+)$", re.IGNORECASE
)
_BY_ONLY = re.compile(r"^(?:by|von)\s*:?\s*(.*)$", re.IGNORECASE)
_X_BY_Y = re.compile(r"^(.+?)\s+(?:by|von)\s*:?\s+(.+)$", re.IGNORECASE)
# Roles that are not the author ("Edited by", "Typed by", "Cover designed by").
_NOT_AUTHOR_ROLE = re.compile(
    r"\b(edit(?:ed|or|ors)|translat\w*|illustrat\w*|narrat\w*|read|typed|scanned|"
    r"proofread\w*|ocr|converted|formatted|prepared|produced|design\w*|cover|art\w*|"
    r"photo\w*|typeset\w*|printed|published|herausgegeben|übersetzt)\b",
    re.IGNORECASE,
)
_INITIALS = re.compile(r"(?:[A-Z]\.){1,4}")
_NAME_TOKEN = re.compile(r"^(?:[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*\.?|[A-Z]\.)$")
_NAME_PARTICLES = {"von", "van", "de", "der", "den", "da", "di", "du", "la", "le", "del", "zu", "y"}
# Words that never occur in a person's name but do in header lines around it:
# structure words, genre labels, publishers and places.
_NOT_NAME_WORDS = frozenset(
    """
    the of and a an in on to for with by from at der die das und ein eine
    chapter kapitel volume vol band part teil book books buch edition version copyright
    published publisher publishers publishing press verlag inc ltd library classics
    contents inhalt prologue prolog introduction foreword preface appendix appendices
    index forematter dramatis personae cast characters
    tales stories story series roman novel novella collection anthology
    science fiction fantasy horror mystery thriller romance
    etext ebook project gutenberg once upon time unknown anonymous
    new york london paris berlin boston toronto city
    cover designed design illustrated edited translated printed printing rights reserved
    first second third all
    """.split()
)
_AUTHOR_TITLE_SEPARATOR = re.compile(r"\.\s+|\s{2,}|\s[-–—]\s")
_LEADING_ARTICLE = re.compile(r"^(the|a|an|der|die|das)\s+", re.IGNORECASE)
_PLACEHOLDER_PARTS = {"unknown", "anonymous"}


def _clean(value: str) -> str:
    value = value.strip().strip("*\"'“”„«»").strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,;:-–—")


def _display_case(value: str) -> str:
    """Title-case ALL-CAPS text without breaking apostrophes ("WHO'S" -> "Who's")."""
    if not value.isupper():
        return value
    return " ".join(word[:1].upper() + word[1:].lower() for word in value.split())


def _strip_possessive(name: str) -> str:
    return re.sub(r"['’][sS]$", "", name)


def _normalized_tokens(value: str) -> list[str]:
    return re.sub(r"[^\w]+", " ", value.lower()).split()


def name_like(value: str) -> bool:
    """2-5 capitalised name tokens (particles, initials allowed), no digits or label words."""
    tokens = value.split()
    if not 2 <= len(tokens) <= 5 or any(ch.isdigit() for ch in value):
        return False
    for token in tokens:
        if _INITIALS.fullmatch(token):
            continue  # "A." or "H.P."
        bare = token.lower().strip(".")
        if bare in _NAME_PARTICLES:
            continue
        if bare in _NOT_NAME_WORDS or not _NAME_TOKEN.match(token):
            return False
    return any(len(token.strip(".")) >= 3 for token in tokens)


def _names(value: str) -> list[str]:
    """Author names from text such as "Weber, David & Evans, Linda" or "H.P. Lovecraft"."""
    value = re.sub(
        r"\(c\).*$|\b(?:in|copyright)\s+\d{4}.*$|,?\s*\d{4}.*$", "", value, flags=re.IGNORECASE
    )
    names: list[str] = []
    for part in re.split(r"\s*(?:&|;|\band\b|\bund\b)\s*", value):
        name = _clean(part)
        if name.count(",") == 1:  # "Last, First"
            last, first = (piece.strip() for piece in name.split(","))
            if first and last:
                name = f"{first} {last}"
        name = _strip_possessive(_display_case(name))
        if name_like(name) and name not in names:
            names.append(name)
    return names


def _title_variants(stem: str) -> list[str]:
    """Title candidates from the file name, leaving out author-looking parts.

    "Hobbit, The - Unknown" -> ["The Hobbit", "Hobbit"];
    "Ende, Michael - Jim Knopf ..." -> ["Jim Knopf ..."];
    "Robinson, Kim Stanley - Red Mars" -> ["Red Mars"] (the comma part is the author, so
    the name-like "Red Mars" must be the title).
    """
    parts = [part.strip() for part in re.split(r"\s+-\s+", stem.replace("_", " ")) if part.strip()]
    parts = [
        part
        for part in parts
        if " ".join(_normalized_tokens(part)) not in _PLACEHOLDER_PARTS
        and not re.fullmatch(r"\d+", part)
    ]
    comma_author = [
        part.count(",") == 1 and name_like(" ".join(reversed(part.split(",")))) for part in parts
    ]
    variants: list[str] = []
    for position, part in enumerate(parts):
        if comma_author[position]:
            continue  # "Ende, Michael"
        if position > 0 and not any(comma_author) and name_like(part):
            continue  # "Title - Arthur Charles Clarke": the later part is the author
        match = re.match(r"^(.*),\s*(The|A|An|Der|Die|Das)$", part, re.IGNORECASE)
        if match:
            part = f"{match.group(2)} {match.group(1)}"
        variants.append(part)
    if len(variants) > 1:
        variants.insert(0, " ".join(variants))
    for variant in list(variants):
        bare = _LEADING_ARTICLE.sub("", variant)
        if bare != variant and len("".join(_normalized_tokens(bare))) >= 4:
            variants.append(bare)
    return variants


def _contains_tokens(haystack: list[str], needle: list[str]) -> bool:
    """Whole-word containment: ``needle`` occurs as a contiguous run of ``haystack``."""
    width = len(needle)
    return any(haystack[i : i + width] == needle for i in range(len(haystack) - width + 1))


def _matches_title(line: str, variants: list[str]) -> tuple[str, bool] | None:
    """``(variant, whole_line)`` for the file-name title the line names, if any.

    ``whole_line`` is true when the line *is* the title (equal or closely similar) and
    false when the title only occurs inside it as whole words next to other text. Bare
    substrings never count, so a stem like "Night" does not match prose ending in "night".
    """
    line_tokens = _normalized_tokens(line)
    if not line_tokens:
        return None
    for variant in variants:
        variant_tokens = _normalized_tokens(variant)
        if not variant_tokens:
            continue
        if line_tokens == variant_tokens:
            return variant, True
        if _title_match_score(line, variant) >= TITLE_SIMILARITY:
            return variant, True
        if (
            len(variant_tokens) >= 2
            and len(line_tokens) - len(variant_tokens) <= MAX_EXTRA_TITLE_LINE_TOKENS
            and _contains_tokens(line_tokens, variant_tokens)
        ):
            return variant, False
    return None


def _author_beside_title(raw_line: str, variant: str) -> str | None:
    """ "Robert A. Heinlein. All you zombies" -> "Robert A. Heinlein" (needs a separator)."""
    match = re.search(re.escape(variant), raw_line, re.IGNORECASE)
    if match is None:
        return None
    before, after = raw_line[: match.start()], raw_line[match.end() :]
    for side, separator_at_end in ((before, True), (after, False)):
        if not side.strip():
            continue
        separators = list(_AUTHOR_TITLE_SEPARATOR.finditer(side))
        if not separators:
            continue
        sep = separators[-1] if separator_at_end else separators[0]
        if separator_at_end and side[sep.end() :].strip():
            continue  # the separator must sit right next to the title
        if not separator_at_end and side[: sep.start()].strip():
            continue
        candidate = side[: sep.start()] if separator_at_end else side[sep.end() :]
        candidate = re.sub(r"\(\d+/\d+\)", " ", candidate)
        names = _names(candidate)
        if len(names) == 1:
            return names[0]
    return None


def parse_text_header(text: str, filename_stem: str) -> dict[str, Any] | None:
    """Title/author hints from the first lines of ``text``; ``None`` if nothing usable.

    Returns ``{"title", "authors", "likely_authors", "rules"}``; each rule entry names
    the rule, the 1-based header line number and the hint it produced.
    """
    lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _DECORATION.match(line):
            continue
        lines.append(line[:MAX_LINE_LENGTH])
        if len(lines) >= HEADER_LINES:
            break
    if not lines:
        return None

    variants = _title_variants(filename_stem)
    title: str | None = None
    title_line: int | None = None
    authors: list[str] = []
    likely_authors: list[str] = []
    rules: list[dict[str, Any]] = []

    def add_authors(target: list[str], found: list[str], line_no: int, rule: str) -> bool:
        known = {author.lower() for author in authors + likely_authors}
        new = [name for name in found if name.lower() not in known]
        if new:
            target.extend(new)
            rules.append({"line": line_no, "rule": rule, "authors": new})
        return bool(found)

    def set_title(value: str, line_no: int, rule: str) -> None:
        nonlocal title, title_line
        value = _display_case(_clean(value))
        if len(value.split()) > MAX_TITLE_WORDS:
            return
        if value and title is None and not _PATH_LIKE.search(value):
            title = value
            title_line = line_no
            rules.append({"line": line_no, "rule": rule, "title": title})

    explicit_author = False
    for index, raw_line in enumerate(lines):
        number = index + 1
        line = _clean(raw_line)
        if match := _LABELLED_TITLE.match(line):
            set_title(match.group(1), number, "labelled title")
            continue
        if match := _LABELLED_AUTHOR.match(line):
            explicit_author |= add_authors(
                authors, _names(match.group(1)), number, "labelled author"
            )
            continue
        if match := _GUTENBERG.search(line):
            set_title(match.group(1), number, "gutenberg header")
            if match.group(2):
                explicit_author |= add_authors(
                    authors, _names(match.group(2)), number, "gutenberg header"
                )
            continue
        if match := _COPYRIGHT_BY.match(line):
            explicit_author |= add_authors(
                authors, _names(match.group(1)), number, "copyright line"
            )
            continue
        if match := _BY_ONLY.match(line):
            remainder = match.group(1)
            if not remainder and index + 1 < len(lines):
                remainder = lines[index + 1]  # "by:" alone, name on the next line
            if add_authors(authors, _names(remainder), number, "by line"):
                explicit_author = True
                continue
        if (match := _X_BY_Y.match(line)) and not _NOT_AUTHOR_ROLE.search(match.group(1)):
            found = _names(match.group(2))
            if found:
                left = match.group(1)
                if _matches_title(left, variants):
                    set_title(left, number, "title by author")
                explicit_author |= add_authors(authors, found, number, "title by author")
                continue
        if _PATH_LIKE.search(line):
            continue
        matched = _matches_title(line, variants)
        if matched is None:
            continue
        variant, whole_line = matched
        beside = None if whole_line else _author_beside_title(raw_line, variant)
        if beside:
            set_title(variant, number, "line contains file-name title")
            explicit_author |= add_authors(authors, [beside], number, "author beside title")
        else:
            set_title(line if whole_line else variant, number, "line matches file name")

    if not explicit_author and title_line is not None:
        # Weakest rule: a bare name-like line right above or below the title line.
        for index, raw_line in enumerate(lines[:NAME_LINE_WINDOW]):
            number = index + 1
            if abs(number - title_line) > NAME_LINE_TITLE_DISTANCE:
                continue
            line = _clean(raw_line)
            if _PATH_LIKE.search(line) or _matches_title(line, variants):
                continue
            name = _strip_possessive(_display_case(line))
            if name_like(name):
                add_authors(likely_authors, [name], number, "name-like line beside title")

    if title is None and not authors and not likely_authors:
        return None
    return {
        "title": title,
        "authors": authors,
        "likely_authors": likely_authors,
        "rules": rules,
    }
