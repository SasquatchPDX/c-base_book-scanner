"""Identify a book from its own, independent local evidence ("local consensus").

Metadata providers do not know every text a library holds: short stories, fairy tales,
novellas and obscure editions have no record of their own. The library itself is often
curated, though: an author folder, the file name, the file's first lines, its embedded
metadata and a Calibre ``metadata.opf`` each say what the file is. When at least two
*independent* origins name the same title and a plausible author is known, the book is
identified without a provider. Providers can still enrich it later.

Origins, not fields, must be independent:

* ``path``: the file name, the configured folder layout and a Calibre sidecar. Calibre
  names its folders and writes ``metadata.opf`` from the same record, so they are one
  witness, not three.
* ``content``: the title read from the text's first lines (``text_header``).
* ``embedded``: the title stored inside the file (EPUB/MOBI/... metadata).

The rule never overrides a provider: when a provider candidate already names the same
title and author, that candidate's own checks decide (they may have held it for a
reason, e.g. an edition conflict), and this module stays out of it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings as django_settings
from rapidfuzz.fuzz import ratio

from . import identification
from .identification import (
    AUTO_ACCEPT_SCORE,
    EVIDENCE_EMBEDDED,
    TEXT_HEADER_EVIDENCE_KEY,
    IdentificationCandidate,
    IdentificationInput,
    authors_conflict,
    is_placeholder_author,
    normalize_text,
)

LOCAL_PROVIDER_ID = "local"
LOCAL_IDENTITY_KEY = "local_identity"
RULE = "local_consensus"

ORIGIN_PATH = "path"
ORIGIN_CONTENT = "content"
ORIGIN_EMBEDDED = "embedded"
# Which origin's spelling of the title/authors to keep when several agree.
_TITLE_ORIGIN_PREFERENCE = (ORIGIN_EMBEDDED, ORIGIN_CONTENT, ORIGIN_PATH)
_AUTHOR_SOURCE_PREFERENCE = ("embedded", "sidecar", "header", "layout", "header_likely")

# Surname similarity (0-100) that still counts as the same person: tolerates typos in
# hand-named folders ("Cliffard D Simak" vs. "Clifford D. Simak", "Issac Asimov").
SURNAME_SIMILARITY = 80.0
_NOT_A_NAME_WORDS = frozenset(
    {
        "and",
        "und",
        "oder",
        "or",
        "of",
        "the",
        "der",
        "die",
        "das",
        "in",
        "on",
        "for",
        "with",
        "von",
        "zu",
        "werden",
        "chapter",
        "kapitel",
        "appendices",
        "appendix",
        "index",
        "love",
        "war",
        "volume",
        "vol",
        "book",
        "books",
        "part",
        "unknown",
        "various",
        "anonymous",
        "collection",
        "notes",
    }
)
_NAME_TOKEN = re.compile(r"^[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.\-]*$")
_PARTICLES = frozenset({"von", "van", "de", "der", "den", "da", "di", "du", "la", "le", "del"})


def enabled() -> bool:
    return bool(getattr(django_settings, "BOOK_SCANNER_IDENTIFICATION_LOCAL_CONSENSUS", True))


@dataclass(frozen=True)
class LocalIdentity:
    title: str
    authors: list[str]
    title_origins: tuple[str, ...]
    author_source: str
    language: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)

    def as_candidate(self) -> IdentificationCandidate:
        """A provider-shaped candidate, so intake provisions the Book as usual."""
        return IdentificationCandidate(
            provider_id=LOCAL_PROVIDER_ID,
            provider_ref=RULE,
            title=self.title,
            authors=list(self.authors),
            isbn10=None,
            isbn13=None,
            language=self.language,
            description=None,
            publisher=None,
            edition=None,
            publication_year=None,
            score=AUTO_ACCEPT_SCORE,
            raw={LOCAL_IDENTITY_KEY: self.evidence},
        )


def _display_name(value: str) -> str:
    name = " ".join(value.replace("_", " ").split()).strip(" ,;")
    if name.count(",") == 1:  # "Last, First"
        last, first = (part.strip() for part in name.split(","))
        if first and last:
            name = f"{first} {last}"
    return name


def plausible_person(value: str) -> bool:
    """A 1-5 token capitalised name without digits or title-like words."""
    name = _display_name(value)
    if not name or is_placeholder_author(name) or any(ch.isdigit() for ch in name):
        return False
    tokens = re.split(r"[\s]+", name)
    if not 1 <= len(tokens) <= 5:
        return False
    for token in tokens:
        bare = token.strip(".,").lower()
        if bare in _PARTICLES:
            continue
        if bare in _NOT_A_NAME_WORDS or not _NAME_TOKEN.match(token):
            return False
    return any(len(token.strip(".")) >= 3 for token in tokens)


def _surname(name: str) -> str:
    tokens = [t for t in re.split(r"[\s.]+", normalize_text(_display_name(name))) if t]
    return max(tokens, key=len) if tokens else ""


def same_people(left: list[str], right: list[str]) -> bool:
    """Whether two author lists name at least one common person (typos tolerated)."""
    if not authors_conflict(left, right):
        return True
    return any(ratio(_surname(a), _surname(b)) >= SURNAME_SIMILARITY for a in left for b in right)


def _variants(value: Any) -> set[str]:
    """Normalised forms of a title: whole, and its parts around " - " and ": "."""
    if not isinstance(value, str) or not normalize_text(value):
        return set()
    text = value.replace("_", " ")
    match = re.match(r"^(.*),\s*(the|a|an|der|die|das)$", text.strip(), re.IGNORECASE)
    if match:
        text = f"{match.group(2)} {match.group(1)}"
    forms = {text}
    for separator in (" - ", ": "):
        if separator in text:
            forms.update(part for part in text.split(separator) if len(part.strip()) > 3)
    return {_key(form) for form in forms if _key(form)}


def _key(title: str) -> str:
    text = normalize_text(re.sub(r"\(.*?\)", " ", title))
    text = re.sub(r"[^\w\s]", " ", text)
    words = text.split()
    if words and words[0] in {"the", "a", "an", "der", "die", "das"}:
        words = words[1:]
    return " ".join(words)


def _titles_by_origin(metadata: dict[str, Any], filename: str) -> dict[str, dict[str, str]]:
    """``{origin: {normalised title: display title}}``."""
    origins: dict[str, dict[str, str]] = {ORIGIN_PATH: {}, ORIGIN_CONTENT: {}, ORIGIN_EMBEDDED: {}}

    def add(origin: str, value: Any) -> None:
        for key in _variants(value):
            origins[origin].setdefault(key, value.strip() if isinstance(value, str) else key)

    filename_titles, _authors = identification.parse_filename_signals(filename)
    for title in filename_titles:
        add(ORIGIN_PATH, title)
    layout = metadata.get("path_layout")
    if isinstance(layout, dict):
        add(ORIGIN_PATH, layout.get("title"))
    sidecar = metadata.get("sidecar_metadata")
    if isinstance(sidecar, dict) and isinstance(sidecar.get("fields"), dict):
        add(ORIGIN_PATH, sidecar["fields"].get("title"))
    header = metadata.get(TEXT_HEADER_EVIDENCE_KEY)
    if isinstance(header, dict):
        add(ORIGIN_CONTENT, header.get("title"))
    # Only a title the extractor read from inside the file; its file-name fallback
    # (title_source "filename") is no witness.
    if metadata.get("title_source") == EVIDENCE_EMBEDDED:
        add(ORIGIN_EMBEDDED, metadata.get("title"))
    return origins


def _authors_by_source(metadata: dict[str, Any]) -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}

    def add(source: str, values: Any) -> None:
        if not isinstance(values, list):
            return
        names = [_display_name(v) for v in values if isinstance(v, str) and plausible_person(v)]
        if names:
            found[source] = names

    if metadata.get("title_source") == EVIDENCE_EMBEDDED:
        add("embedded", metadata.get("authors"))
    sidecar = metadata.get("sidecar_metadata")
    if isinstance(sidecar, dict) and isinstance(sidecar.get("fields"), dict):
        add("sidecar", sidecar["fields"].get("authors"))
    header = metadata.get(TEXT_HEADER_EVIDENCE_KEY)
    if isinstance(header, dict):
        add("header", header.get("authors"))
    layout = metadata.get("path_layout")
    if isinstance(layout, dict):
        add("layout", layout.get("authors"))
    if not found and isinstance(header, dict):
        # A bare name-like line beside the title may be a character or a typist; it
        # only counts when nothing else names an author, and never contradicts.
        add("header_likely", header.get("likely_authors"))
    return found


def local_consensus(
    metadata: dict[str, Any],
    signals: IdentificationInput,
    candidates: list[IdentificationCandidate],
    *,
    filename: str,
) -> LocalIdentity | None:
    """The book's identity from independent local evidence, or ``None``.

    ``filename`` is the file's original name; its title parts are the path's witness.
    """
    if not enabled():
        return None
    origins = _titles_by_origin(metadata, filename)
    agreed: dict[str, set[str]] = {}
    names = list(origins)
    for i, left in enumerate(names):
        for right in names[i + 1 :]:
            for key in origins[left].keys() & origins[right].keys():
                agreed.setdefault(key, set()).update({left, right})
    if not agreed:
        return None
    # The most specific agreed title: the longest one that the most origins confirm.
    key = max(agreed, key=lambda k: (len(agreed[k]), len(k)))
    title_origins = tuple(o for o in _TITLE_ORIGIN_PREFERENCE if o in agreed[key])
    display = next(origins[o][key] for o in title_origins if key in origins[o])

    authors_by_source = _authors_by_source(metadata)
    if not authors_by_source:
        return None
    sources = [s for s in _AUTHOR_SOURCE_PREFERENCE if s in authors_by_source]
    lists = [authors_by_source[s] for s in sources]
    if any(not same_people(lists[0], other) for other in lists[1:]):
        return None
    authors = lists[0]
    # The file name's author part ("John Roe - Borderline Match") is a guess, too weak to
    # name the author, but a person it names must not contradict the chosen authors.
    _titles, filename_authors = identification.parse_filename_signals(filename)
    named = [
        _display_name(name)
        for name in filename_authors
        # A single word ("Gold", "Frustration") is a title part, not a person.
        if plausible_person(name) and len(name.split()) >= 2 and _key(name) != key
    ]
    if named and not same_people(authors, named):
        return None
    if plausible_person(display) and "," in display:
        return None  # "Kohl, Walter" as the title: author and title are swapped
    if any(_key(display) == _key(author) for author in authors):
        return None

    for candidate in candidates:
        if candidate.provider_id == LOCAL_PROVIDER_ID:
            continue
        if identification._title_match_score(display, candidate.title) >= 1.0 and same_people(
            authors, candidate.authors
        ):
            return None  # a provider names this book; its own checks decide

    evidence = {
        "rule": RULE,
        "title": display,
        "title_origins": list(title_origins),
        "authors": authors,
        "author_source": sources[0],
        "author_sources": sources,
    }
    return LocalIdentity(
        title=display,
        authors=authors,
        title_origins=title_origins,
        author_source=sources[0],
        language=signals.language,
        evidence=evidence,
    )
