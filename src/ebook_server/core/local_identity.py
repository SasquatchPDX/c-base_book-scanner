"""What a file's own, independent local evidence says it is ("local consensus").

Metadata providers do not know every text a library holds: short stories, fairy tales,
novellas and obscure editions have no record of their own. The library itself is often
curated, though: an author folder, the file name, the file's first lines, its embedded
metadata and a Calibre ``metadata.opf`` each say what the file is. When at least two
*independent* origins name the same title and a plausible author is known, the result
is a pre-filled **review suggestion**: the reviewer confirms it in one click.

It never accepts a book. External bibliographic verification is mandatory for intake
(AGENTS.md), so a file no provider record fits always goes to review; local consensus
only makes that review fast. When a provider record independently names the same
title and author, the suggestion *confirms* that record (the reviewer accepts the
provider record); its score is not changed, so it is never accepted on local evidence.
A provider record that names the title by someone else is evidence against the local
author: then there is no suggestion. A provider outage is noted in the suggestion.

Origins, not fields, must be independent:

* ``path``: the file name, the configured folder layout and a Calibre sidecar. Calibre
  names its folders and writes ``metadata.opf`` from the same record, so they are one
  witness, not three.
* ``content``: a title the text's first lines state on their own (``text_header``: a
  "Title:" label or a Project Gutenberg header). A header line counted as the title only
  because it repeats the file name echoes the path and is no second witness.
* ``embedded``: the title stored inside the file (EPUB/MOBI/... metadata).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from django.conf import settings as django_settings
from rapidfuzz.fuzz import ratio

from . import identification
from .identification import (
    EVIDENCE_EMBEDDED,
    TEXT_HEADER_EVIDENCE_KEY,
    IdentificationCandidate,
    IdentificationInput,
    ProviderOutcome,
    authors_conflict,
    is_placeholder_author,
    normalize_text,
    title_match_score,
)

LOCAL_IDENTITY_KEY = "local_identity"
RULE = "local_consensus"
# text_header rules whose title does not depend on the file name. Its other title rules
# ("line matches file name", "line contains file-name title", "title by author") only
# take a line as the title because it repeats the file name's title.
_INDEPENDENT_HEADER_TITLE_RULES = frozenset({"labelled title", "gutenberg header"})

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
# Genre, category and catch-all folder names: an "{authors}/**" library may still hold a
# "Science Fiction/" or "Kurzgeschichten/" folder, and that names no person.
_GENRE_WORDS = frozenset(
    """
    fiction nonfiction non-fiction sf scifi sci-fi fantasy horror mystery mysteries
    thriller thrillers romance crime krimi krimis stories story tales fairy märchen
    kurzgeschichten erzählungen novellen novelle romane short-stories poetry poems
    gedichte lyrik drama dramen essays anthology anthologies classics klassiker
    biography biographies biografie biografien history geschichte geschichten
    philosophy philosophie religion comics manga humor humour sachbuch sachbücher
    kinderbuch kinderbücher jugendbuch children kids misc miscellaneous
    sonstiges diverse ebooks e-books bücher library bibliothek incoming unsorted
    science
    """.split()
)
_NAME_TOKEN = re.compile(r"^[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ'’.\-]*$")
_PARTICLES = frozenset({"von", "van", "de", "der", "den", "da", "di", "du", "la", "le", "del"})


def enabled() -> bool:
    return bool(getattr(django_settings, "BOOK_SCANNER_IDENTIFICATION_LOCAL_CONSENSUS", True))


@dataclass(frozen=True)
class LocalIdentity:
    """A review suggestion; never a candidate that intake could accept."""

    title: str
    authors: list[str]
    title_origins: tuple[str, ...]
    author_source: str
    language: str | None = None
    # The provider record that independently names this title and author, if any.
    confirms: dict[str, str] | None = None
    # Providers that failed during the search: their silence proves nothing.
    provider_outage: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


def _display_name(value: str) -> str:
    name = " ".join(value.replace("_", " ").split()).strip(" ,;")
    if name.count(",") == 1:  # "Last, First"
        last, first = (part.strip() for part in name.split(","))
        if first and last:
            name = f"{first} {last}"
    return name


def plausible_person(value: str) -> bool:
    """A 1-5 token capitalised name without digits, title-like or genre words."""
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
        if bare in _NOT_A_NAME_WORDS or bare in _GENRE_WORDS or not _NAME_TOKEN.match(token):
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


def _header_title_is_independent(header: dict[str, Any]) -> bool:
    """Whether the header's title was read without matching it to the file name."""
    rules = header.get("rules")
    if not isinstance(rules, list):
        return False
    title_rules = [
        rule.get("rule") for rule in rules if isinstance(rule, dict) and rule.get("title")
    ]
    return bool(title_rules) and title_rules[0] in _INDEPENDENT_HEADER_TITLE_RULES


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
    if isinstance(header, dict) and _header_title_is_independent(header):
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
    provider_outcomes: list[ProviderOutcome] | tuple[ProviderOutcome, ...] = (),
) -> LocalIdentity | None:
    """A review suggestion from independent local evidence, or ``None``.

    ``filename`` is the file's original name; its title parts are the path's witness.
    ``candidates`` are the provider records: one naming the same title and author is
    the record the suggestion confirms; one naming the title only by someone else
    contradicts the local author, and there is no suggestion. Failed providers in
    ``provider_outcomes`` are noted. The result never makes intake accept anything.
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

    same_title = [
        candidate
        for candidate in candidates
        if candidate.authors and title_match_score(display, candidate.title) >= 1.0
    ]
    agreeing = [candidate for candidate in same_title if same_people(authors, candidate.authors)]
    if same_title and not agreeing:
        # A provider knows this title by someone else ("The Tin Soldier" by Andersen,
        # not by the "Grimm, Jacob" folder): evidence against the local author.
        return None
    confirmed = agreeing[0] if agreeing else None
    confirms = (
        {
            "provider_id": confirmed.provider_id,
            "provider_ref": confirmed.provider_ref,
            "title": confirmed.title,
        }
        if confirmed is not None
        else None
    )
    outage = tuple(
        dict.fromkeys(
            outcome.provider_id for outcome in provider_outcomes if outcome.status == "error"
        )
    )

    evidence: dict[str, Any] = {
        "rule": RULE,
        "title": display,
        "title_origins": list(title_origins),
        "authors": authors,
        "author_source": sources[0],
        "author_sources": sources,
        "language": signals.language,
        "confirms": confirms,
        "provider_outage": list(outage),
    }
    return LocalIdentity(
        title=display,
        authors=authors,
        title_origins=title_origins,
        author_source=sources[0],
        language=signals.language,
        confirms=confirms,
        provider_outage=outage,
        evidence=evidence,
    )
