"""Title/author hints from the first lines of plain-text books.

Plain-text e-books carry no metadata container, but their first lines very often
name title and author::

    Title: The Sitters            The Alchemist by H.P. Lovecraft
    Author: Clifford D. Simak     Crystal Singer / by: / Anne McCaffrey
    COUNT ZERO / William Gibson   Robert A. Heinlein. All you zombies
    The Project Gutenberg Etext of Barnaby Rudge, by Charles Dickens

Only the first non-empty lines are inspected. A line similar to the file name is
taken as the title; explicit markers ("Author:", "by …", Gutenberg and copyright
lines) give authors; failing those, capitalised name-like lines near the top are
taken as likely authors. Results are hints for the external lookup and are
recorded as evidence together with the lines and rules they came from.
"""

from __future__ import annotations

import re
from typing import Any

from rapidfuzz.fuzz import token_set_ratio

HEADER_LINES = 8  # non-empty lines inspected
NAME_LINE_WINDOW = 5  # bare name-like lines are only trusted this close to the top
MAX_LINE_LENGTH = 200
TITLE_SIMILARITY = 85
MAX_TITLE_WORDS = 14  # longer lines are prose, not a title

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
# Roles that are not the author ("Edited by", "Typed by", "proofread … by").
_NOT_AUTHOR_ROLE = re.compile(
    r"\b(edit(?:ed|or|ors)|translat\w*|illustrat\w*|narrat\w*|read|typed|scanned|"
    r"proofread\w*|ocr|converted|formatted|prepared|produced|herausgegeben|übersetzt)\b",
    re.IGNORECASE,
)
_NAME_TOKEN = re.compile(r"^(?:(?:[A-Z]\.){1,4}|[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*\.?|[A-Z]\.)$")
_NAME_PARTICLES = {"von", "van", "de", "der", "den", "da", "di", "du", "la", "le", "del", "zu", "y"}
_NOT_NAME_WORDS = {
    "the",
    "of",
    "and",
    "a",
    "an",
    "in",
    "on",
    "to",
    "for",
    "with",
    "by",
    "der",
    "die",
    "das",
    "und",
    "ein",
    "eine",
    "chapter",
    "kapitel",
    "volume",
    "vol",
    "band",
    "part",
    "teil",
    "book",
    "buch",
    "edition",
    "version",
    "copyright",
    "published",
    "contents",
    "inhalt",
    "prologue",
    "prolog",
    "introduction",
    "foreword",
    "preface",
    "tales",
    "stories",
    "series",
    "roman",
    "novel",
    "story",
    "etext",
    "ebook",
    "project",
    "gutenberg",
    "forematter",
    "appendices",
    "appendix",
    "index",
    "once",
    "upon",
    "time",
    "unknown",
    "anonymous",
}
_AUTHOR_TITLE_SEPARATOR = re.compile(r"\.\s+|\s{2,}|\s[-–—]\s")
_LEADING_ARTICLE = re.compile(r"^(the|a|an|der|die|das)\s+", re.IGNORECASE)


def _clean(value: str) -> str:
    value = value.strip().strip("*\"'“”„«»").strip()
    value = re.sub(r"\s+", " ", value)
    return value.strip(" .,;:-–—")


def _display_case(value: str) -> str:
    """Title-case ALL-CAPS text without breaking apostrophes ("WHO'S" → "Who's")."""
    if not value.isupper():
        return value
    return " ".join(word[:1].upper() + word[1:].lower() for word in value.split())


def _strip_possessive(name: str) -> str:
    return re.sub(r"['’][sS]$", "", name)


def _name_like(value: str) -> bool:
    """2–5 capitalised name tokens (particles allowed), no digits or structure words."""
    tokens = value.split()
    if not 2 <= len(tokens) <= 5 or any(ch.isdigit() for ch in value):
        return False
    for token in tokens:
        if re.fullmatch(r"(?:[A-Z]\.){1,4}", token):
            continue  # initials such as "A." or "H.P."
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
        if _name_like(name) and name not in names:
            names.append(name)
    return names


def _normalize(value: str) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", value.lower()).split())


def _title_variants(stem: str) -> list[str]:
    """Title candidates from the file name, leaving out author-looking parts.

    "Hobbit, The - Unknown" → ["The Hobbit", "Hobbit"];
    "Ende, Michael - Jim Knopf …" → ["Jim Knopf …"].
    """
    parts = [part.strip() for part in re.split(r"\s+-\s+", stem.replace("_", " ")) if part.strip()]
    variants: list[str] = []
    for position, part in enumerate(parts):
        if _normalize(part) in {"unknown", "anonymous"} or re.fullmatch(r"\d+", part):
            continue
        if part.count(",") == 1 and _name_like(" ".join(reversed(part.split(",")))):
            continue  # "Ende, Michael"
        if position > 0 and _name_like(part):
            continue  # "Title - Arthur Charles Clarke": the later part is the author
        match = re.match(r"^(.*),\s*(The|A|An|Der|Die|Das)$", part, re.IGNORECASE)
        if match:
            part = f"{match.group(2)} {match.group(1)}"
        variants.append(part)
    if len(variants) > 1:
        variants.insert(0, " ".join(variants))
    for variant in list(variants):
        bare = _LEADING_ARTICLE.sub("", variant)
        if bare != variant and len(_normalize(bare)) >= 4:
            variants.append(bare)
    return variants


def _matches_title(line: str, variants: list[str]) -> str | None:
    """The variant the line contains or closely resembles, if any."""
    normalized_line = _normalize(line)
    for variant in variants:
        normalized_variant = _normalize(variant)
        if not normalized_variant:
            continue
        if normalized_variant in normalized_line or normalized_line in normalized_variant:
            return variant
        if token_set_ratio(normalized_line, normalized_variant) >= TITLE_SIMILARITY:
            return variant
    return None


def _author_beside_title(raw_line: str, variant: str) -> str | None:
    """ "Robert A. Heinlein. All you zombies" → "Robert A. Heinlein" (needs a separator)."""
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
    """Title/author hints from the first lines of ``text``; ``None`` if nothing usable."""
    raw_lines: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or _DECORATION.match(line):
            continue
        raw_lines.append(line[:MAX_LINE_LENGTH])
        if len(raw_lines) >= HEADER_LINES:
            break
    if not raw_lines:
        return None

    variants = _title_variants(filename_stem)
    title: str | None = None
    authors: list[str] = []
    rules: list[dict[str, Any]] = []

    def add_authors(found: list[str], line_no: int, rule: str) -> bool:
        known = {author.lower() for author in authors}
        new = [name for name in found if name.lower() not in known]
        if new:
            authors.extend(new)
            rules.append({"line": line_no, "rule": rule, "authors": new})
        return bool(found)

    def set_title(value: str, line_no: int, rule: str) -> None:
        nonlocal title
        value = _display_case(_clean(value))
        if len(value.split()) > MAX_TITLE_WORDS:
            return
        if value and title is None and not _PATH_LIKE.search(value):
            title = value
            rules.append({"line": line_no, "rule": rule, "title": title})

    explicit_author = False
    for index, raw_line in enumerate(raw_lines):
        number = index + 1
        line = _clean(raw_line)
        if match := _LABELLED_TITLE.match(line):
            set_title(match.group(1), number, "labelled title")
            continue
        if match := _LABELLED_AUTHOR.match(line):
            explicit_author |= add_authors(_names(match.group(1)), number, "labelled author")
            continue
        if match := _GUTENBERG.search(line):
            set_title(match.group(1), number, "gutenberg header")
            if match.group(2):
                explicit_author |= add_authors(_names(match.group(2)), number, "gutenberg header")
            continue
        if match := _COPYRIGHT_BY.match(line):
            explicit_author |= add_authors(_names(match.group(1)), number, "copyright line")
            continue
        if match := _BY_ONLY.match(line):
            remainder = match.group(1)
            if not remainder and index + 1 < len(raw_lines):
                remainder = raw_lines[index + 1]  # "by:" alone, name on the next line
            if add_authors(_names(remainder), number, "by line"):
                explicit_author = True
                continue
        if (match := _X_BY_Y.match(line)) and not _NOT_AUTHOR_ROLE.search(match.group(1)):
            found = _names(match.group(2))
            if found:
                left = match.group(1)
                if _matches_title(left, variants):
                    set_title(left, number, "title by author")
                explicit_author |= add_authors(found, number, "title by author")
                continue
        if _PATH_LIKE.search(line):
            continue
        variant = _matches_title(line, variants)
        if variant is not None:
            beside = _author_beside_title(raw_line, variant)
            if beside:
                set_title(variant, number, "line contains file-name title")
                explicit_author |= add_authors([beside], number, "author beside title")
            else:
                set_title(line, number, "line similar to file name")

    if not explicit_author:
        for index, raw_line in enumerate(raw_lines[:NAME_LINE_WINDOW]):
            line = _clean(raw_line)
            if _PATH_LIKE.search(line) or _matches_title(line, variants):
                continue
            name = _strip_possessive(_display_case(line))
            if _name_like(name):
                add_authors([name], index + 1, "name-like line near the top")
            if len(authors) >= 2:
                break

    if title is None and not authors:
        return None
    return {"title": title, "authors": authors, "rules": rules, "lines": raw_lines}
