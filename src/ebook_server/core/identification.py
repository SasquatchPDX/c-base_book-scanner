"""Bibliographic identification helpers and metadata provider abstraction."""

from __future__ import annotations

import importlib
import json
import re
import socket
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from time import perf_counter
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import structlog
from django.conf import settings as django_settings
from isbnlib import is_isbn10, is_isbn13, to_isbn13
from rapidfuzz.fuzz import ratio, token_set_ratio, token_sort_ratio
from rapidfuzz.utils import default_process

from .rate_limit import RateLimitWaitExceeded, throttle_provider

logger = structlog.get_logger(__name__)

MAX_AUTHOR_LOOKUPS = 5
MAX_RETRY_AFTER_SECONDS = 5.0
REQUEST_TIMEOUT_SECONDS = 5
# Subject/genre tags are evidence only (#50); keep them bounded so an untrusted file or
# a provider record with thousands of subjects cannot bloat job evidence or candidates.
MAX_SUBJECTS = 25
MAX_SUBJECT_LENGTH = 128
OPEN_LIBRARY_SEARCH_FIELDS = ",".join(
    (
        "key",
        "title",
        "author_name",
        "author_key",
        "first_publish_year",
        "language",
        "edition_count",
        "cover_i",
        "cover_edition_key",
        "subject",
    )
)
DEFAULT_USER_AGENT = (
    "ebook-server (+https://github.com/SasquatchPDX/c-base_book-scanner; "
    "self-hosted hackerspace library)"
)

AUTO_ACCEPT_SCORE = getattr(
    django_settings,
    "BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT",
    0.86,
)
REVIEW_SCORE = getattr(django_settings, "BOOK_SCANNER_IDENTIFICATION_REVIEW", 0.55)
# Evidence key for title/author hints read from the first lines of plain-text books
# (see core.text_header); written by intake, read when building signals.
TEXT_HEADER_EVIDENCE_KEY = "text_header"
# Added to an already linked Book's score when the file is another format of it (same
# source folder, same file name, ``SourceLocation.group_sibling_formats``). With an equal
# title (0.55) it clears auto-accept; alone it does not (#39).
SIBLING_FORMAT_BONUS = getattr(
    django_settings,
    "BOOK_SCANNER_IDENTIFICATION_SIBLING_BONUS",
    0.35,
)
# Title similarity above which two candidates may describe the same book.
SAME_BOOK_TITLE_MIN_SCORE = 0.9
# Author-name variants (same surname, compatible given names) count as a partial match.
AUTHOR_VARIANT_MATCH_SCORE = 0.9
# Minimum rapidfuzz ratio for two given-name tokens to count as spellings of one name.
AUTHOR_GIVEN_NAME_MIN_RATIO = 85.0

# Strong-evidence rule for candidates without ISBN confirmation (see
# docs/metadata-providers.md and _apply_strong_match_rule). Its settings are read at
# call time so they can be tuned per deployment and overridden in tests.
STRONG_MATCH_RULE = "exact_title_strong_author"
STRONG_MATCH_EVIDENCE_KEY = "_auto_accept_rule"

# Common ISO 639-1 / 639-2 (B and T) codes and English names, for the language-conflict
# check only. Unknown values are compared verbatim, which errs towards "conflict".
_LANGUAGE_ALIASES: dict[str, str] = {
    alias: code
    for code, aliases in {
        "en": ("eng", "english"),
        "de": ("ger", "deu", "german", "deutsch"),
        "fr": ("fre", "fra", "french"),
        "es": ("spa", "spanish"),
        "it": ("ita", "italian"),
        "nl": ("dut", "nld", "dutch"),
        "pt": ("por", "portuguese"),
        "ru": ("rus", "russian"),
        "pl": ("pol", "polish"),
        "cs": ("cze", "ces", "czech"),
        "sv": ("swe", "swedish"),
        "da": ("dan", "danish"),
        "no": ("nor", "nob", "norwegian"),
        "fi": ("fin", "finnish"),
        "hu": ("hun", "hungarian"),
        "el": ("gre", "ell", "greek"),
        "la": ("lat", "latin"),
        "tr": ("tur", "turkish"),
        "ja": ("jpn", "japanese"),
        "zh": ("chi", "zho", "chinese"),
    }.items()
    for alias in (code, *aliases)
}


class MetadataProvider(Protocol):
    """Minimal metadata-provider contract."""

    provider_id: str

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return raw candidate dicts from external metadata source."""


# --- Typed evidence ----------------------------------------------------------------------
# Where a title or author hint came from, strongest first. Embedded metadata is what the
# file says about itself; a sidecar (Calibre metadata.opf) is curated next to it; a text
# header is explicit but unstructured; a configured folder layout is deliberate but
# coarse; a filename is a guess.
EVIDENCE_EMBEDDED = "embedded"
EVIDENCE_SIDECAR = "sidecar"
EVIDENCE_HEADER = "header"
EVIDENCE_LAYOUT = "layout"
EVIDENCE_FILENAME = "filename"
EVIDENCE_UNKNOWN = "unknown"
EVIDENCE_RANK = {
    EVIDENCE_EMBEDDED: 5,
    EVIDENCE_SIDECAR: 4,
    EVIDENCE_HEADER: 3,
    EVIDENCE_LAYOUT: 2,
    EVIDENCE_FILENAME: 1,
    EVIDENCE_UNKNOWN: 0,
}

# How the names of one author group relate to each other.
# * ALTERNATIVES: competing guesses, at most one of which is the author ("Stephen King -
#   Pet Sematary" yields both halves). The best single match is the group's score.
# * COAUTHORS: every name is a genuine author (embedded dc:creator list, sidecar, an
#   explicit header). A candidate must cover them all; its score is the share covered.
AUTHORS_ALTERNATIVES = "alternatives"
AUTHORS_COAUTHORS = "coauthors"


@dataclass(frozen=True)
class AuthorEvidence:
    kind: str
    source: str
    names: tuple[str, ...]


@dataclass(frozen=True)
class TitleEvidence:
    title: str
    source: str


class ProviderRequestError(OSError):
    """A metadata provider could not answer the request.

    Distinct from "no results": the provider timed out, was unreachable, answered
    with an HTTP error, rate limited us or returned a malformed response. ``reason``
    is a short, safe description (never a URL, query string or credential), shown
    to reviewers and recorded on the search run.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def describe_provider_error(exc: BaseException) -> str:
    """Short, credential-free reason for a provider failure, for reviewers and logs."""
    if isinstance(exc, ProviderRequestError):
        return exc.reason
    if isinstance(exc, RateLimitWaitExceeded):
        return "rate limited (local request budget exhausted)"
    if isinstance(exc, HTTPError):
        if exc.code == 429:
            return "rate limited (HTTP 429)"
        return f"HTTP {exc.code}"
    if isinstance(exc, URLError):
        if isinstance(exc.reason, (TimeoutError, socket.timeout)):
            return "timed out"
        return "unreachable"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timed out"
    if isinstance(exc, (UnicodeDecodeError, json.JSONDecodeError)):
        return "malformed response"
    if isinstance(exc, OSError) and type(exc).__name__.endswith("ProviderError"):
        # Provider-specific errors (e.g. Hardcover's) promise credential-free messages.
        return str(exc)[:200] or type(exc).__name__
    return type(exc).__name__


@dataclass(frozen=True)
class ProviderOutcome:
    """What one provider did during one search run."""

    provider_id: str
    status: str  # "results" | "no_results" | "error"
    result_count: int
    duration_seconds: float
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "status": self.status,
            "result_count": self.result_count,
            "duration_seconds": round(self.duration_seconds, 3),
            "error": self.error,
        }


@dataclass(frozen=True)
class IdentificationInput:
    title_hints: list[str]
    author_hints: list[str]
    isbns: list[str]
    folder_hints: list[str]
    language: str | None
    publisher: str | None
    description: str | None
    publication_year: int | None = None
    # Typed evidence behind the flat hint lists. Inputs built by hand (scan jobs, review
    # re-searches, tests) may leave them empty: the hints then count as alternatives of
    # unknown provenance, which is how they were always scored.
    author_evidence: tuple[AuthorEvidence, ...] = ()
    title_evidence: tuple[TitleEvidence, ...] = ()

    def best_title(self) -> str | None:
        return self.title_hints[0] if self.title_hints else None

    def best_author(self) -> str | None:
        return self.author_hints[0] if self.author_hints else None

    def author_groups(self) -> tuple[AuthorEvidence, ...]:
        if self.author_evidence:
            return self.author_evidence
        if not self.author_hints:
            return ()
        names = tuple(self.author_hints)
        return (AuthorEvidence(AUTHORS_ALTERNATIVES, EVIDENCE_UNKNOWN, names),)

    def title_sources(self) -> tuple[TitleEvidence, ...]:
        if self.title_evidence:
            return self.title_evidence
        return tuple(TitleEvidence(title, EVIDENCE_UNKNOWN) for title in self.title_hints)


@dataclass(frozen=True)
class IdentificationCandidate:
    provider_id: str
    provider_ref: str
    title: str
    authors: list[str]
    isbn10: str | None
    isbn13: str | None
    language: str | None
    description: str | None
    publisher: str | None
    edition: str | None
    publication_year: int | None
    score: float
    raw: dict[str, Any]
    subjects: list[str] = field(default_factory=list)


def _normalize_identifier(value: str | None) -> str | None:
    if not value:
        return None

    normalized = re.sub(r"[^0-9Xx]", "", value).upper()
    if is_isbn10(normalized):
        return normalized
    if is_isbn13(normalized):
        return normalized

    if len(normalized) == 10 and _isbn10_valid(normalized):
        return normalized
    if len(normalized) == 13 and _isbn13_valid(normalized):
        return normalized
    return None


def _isbn10_check(isbn10: str) -> bool:
    total = 0
    for index, char in enumerate(isbn10):
        value = 10 if char == "X" else int(char)
        total += value * (10 - index)
    return total % 11 == 0


def _isbn10_to_isbn13(isbn10: str) -> str:
    converted = to_isbn13(isbn10)
    if _isbn13_valid(converted):
        return converted
    body = isbn10[:-1]
    prefix12 = f"978{body}"
    total = 0
    for index, char in enumerate(prefix12):
        value = int(char)
        total += value * (1 if index % 2 == 0 else 3)
    check = (10 - (total % 10)) % 10
    return f"{prefix12}{check}"


def _isbn13_valid(isbn13: str) -> bool:
    if len(isbn13) != 13 or not isbn13.isdigit():
        return False
    if not isbn13.startswith("978") and not isbn13.startswith("979"):
        return False
    check_digit = int(isbn13[-1])
    total = 0
    for index, char in enumerate(isbn13[:-1]):
        value = int(char)
        total += value * (1 if index % 2 == 0 else 3)
    expected = (10 - (total % 10)) % 10
    return expected == check_digit


def _isbn10_valid(isbn10: str) -> bool:
    if not re.fullmatch(r"[0-9]{9}[0-9X]", isbn10):
        return False
    return _isbn10_check(isbn10)


def normalize_isbns(raw: str | None) -> list[str]:
    """Return validated ISBN-10/13 values from loosely formatted string."""
    if not raw:
        return []
    values: list[str] = []
    for token in re.split(r"[\s,;]+", raw):
        normalized = _normalize_identifier(token)
        if not normalized:
            continue
        if normalized not in values:
            values.append(normalized)
        if len(normalized) == 10:
            converted = _isbn10_to_isbn13(normalized)
            if converted not in values:
                values.append(converted)
    return values


def normalize_subjects(values: Any) -> list[str]:
    """Return trimmed, de-duplicated (case-insensitive), bounded subject/genre tags.

    Tags are kept as free text in first-seen order; no taxonomy mapping is applied here.
    """
    subjects: list[str] = []
    seen: set[str] = set()
    for value in _as_str_list(values):
        tag = re.sub(r"\s+", " ", value).strip()
        if not tag or len(tag) > MAX_SUBJECT_LENGTH:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        subjects.append(tag)
        if len(subjects) >= MAX_SUBJECTS:
            break
    return subjects


def normalize_text(value: str | None) -> str:
    if not value:
        return ""
    return " ".join(value.lower().strip().split())


# --- Person-name heuristics (#15) -------------------------------------------------------
# Shared by every signal source that has to tell a person's name apart from a title
# (filename parsing below, TXT header hints, ...). Keep them in this one place so the word
# lists cannot drift between callers.

_INITIALS_TOKEN = re.compile(r"^(?:[A-Z]\.?){1,3}$")
_CAPITALISED_TOKEN = re.compile(r"^[A-ZÀ-ÖØ-Þ][A-Za-zÀ-ÖØ-öø-ÿ'’\-]*\.?$")
# An inner capital after a lowercase letter, except in "McNab", "MacDonald", "DeWitt",
# "LeGuin", "VanDyke", "O'Brien"-style surnames.
_CAMEL_CASE_WORD = re.compile(r"^(?!Mc|Mac|De|Di|Da|La|Le|Van|Von).*[a-z][A-Z]")
# Lowercase particles inside a name: "Ludwig van Beethoven", "Gabriel de la Cruz".
_NAME_PARTICLES = frozenset(
    {"von", "van", "de", "da", "di", "du", "la", "le", "del", "della", "zu", "y", "ter", "ten"},
)
# "der"/"den" count as particles only straight after von/van/de ("Johannes van der Waals");
# anywhere else they are far more likely German title words ("Insel der Freibeuter").
_COMPOUND_PARTICLES = frozenset({"der", "den"})
_COMPOUND_PARTICLE_LEADS = frozenset({"von", "van", "de"})
# Words that (almost) never occur in a person's name but are common in titles.
_NOT_NAME_WORDS = frozenset(
    {
        # English function words
        *("the", "a", "an", "of", "and", "or", "in", "on", "at", "to", "for", "with", "by"),
        *("from", "into", "is", "are", "my", "your", "his", "her", "its", "our", "their"),
        *("this", "that", "not", "no", "than", "as", "about", "after", "before", "over"),
        *("under", "without", "all", "so", "if", "when", "who", "what", "how", "why"),
        # German function words
        *("der", "die", "das", "des", "dem", "den", "ein", "eine", "einer", "und", "oder"),
        *("im", "am", "auf", "mit", "aus", "zum", "zur", "vom", "ohne"),
        # placeholders and bibliographic words
        *("unknown", "anonymous", "various", "unbekannt", "volume", "vol", "book", "band"),
        *("bd", "part", "teil", "series", "edition", "stories", "tales", "saga"),
        *("chronicles", "novel", "novella", "roman", "anthology", "collection", "omnibus"),
        *("guide", "handbook", "introduction", "manual"),
    },
)
# Placeholder authors ("Doctor Marigold - Unknown") are recognised with the shared
# ``is_placeholder_author`` further down, which the author-conflict guard also uses.
_TITLE_ARTICLES = frozenset({"the", "a", "an", "der", "die", "das"})
_AUTHOR_LIST_SEPARATOR = re.compile(r"\s*(?:&|;|\+|\band\b|\bund\b)\s*", re.IGNORECASE)


def looks_like_person_name(value: str) -> bool:
    """True when ``value`` reads as one person's name in "First [Middle] Last" order.

    Two to five tokens, each capitalised, an initial ("A.", "H.P.", "T"), or a lowercase
    particle inside the name ("van"); no digits and no title/function words. This is
    shape-based, so short title-case titles ("Pet Sematary") match too: callers must
    treat a match as evidence, never as proof.
    """
    tokens = value.split()
    if not 2 <= len(tokens) <= 5 or any(ch.isdigit() for ch in value):
        return False
    shouting = value.isupper()  # "HARRY HARRISON": all-caps tokens are fine here
    has_name_word = False
    for index, token in enumerate(tokens):
        if _INITIALS_TOKEN.match(token):
            continue
        if not shouting and (token.isupper() or _CAMEL_CASE_WORD.search(token)):
            return False  # acronyms and product names: "LDAP", "MATLAB", "MathWorks"
        bare = token.lower().rstrip(".")
        if token.islower() and 0 < index < len(tokens) - 1:
            if bare in _NAME_PARTICLES or (
                bare in _COMPOUND_PARTICLES and tokens[index - 1] in _COMPOUND_PARTICLE_LEADS
            ):
                continue
        if bare in _NOT_NAME_WORDS or not _CAPITALISED_TOKEN.match(token):
            return False
        has_name_word = True
    return has_name_word


# Edition, volume and ordinal words: "Python Development, Second Edit", "Windows
# Programming, Third Edition", "Faust, Zweite Auflage". A comma phrase containing one is
# a title with its edition/volume suffix, never "Last, First" (#15). "Ed" alone is left
# out on purpose: it is also a given name ("McBain, Ed"), and "2nd Ed." is caught by the
# ordinal anyway.
_EDITION_WORDS = frozenset(
    {
        *("edition", "editions", "edit", "ausgabe", "auflage", "aufl", "neuauflage"),
        *("vol", "volume", "volumes", "part", "band", "bd", "teil", "book", "buch"),
        *("revised", "expanded", "updated", "unabridged", "abridged", "reprint"),
        *("first", "second", "third", "fourth", "fifth", "sixth", "seventh", "eighth"),
        *("ninth", "tenth", "eleventh", "twelfth"),
    },
)
_GERMAN_ORDINAL = re.compile(
    r"^(?:erst|zweit|dritt|viert|f(?:ü|ue|u)nft|sechst|siebt|acht|neunt|zehnt)(?:e|er|en|es)$",
)
_NUMERIC_ORDINAL = re.compile(r"^\d+(?:st|nd|rd|th|\.)?$")


def is_edition_phrase(value: str) -> bool:
    """True when ``value`` contains an edition, volume, part or ordinal word."""
    for token in value.split():
        bare = token.lower().strip(".,;:()[]")
        if bare in _EDITION_WORDS or _GERMAN_ORDINAL.match(bare) or _NUMERIC_ORDINAL.match(bare):
            return True
    return False


def invert_last_first_name(value: str) -> str | None:
    """ "Corey, James S. A." -> "James S. A. Corey" when it reads as a person's name.

    Returns ``None`` for anything else, including the library-sort title form
    "Hobbit, The" and comma titles such as "Python Development, Second Edit": an
    edition/volume/ordinal phrase on either side of the comma is never a name.
    """
    if value.count(",") != 1:
        return None
    last, first = (part.strip() for part in value.split(","))
    if not last or not first or first.lower() in _TITLE_ARTICLES:
        return None
    if is_edition_phrase(first) or is_edition_phrase(last):
        return None
    # A surname is one or two words plus particles ("Le Guin", "van Vogt", "Carre").
    surname_words = [t for t in last.split() if t.lower() not in _NAME_PARTICLES]
    if len(surname_words) > 2:
        return None
    candidate = f"{first} {last}"
    return candidate if looks_like_person_name(candidate) else None


def split_author_list(value: str) -> list[str] | None:
    """ "David Weber & Linda Evans" / "Cory Doctorow;Charles Stross" -> list of names.

    Each part may be "First Last" or "Last, First" (returned as "First Last"). Returns
    ``None`` unless every part reads as a person's name.
    """
    names: list[str] = []
    for raw_part in _AUTHOR_LIST_SEPARATOR.split(value):
        part = raw_part.strip()
        if not part:
            continue
        inverted = invert_last_first_name(part)
        if inverted:
            names.append(inverted)
        elif looks_like_person_name(part):
            names.append(part)
        else:
            return None
    return names or None


# Lopsided separators ("Asimov- Foundation", "Foundation -Asimov") split too; a hyphen
# inside a word ("Mother-Not", "Priest-Kings of Gor") never does.
_FILENAME_SEPARATOR = re.compile(r"\s+-+\s*|\s*-+\s+|--+")
# Calibre writes ":" as "_" ("Rabenseelen_ Roman") and the "." of a trailing initial as
# "_" ("Lovecraft, H.P_").
_UNDERSCORE_INITIAL = re.compile(r"(?<=\b[A-Za-z])_(?=\s|$)")
_UNDERSCORE_COLON = re.compile(r"(?<=\w)_(?=\s)")
# "Title, The" -> "The Title" (library sorting convention).
_INVERTED_ARTICLE = re.compile(r"^(.+?),\s*(The|A|An|Der|Die|Das)$", re.IGNORECASE)
_TRAILING_ANNOTATION = re.compile(r"\s*[(\[][^()\[\]]*[)\]]\s*$")
_NUMERIC_SEGMENT = re.compile(r"^[\d\s.#]+$")
_BY_PREFIX = re.compile(r"^by\s+", re.IGNORECASE)


def _normalize_filename_stem(stem: str) -> str:
    value = _UNDERSCORE_INITIAL.sub(".", stem)
    value = _UNDERSCORE_COLON.sub(":", value)
    return " ".join(value.replace("_", " ").split())


def _title_variants(title: str) -> list[str]:
    """Cheap, safe normalisations of one title guess, as extra hints (never a replacement)."""
    variants = [title]
    stripped = _TRAILING_ANNOTATION.sub("", title).strip()
    if stripped:
        variants.append(stripped)
    for value in list(variants):
        inverted = _INVERTED_ARTICLE.match(value)
        if inverted:
            variants.append(f"{inverted.group(2)} {inverted.group(1)}".strip())
    main_title = title.split(": ", 1)[0].strip()
    if main_title:
        variants.append(main_title)
    return list(dict.fromkeys(v for v in variants if v))


def _titles_for(title_segments: list[str]) -> list[str]:
    """Primary title, then the fuller fallback, for the segments left beside the author.

    "Series 03 - Title" puts the title last; the series/volume prefix is only kept as a
    fallback, since searching with it attached can make a provider return nothing.
    """
    if len(title_segments) == 1:
        return title_segments
    named = [s for s in title_segments if not _NUMERIC_SEGMENT.match(s)]
    primary = named[-1] if named else title_segments[-1]
    return [primary, " - ".join(title_segments)]


def _same_title(left: str, right: str) -> bool:
    def strip(value: str) -> str:
        return normalize_text(_TRAILING_ANNOTATION.sub("", value))

    return strip(left) == strip(right)


# How strongly one end of a filename split reads as the author.
_END_NOT_A_NAME = 0
# A single word could be a bare surname ("Asimov - Foundation"), and "Series 2" could
# be the series prefix of a name-shaped title ("Ender's Saga 1 - Ender's Game"): neither
# is a name, but neither rules out the other end being a title.
_END_UNCERTAIN = 1
_END_NAME_SHAPED = 2
_END_LAST_FIRST = 3
_SINGLE_WORD = re.compile(r"^[^\W\d_][\w'’.\-]*$")
_SERIES_NUMBER_SUFFIX = re.compile(r"\s(?:\d+|[IVX]+)$")
_TRAILING_NUMBER = re.compile(r"[\s\d.#]+$")


def _author_end(segment: str) -> tuple[int, list[str]]:
    """Classify one end of a split and return the author name(s) it would provide.

    A plain "A B and C D" list counts as name-shaped, not stronger: "Advanced
    Mathematics and Mechanics Applications" splits into two name-shaped halves just as
    well as two authors do.
    """
    segment = _TRAILING_ANNOTATION.sub("", segment).strip()  # "Dumas, Alexandre (d.A.)"
    segment = _BY_PREFIX.sub("", segment)  # "By Cory Doctorow"
    names = split_author_list(segment)
    if names:
        parts = (part.strip() for part in _AUTHOR_LIST_SEPARATOR.split(segment))
        if any(invert_last_first_name(part) for part in parts if part):
            return _END_LAST_FIRST, names
        return _END_NAME_SHAPED, names
    if _SINGLE_WORD.match(segment) or _SERIES_NUMBER_SUFFIX.search(segment):
        return _END_UNCERTAIN, []
    return _END_NOT_A_NAME, []


def _ambiguous_author_hint(segment: str) -> str | None:
    """One end of an ambiguous split, cleaned up for use as a (possible) author hint."""
    cleaned = _TRAILING_ANNOTATION.sub("", segment).strip()
    cleaned = _TRAILING_NUMBER.sub("", cleaned).strip()  # "Henning Mankell_2279"
    if not cleaned or any(ch.isdigit() for ch in cleaned):
        return None
    return cleaned


def _commits(author_end: int, other_end: int) -> bool:
    """Whether one end is clearly enough the author to drop the other orientation."""
    if author_end == _END_LAST_FIRST:
        return other_end < _END_LAST_FIRST
    return author_end == _END_NAME_SHAPED and other_end == _END_NOT_A_NAME


def parse_filename_signals(raw_filename: str) -> tuple[list[str], list[str]]:
    """Return title and author candidates from filename patterns.

    "Title - Author" and "Author - Title" are both common, so the two ends of a split
    filename are compared:

    * "Title - Unknown" / "Unknown - Title": no author; the rest is the title.
    * Exactly one end in "Last, First" form - or, failing that, one end name-shaped
      and the other plainly not a name: commit to that orientation. The author hints
      are that name (each name, for a co-author list) and nothing from the title side.
    * Otherwise - both ends name-shaped ("Stephen King - Pet Sematary"), neither
      ("Asimov - Foundation", "tolkien - the hobbit"), or the other end a single word
      (maybe a bare surname) or a "Series 2" prefix: keep *both* orientations as hints,
      "Title - Author" first since it is the more common convention. Author scoring
      takes the best-matching hint, so the wrong orientation costs nothing.

    Only the outermost segments can be an author. For "Series 03 - Title - Author" the
    series/volume prefix is dropped from the primary title and kept as a fallback hint.
    Library sort titles ("Title, The") and Calibre's "_" for ":" are also undone.
    """
    stem = Path(raw_filename).stem
    if not stem:
        return [], []
    normalized = _normalize_filename_stem(stem)
    if not normalized:
        return [], []

    segments = [s.strip() for s in _FILENAME_SEPARATOR.split(normalized)]
    segments = [s for s in segments if s.strip("-")]
    title_guesses: list[str] = []
    author_hints: list[str] = []

    if len(segments) >= 2:
        first, last = segments[0], segments[-1]
        if is_placeholder_author(last):
            title_guesses = _titles_for(segments[:-1])
        elif is_placeholder_author(first):
            title_guesses = _titles_for(segments[1:])
        elif len(segments) == 2 and _same_title(first, last):
            title_guesses = [first, last]  # "Die Kandidaten - Die Kandidaten"
        else:
            last_strength, last_names = _author_end(last)
            first_strength, first_names = _author_end(first)
            if _commits(last_strength, first_strength):
                author_hints = last_names
                title_guesses = _titles_for(segments[:-1])
            elif _commits(first_strength, last_strength):
                author_hints = first_names
                title_guesses = _titles_for(segments[1:])
            else:
                author_last_titles = _titles_for(segments[:-1])
                author_first_titles = _titles_for(segments[1:])
                title_guesses = [author_last_titles[0], author_first_titles[0]]
                title_guesses += author_last_titles[1:] + author_first_titles[1:]
                for names, end in ((last_names, last), (first_names, first)):
                    hint = _ambiguous_author_hint(end)
                    author_hints.extend(names or ([hint] if hint else []))

    title_hints: list[str] = []
    for guess in [*title_guesses, normalized]:
        title_hints.extend(_title_variants(guess))

    return (
        list(dict.fromkeys(h for h in title_hints if h)),
        list(dict.fromkeys(h for h in author_hints if h)),
    )


def _header_strings(value: Any) -> list[str]:
    return [item.strip() for item in _as_str_list(value) if item.strip()]


# "Last, First" in a filename part: one comma, a short name on each side, no digits,
# and not a moved article ("Hobbit, The").
_COMMA_FORM_AUTHOR = re.compile(r"^\s*([^,\d]{2,40}),\s*([^,\d]{1,40})\s*$")
_MOVED_ARTICLES = frozenset({"the", "a", "an", "der", "die", "das", "le", "la", "les"})


def comma_form_filename_authors(raw_filename: str) -> list[str]:
    """Filename parts written as "Last, First" ("London, Jack - White Fang.txt").

    That form is a deliberate author credit, so header hints read from the text may
    add to it but never delete it.
    """
    stem = Path(raw_filename).stem.replace("_", " ")
    authors: list[str] = []
    for part in re.split(r"\s+-\s+", stem):
        match = _COMMA_FORM_AUTHOR.match(part)
        if match is None:
            continue
        last, first = match.group(1).strip(), match.group(2).strip()
        if not last or not first or first.casefold() in _MOVED_ARTICLES:
            continue
        if len(last.split()) > 3 or len(first.split()) > 3:
            continue
        authors.append(part.strip())
    return authors


def _apply_text_header_hints(
    header: dict[str, Any],
    title_hints: list[str],
    author_hints: list[str],
    *,
    protected_authors: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Merge hints read from a plain-text book's first lines (see ``core.text_header``).

    Precedence for authors: explicit header rules (``Author:``, Gutenberg header,
    ``by ...``, copyright line) replace the filename's "A - B" split guesses; heuristic
    name-like lines are only appended after the other hints, so they never become the
    author sent to providers ahead of them. Hints from an admin-configured source such
    as a folder layout must likewise rank ahead of anything guessed from the text.

    ``protected_authors`` (the filename's "Last, First" credits) are never removed:
    not because the header title resembles them, and not by explicit header authors,
    which are put in front of them instead.
    """
    title_hints = list(title_hints)
    author_hints = list(author_hints)
    protected = [name for name in protected_authors or [] if name.strip()]
    protected_keys = {normalize_text(name) for name in protected}
    header_title = header.get("title")
    if isinstance(header_title, str) and header_title.strip():
        header_title = header_title.strip()
        title_hints.insert(1 if title_hints else 0, header_title)
        # A filename part the text names as the title is not an author, unless the
        # filename credits it in "Last, First" form.
        author_hints = [
            hint
            for hint in author_hints
            if normalize_text(hint) in protected_keys
            or _title_match_score(hint, header_title) < 0.95
        ]
    explicit_authors = _header_strings(header.get("authors"))
    if explicit_authors:
        kept = [
            name
            for name in protected
            if not any(
                _author_name_similarity(normalize_text(name), normalize_text(explicit))
                for explicit in explicit_authors
            )
        ]
        return title_hints, explicit_authors + kept
    author_hints = [
        hint for hint in author_hints if normalize_text(hint) not in {"unknown", "anonymous"}
    ]
    for likely in _header_strings(header.get("likely_authors")):
        if not any(_author_name_similarity(likely, known) for known in author_hints):
            author_hints.append(likely)
    return title_hints, author_hints


# Evidence key holding hints from the source's configured folder layout (#35); written
# by intake from ``core.source_layout``, read here when building signals.
PATH_LAYOUT_EVIDENCE_KEY = "path_layout"


def build_identification_input(
    *,
    source_path: str,
    original_filename: str,
    extracted_metadata: dict[str, Any],
) -> IdentificationInput:
    filename_title_hints, filename_author_hints = parse_filename_signals(original_filename)

    metadata_title = extracted_metadata.get("title")
    # Format extractors with nothing real to say fall back to the raw filename stem as
    # "title" (see intake.extract_metadata). That is no better than the parsed filename
    # hints above, so it must not jump ahead of them.
    title_source = extracted_metadata.get("title_source")
    is_stem_fallback = title_source == EVIDENCE_FILENAME or (
        title_source is None
        and isinstance(metadata_title, str)
        and normalize_text(metadata_title) == normalize_text(Path(original_filename).stem)
    )
    if isinstance(metadata_title, str) and normalize_text(metadata_title) and not is_stem_fallback:
        title_hints = [metadata_title] + filename_title_hints
    else:
        title_hints = filename_title_hints

    author_hints = list(filename_author_hints)
    header = extracted_metadata.get(TEXT_HEADER_EVIDENCE_KEY)
    if isinstance(header, dict):
        title_hints, author_hints = _apply_text_header_hints(
            header,
            title_hints,
            author_hints,
            protected_authors=comma_form_filename_authors(original_filename),
        )
    metadata_authors = extracted_metadata.get("authors")
    if isinstance(metadata_authors, list):
        for raw_author in metadata_authors:
            if isinstance(raw_author, str):
                normalized_author = raw_author.strip()
                if normalized_author and normalized_author not in author_hints:
                    author_hints.insert(0, normalized_author)

    title_hints, author_hints = _apply_path_layout_hints(
        extracted_metadata,
        list(title_hints),
        list(author_hints),
        has_embedded_title=(
            isinstance(metadata_title, str)
            and bool(normalize_text(metadata_title))
            and not is_stem_fallback
        ),
    )

    title_hints = list(dict.fromkeys([h for h in title_hints if h]))
    author_hints = [h for h in dict.fromkeys(author_hints).keys()] if author_hints else []

    isbns: list[str] = []
    for field_name in ["isbn", "isbn10", "isbn13", "identifier"]:
        value = extracted_metadata.get(field_name)
        if not value:
            continue
        if isinstance(value, str):
            for isbn in normalize_isbns(value):
                if isbn not in isbns:
                    isbns.append(isbn)

    folder_hint = Path(source_path).parent.name
    folder_hints = [folder_hint] if folder_hint else []

    embedded_title = (
        metadata_title
        if isinstance(metadata_title, str) and normalize_text(metadata_title)
        else None
    )
    return IdentificationInput(
        title_hints=title_hints,
        author_hints=author_hints,
        isbns=isbns,
        folder_hints=[h for h in folder_hints if h],
        language=normalize_text(extracted_metadata.get("language")),
        publisher=normalize_text(extracted_metadata.get("publisher")),
        description=extracted_metadata.get("description")
        if isinstance(extracted_metadata.get("description"), str)
        else None,
        publication_year=_metadata_year(extracted_metadata),
        author_evidence=_author_evidence(extracted_metadata, filename_author_hints),
        title_evidence=_title_evidence(
            title_hints,
            embedded_title=None if is_stem_fallback else embedded_title,
            layout_title=_layout_value(extracted_metadata, "title"),
            metadata_title_source=EVIDENCE_SIDECAR
            if title_source == EVIDENCE_SIDECAR
            else EVIDENCE_EMBEDDED,
            header=header if isinstance(header, dict) else {},
        ),
    )


def _layout_value(extracted_metadata: dict[str, Any], key: str) -> Any:
    layout = extracted_metadata.get(PATH_LAYOUT_EVIDENCE_KEY)
    return layout.get(key) if isinstance(layout, dict) else None


def _coauthor_names(raw_names: Any) -> tuple[str, ...]:
    """Real author names from one explicit author list, placeholders removed.

    A single entry that is itself a list ("Terry Pratchett & Neil Gaiman") is split, so
    each coauthor has to be covered on its own.
    """
    names: list[str] = []
    for name in evidence_author_names({"authors": raw_names}):
        names.extend(split_author_list(name) or [name])
    return tuple(dict.fromkeys(names))


def _author_evidence(
    extracted_metadata: dict[str, Any],
    filename_author_hints: list[str],
) -> tuple[AuthorEvidence, ...]:
    """Group the author hints by provenance and by how their names relate.

    Embedded metadata, applied sidecar fields, explicit text headers and configured
    folder layouts name *coauthors*: every name is meant. Filename splits and weak
    header names are *alternatives*. A layout author replaces header/filename guesses,
    as in ``_apply_path_layout_hints``.
    """
    groups: list[AuthorEvidence] = []
    embedded = _coauthor_names(extracted_metadata.get("authors"))
    sidecar = extracted_metadata.get("sidecar_metadata")
    applied = sidecar.get("applied_fields", []) if isinstance(sidecar, dict) else []
    if embedded:
        source = EVIDENCE_SIDECAR if "authors" in applied else EVIDENCE_EMBEDDED
        groups.append(AuthorEvidence(AUTHORS_COAUTHORS, source, embedded))
    layout = _coauthor_names(_layout_value(extracted_metadata, "authors"))
    header = extracted_metadata.get(TEXT_HEADER_EVIDENCE_KEY)
    # Configured layout authors replace header/filename hints in the flat input too.
    if isinstance(header, dict) and not layout:
        explicit = _coauthor_names(header.get("authors"))
        if explicit:
            groups.append(AuthorEvidence(AUTHORS_COAUTHORS, EVIDENCE_HEADER, explicit))
        else:
            likely = _coauthor_names(header.get("likely_authors"))
            if likely:
                groups.append(AuthorEvidence(AUTHORS_ALTERNATIVES, EVIDENCE_HEADER, likely))
    if layout:
        groups.append(AuthorEvidence(AUTHORS_COAUTHORS, EVIDENCE_LAYOUT, layout))
    elif filename_author_hints:
        filename = tuple(dict.fromkeys(filename_author_hints))
        groups.append(AuthorEvidence(AUTHORS_ALTERNATIVES, EVIDENCE_FILENAME, filename))
    return tuple(groups)


def _title_evidence(
    title_hints: list[str],
    *,
    embedded_title: str | None,
    layout_title: Any,
    metadata_title_source: str = EVIDENCE_EMBEDDED,
    header: dict[str, Any] | None = None,
) -> tuple[TitleEvidence, ...]:
    """Each title hint with the strongest source that supplied it."""
    sources: dict[str, str] = {}
    if embedded_title:
        sources[normalize_text(embedded_title)] = metadata_title_source
    for rule in (header or {}).get("rules", []):
        if (
            isinstance(rule, dict)
            and rule.get("rule") in {"labelled title", "gutenberg header", "title by author"}
            and isinstance(rule.get("title"), str)
        ):
            sources.setdefault(normalize_text(rule["title"]), EVIDENCE_HEADER)
    if isinstance(layout_title, str) and normalize_text(layout_title):
        sources.setdefault(normalize_text(layout_title), EVIDENCE_LAYOUT)
    return tuple(
        TitleEvidence(hint, sources.get(normalize_text(hint), EVIDENCE_FILENAME))
        for hint in title_hints
    )


def strongest_title_evidence(signals: IdentificationInput) -> list[TitleEvidence]:
    """The title hints from the strongest source present (all of them, on a tie)."""
    titles = signals.title_sources()
    if not titles:
        return []
    best = max(EVIDENCE_RANK.get(t.source, 0) for t in titles)
    return [t for t in titles if EVIDENCE_RANK.get(t.source, 0) == best]


def _metadata_year(extracted_metadata: dict[str, Any]) -> int | None:
    """Publication year from embedded metadata, when an extractor provides one."""
    for field_name in ("publication_year", "date", "pubdate"):
        value = extracted_metadata.get(field_name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, str):
            match = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", value)
            if match:
                return int(match.group(1))
    return None


def _apply_path_layout_hints(
    extracted_metadata: dict[str, Any],
    title_hints: list[str],
    author_hints: list[str],
    *,
    has_embedded_title: bool,
) -> tuple[list[str], list[str]]:
    """Merge folder-layout hints (evidence, not truth) into the title/author hints.

    A layout title goes after a real embedded title (usually the best signal) and
    before the raw filename guesses; an extractor's filename-stem fallback does not
    count as embedded. A layout author replaces the filename "A - B" split guesses,
    which add *both* halves as authors, but never outranks embedded authors.
    """
    layout = extracted_metadata.get(PATH_LAYOUT_EVIDENCE_KEY)
    if not isinstance(layout, dict):
        return title_hints, author_hints

    layout_title = layout.get("title")
    if isinstance(layout_title, str) and layout_title.strip():
        title_hints.insert(1 if has_embedded_title else 0, layout_title.strip())

    raw_layout_authors = layout.get("authors")
    layout_authors = [
        author.strip()
        for author in (raw_layout_authors if isinstance(raw_layout_authors, list) else [])
        if isinstance(author, str) and author.strip()
    ]
    if layout_authors:
        raw_embedded = extracted_metadata.get("authors")
        embedded = [
            author.strip()
            for author in (raw_embedded if isinstance(raw_embedded, list) else [])
            if isinstance(author, str) and author.strip()
        ]
        known = {normalize_text(author) for author in embedded}
        author_hints = embedded + [a for a in layout_authors if normalize_text(a) not in known]
    return title_hints, author_hints


class OpenLibraryMetadataProvider:
    """Public Open Library metadata provider (primary external source)."""

    provider_id = "openlibrary"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        isbn_error: ProviderRequestError | None = None
        if isbn:
            try:
                isbn_candidate = self._query_isbn(isbn)
            except ProviderRequestError as exc:
                # Still try the text search; the failure is reported only if that
                # finds nothing either, so it is never mistaken for "no results".
                isbn_error = exc
                isbn_candidate = None
            if isbn_candidate:
                return [isbn_candidate]

        # The folder name is only a last-resort hint: for web uploads it is a constant
        # such as "uploads", and adding it to the query makes real titles match nothing.
        query_parts = [part for part in (title, author) if part]
        if not query_parts and folder_hint:
            query_parts = [folder_hint]
        if not query_parts:
            if isbn_error is not None:
                raise isbn_error
            return []
        hits = self._query_search(" ".join(query_parts))
        if not hits and isbn_error is not None:
            raise isbn_error
        return hits

    def _query_isbn(self, isbn: str) -> dict[str, Any] | None:
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        try:
            payload = self._get_json(url)
        except HTTPError as exc:
            if exc.code == 404:  # unknown ISBN: a genuine miss, not a failure
                return None
            raise self._request_error("isbn", exc) from exc
        except (OSError, ValueError) as exc:
            raise self._request_error("isbn", exc) from exc
        if not isinstance(payload, dict):
            return None
        payload = dict(payload)
        payload["_queried_isbn"] = isbn
        names = self._resolve_author_names(payload)
        if names:
            payload["author_name"] = names
        return payload

    def _resolve_author_names(self, payload: dict[str, Any]) -> list[str]:
        """Resolve an edition's author references to names (bounded, best effort).

        Many edition records carry no ``authors`` at all, only ``works: [{key}]``;
        the authors are then taken from the (first) work record.
        """
        keys = _open_library_author_keys(payload.get("authors"))
        if not keys:
            keys = self._work_author_keys(payload.get("works"))
        names: list[str] = []
        for key in keys[:MAX_AUTHOR_LOOKUPS]:
            try:
                author_payload = self._get_json(f"https://openlibrary.org{key}.json")
            except (OSError, ValueError) as exc:
                logger.warning(
                    "metadata_provider_request_failed",
                    provider_id=self.provider_id,
                    operation="author",
                    error_type=type(exc).__name__,
                )
                continue
            name = author_payload.get("name") if isinstance(author_payload, dict) else None
            if isinstance(name, str) and name.strip():
                names.append(name.strip())
        return names

    def _work_author_keys(self, works: Any) -> list[str]:
        """Author keys of the edition's first work record (``/works/OL...W``)."""
        if not isinstance(works, list) or not works:
            return []
        entry = works[0]
        key = entry.get("key") if isinstance(entry, dict) else None
        if not isinstance(key, str) or not re.fullmatch(r"/works/OL\d+W", key):
            return []
        try:
            work_payload = self._get_json(f"https://openlibrary.org{key}.json")
        except (OSError, ValueError) as exc:
            logger.warning(
                "metadata_provider_request_failed",
                provider_id=self.provider_id,
                operation="work",
                error_type=type(exc).__name__,
            )
            return []
        if not isinstance(work_payload, dict):
            return []
        return _open_library_author_keys(work_payload.get("authors"))

    def _query_search(self, query: str) -> list[dict[str, Any]]:
        # Open Library's default search field set omits ``subject``; request it explicitly
        # alongside the default fields the candidate normalizer and scorer rely on.
        encoded = urlencode({"q": query, "limit": "5", "fields": OPEN_LIBRARY_SEARCH_FIELDS})
        url = f"https://openlibrary.org/search.json?{encoded}"
        try:
            payload = self._get_json(url)
        except (OSError, ValueError) as exc:
            raise self._request_error("search", exc) from exc
        docs = payload.get("docs", []) if isinstance(payload, dict) else None
        if not isinstance(docs, list):
            raise self._request_error("search", ProviderRequestError("malformed response"))
        return [doc for doc in docs if isinstance(doc, dict)]

    def _request_error(self, operation: str, exc: BaseException) -> ProviderRequestError:
        reason = describe_provider_error(exc)
        logger.warning(
            "metadata_provider_request_failed",
            provider_id=self.provider_id,
            operation=operation,
            error_type=type(exc).__name__,
            reason=reason,
        )
        return ProviderRequestError(reason)

    def _get_json(self, url: str) -> Any:
        user_agent = str(
            getattr(django_settings, "BOOK_SCANNER_METADATA_USER_AGENT", "") or DEFAULT_USER_AGENT
        )
        for attempt in range(2):
            # Proactive client-side pacing (shared across workers via Redis when configured);
            # the 429 handling below is only the fallback when the server still objects.
            throttle_provider(self.provider_id)
            request = Request(url, headers={"User-Agent": user_agent})
            try:
                with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    if response.status >= 400:
                        raise URLError(f"provider error {response.status}")
                    return json.loads(response.read().decode("utf-8"))
            except HTTPError as exc:
                if exc.code != 429 or attempt == 1:
                    raise
                time.sleep(_retry_after_seconds(exc))
        raise URLError("provider rate limited")  # pragma: no cover - loop always returns/raises


def _open_library_author_keys(raw_authors: Any) -> list[str]:
    """Author keys from ``[{"key": ...}]`` (editions) or ``[{"author": {"key": ...}}]`` (works)."""
    if not isinstance(raw_authors, list):
        return []
    keys: list[str] = []
    for entry in raw_authors:
        if not isinstance(entry, dict):
            continue
        key = entry.get("key")
        nested = entry.get("author")
        if not isinstance(key, str) and isinstance(nested, dict):
            key = nested.get("key")
        if isinstance(key, str) and re.fullmatch(r"/authors/OL\d+A", key) and key not in keys:
            keys.append(key)
    return keys


def _retry_after_seconds(error: HTTPError) -> float:
    """Bounded Retry-After (seconds form only) for a 429 response."""
    raw = error.headers.get("Retry-After") if error.headers else None
    try:
        delay = float(raw) if raw is not None else 1.0
    except ValueError:
        delay = 1.0
    return min(max(delay, 0.0), MAX_RETRY_AFTER_SECONDS)


def _provider_instances() -> list[MetadataProvider]:
    configured = getattr(
        django_settings,
        "BOOK_SCANNER_METADATA_PROVIDERS",
        ["ebook_server.core.identification.OpenLibraryMetadataProvider"],
    )
    providers: list[MetadataProvider] = []
    for provider_ref in configured:
        if provider_ref == "_disabled_":
            continue
        module_path, _, symbol = provider_ref.rpartition(".")
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, symbol)
            provider = cls()
            providers.append(provider)
        except ValueError as exc:
            # A provider that cannot run as configured (e.g. a missing API key) is
            # skipped with a warning instead of failing every lookup.
            logger.warning(
                "metadata_provider_unavailable",
                provider_ref=provider_ref,
                reason=str(exc),
            )
            continue
        except (ImportError, AttributeError, TypeError):
            continue
    return providers


def resolve_candidates(signals: IdentificationInput) -> list[IdentificationCandidate]:
    candidates, _outcomes = resolve_candidates_with_report(signals)
    return candidates


def resolve_candidates_with_report(
    signals: IdentificationInput,
) -> tuple[list[IdentificationCandidate], list[ProviderOutcome]]:
    """Ranked candidates plus what every configured provider did.

    A failing provider never aborts the run: it is reported as ``error`` with a
    short reason, so a reviewer can tell "the provider was down" from "no match".
    """
    raw_candidates: list[tuple[MetadataProvider, dict[str, Any]]] = []
    outcomes: list[ProviderOutcome] = []
    for provider in _provider_instances():
        started_at = perf_counter()
        try:
            raw_list = _query_provider(provider, signals)
        except Exception as exc:  # provider boundary: one provider must not abort intake
            duration = perf_counter() - started_at
            reason = describe_provider_error(exc)
            # Network-level failures are expected; only unexpected ones need a traceback.
            log = logger.warning if isinstance(exc, OSError) else logger.exception
            log(
                "metadata_provider_failed",
                provider_id=provider.provider_id,
                duration_seconds=duration,
                error_type=type(exc).__name__,
                reason=reason,
            )
            outcomes.append(
                ProviderOutcome(
                    provider_id=provider.provider_id,
                    status="error",
                    result_count=0,
                    duration_seconds=duration,
                    error=reason,
                )
            )
            continue
        duration = perf_counter() - started_at
        logger.info(
            "metadata_provider_completed",
            provider_id=provider.provider_id,
            candidate_count=len(raw_list),
            duration_seconds=duration,
        )
        outcomes.append(
            ProviderOutcome(
                provider_id=provider.provider_id,
                status="results" if raw_list else "no_results",
                result_count=len(raw_list),
                duration_seconds=duration,
            )
        )
        for candidate in raw_list:
            raw_candidates.append((provider, candidate))

    normalized: list[IdentificationCandidate] = []
    for provider, payload in raw_candidates:
        normalized.append(_normalize_candidate(provider.provider_id, payload, signals))

    normalized.sort(key=lambda item: item.score, reverse=True)
    return _apply_strong_match_rule(signals, normalized), outcomes


def _query_provider(
    provider: MetadataProvider,
    signals: IdentificationInput,
) -> list[dict[str, Any]]:
    isbn = signals.isbns[0] if signals.isbns else None
    author = signals.best_author()
    folder_hint = signals.folder_hints[0] if signals.folder_hints else None

    for title in signals.title_hints[:3]:
        hits = provider.search(
            title=title,
            author=author,
            isbn=isbn,
            folder_hint=folder_hint,
        )
        if hits:
            return hits
        # The ISBN lookup does not depend on the title; do not repeat it per title hint.
        isbn = None

    if signals.title_hints:
        return []
    # No title hints at all: try identifiers / author / folder alone.
    return provider.search(
        title=None,
        author=author,
        isbn=isbn,
        folder_hint=folder_hint,
    )


def _normalize_candidate(
    provider_id: str,
    payload: dict[str, Any],
    signals: IdentificationInput,
) -> IdentificationCandidate:
    title = payload.get("title") or payload.get("title_suggest") or "Unknown title"
    if not isinstance(title, str):
        title = "Unknown title"

    raw_authors = payload.get("author_name")
    authors = (
        [
            str(value)
            for value in raw_authors
            if isinstance(value, str) and normalize_text(value).strip()
        ]
        if isinstance(raw_authors, list)
        else []
    )

    isbn10 = None
    isbn13 = None
    if isinstance(payload.get("isbn_10"), list) and payload.get("isbn_10"):
        isbn10 = _normalize_identifier(str(payload["isbn_10"][0]))
    elif isinstance(payload.get("isbn_10"), str):
        isbn10 = _normalize_identifier(payload["isbn_10"])

    if isinstance(payload.get("isbn_13"), list) and payload.get("isbn_13"):
        isbn13 = _normalize_identifier(str(payload["isbn_13"][0]))
    elif isinstance(payload.get("isbn_13"), str):
        isbn13 = _normalize_identifier(payload["isbn_13"])

    queried_isbn = _normalize_identifier(str(payload.get("_queried_isbn") or ""))
    if queried_isbn is not None:
        # Edition records can list several ISBNs; the one we looked up must be the one
        # the candidate carries, otherwise the ISBN-match score bonus is lost.
        if len(queried_isbn) == 10:
            isbn10 = queried_isbn
            isbn13 = _isbn10_to_isbn13(queried_isbn)
        else:
            isbn13 = queried_isbn
            isbn10 = next(
                (
                    value
                    for value in _as_str_list(payload.get("isbn_10"))
                    if (normalized := _normalize_identifier(value))
                    and len(normalized) == 10
                    and _isbn10_to_isbn13(normalized) == queried_isbn
                ),
                None,
            )
            if isbn10 is not None:
                isbn10 = _normalize_identifier(isbn10)

    publisher = None
    raw_publisher = payload.get("publisher") or payload.get("publishers")
    if isinstance(raw_publisher, list) and raw_publisher:
        publisher = normalize_text(str(raw_publisher[0]))
    elif isinstance(raw_publisher, str):
        publisher = normalize_text(raw_publisher)

    publication_year = None
    raw_publish = payload.get("first_publish_year")
    if raw_publish is None and isinstance(payload.get("publish_date"), str):
        year_match = re.search(r"\b(1[0-9]{3}|20[0-9]{2})\b", payload["publish_date"])
        if year_match:
            raw_publish = int(year_match.group(1))
    if isinstance(raw_publish, int):
        publication_year = raw_publish
    elif isinstance(raw_publish, str) and raw_publish.isdigit():
        publication_year = int(raw_publish)

    language: str | None = None
    raw_language = payload.get("language")
    if isinstance(raw_language, str):
        language = normalize_text(raw_language)
    elif isinstance(raw_language, list) and raw_language:
        first_language = raw_language[0]
        if isinstance(first_language, str):
            language = normalize_text(first_language)

    candidate = IdentificationCandidate(
        provider_id=provider_id,
        provider_ref=str(payload.get("key") or payload.get("id") or payload.get("isbn") or ""),
        title=title,
        authors=authors,
        isbn10=isbn10,
        isbn13=isbn13,
        language=language,
        description=payload.get("subtitle") if isinstance(payload.get("subtitle"), str) else None,
        publisher=publisher,
        edition=payload.get("edition_name")
        if isinstance(payload.get("edition_name"), str)
        else None,
        publication_year=publication_year,
        score=0.0,
        raw=payload,
        # Open Library search docs use ``subject``, edition records ``subjects``;
        # ``categories`` is the Google Books volumeInfo shape.
        subjects=normalize_subjects(
            _as_str_list(payload.get("subject"))
            + _as_str_list(payload.get("subjects"))
            + _as_str_list(payload.get("categories"))
        ),
    )
    return _apply_scoring(signals, candidate)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


def _title_match_score(left: str, right: str) -> float:
    """Similarity of two titles in [0, 1], penalising tokens only one side has.

    ``token_set_ratio`` alone scores 1.0 whenever one title's tokens are a subset of
    the other's ("Foundation" vs "Foundation and Earth"), which are different books.
    Averaging it with ``token_sort_ratio`` keeps word-order tolerance while making
    unmatched tokens cost score; only genuinely equal titles reach 1.0.
    """
    left_processed = default_process(left)
    right_processed = default_process(right)
    if not left_processed or not right_processed:
        return 0.0
    set_score = token_set_ratio(left_processed, right_processed)
    sort_score = token_sort_ratio(left_processed, right_processed)
    return (set_score + sort_score) / 200.0


def _author_name_tokens(name: str) -> list[str]:
    """Lowercased, punctuation-free name tokens with "Last, First" reordered."""
    if name.count(",") == 1:
        last, first = name.split(",")
        name = f"{first} {last}"
    return default_process(name).split()


def _given_names_compatible(left: list[str], right: list[str]) -> bool:
    """True when the shorter given-name list aligns, in order, with the longer one.

    Tokens align when equal, when one is a single-letter initial of the other, or when
    they are close spellings of the same name; unmatched tokens in the longer list
    (a missing middle name or initial) are skipped.
    """
    shorter, longer = (left, right) if len(left) <= len(right) else (right, left)
    position = 0
    for token in shorter:
        while position < len(longer):
            candidate = longer[position]
            position += 1
            if (
                token == candidate
                or (len(token) == 1 and candidate.startswith(token))
                or (len(candidate) == 1 and token.startswith(candidate))
                or ratio(token, candidate) >= AUTHOR_GIVEN_NAME_MIN_RATIO
            ):
                break
        else:
            return False
    return True


def _author_name_similarity(left: str, right: str) -> float:
    """1.0 for the same name, a partial score for compatible variants, else 0.0.

    Variants share the surname and have compatible given names: "Iain M. Banks" vs
    "Iain Banks", "H. P. Lovecraft" vs "H P Lovecraft", "Banks, Iain" vs "Iain Banks".
    A shared surname alone ("Frank Herbert" vs "Brian Herbert") is not a match.
    """
    left_tokens = _author_name_tokens(left)
    right_tokens = _author_name_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    # Equal up to spacing too: "HP Lovecraft" / "H P Lovecraft", "Le Guin" / "LeGuin".
    if "".join(left_tokens) == "".join(right_tokens):
        return 1.0
    if left_tokens[-1] != right_tokens[-1]:
        return 0.0
    left_given = left_tokens[:-1]
    right_given = right_tokens[:-1]
    if not left_given or not right_given:
        return 0.0
    if _given_names_compatible(left_given, right_given):
        return AUTHOR_VARIANT_MATCH_SCORE
    return 0.0


def _author_match_score(left: list[str], right: list[str]) -> float:
    """Best similarity between any author hint and any candidate author.

    Author hints are alternatives, not a list that must all match: an ambiguous
    "Stephen King - Pet Sematary" filename yields both halves as hints (#15), and only
    one of them can be the author. Averaging over hints would halve the score of the
    right candidate merely because the filename did not say which half is which.
    """
    if not left or not right:
        return 0.0
    left_names = [name for name in dict.fromkeys(normalize_text(a) for a in left) if name]
    right_names = [name for name in dict.fromkeys(normalize_text(a) for a in right) if name]
    if not left_names or not right_names:
        return 0.0
    return max(_author_name_similarity(name, other) for name in left_names for other in right_names)


def _author_coverage(names: list[str] | tuple[str, ...], candidate_authors: list[str]) -> float:
    """Share of ``names`` the candidate covers (each name by its best similarity).

    For coauthor lists every name counts: [Terry Pratchett, Neil Gaiman] against a
    candidate naming only Terry Pratchett covers half of the book's authors.
    """
    wanted = [name for name in dict.fromkeys(normalize_text(a) for a in names) if name]
    offered = [name for name in dict.fromkeys(normalize_text(a) for a in candidate_authors) if name]
    if not wanted or not offered:
        return 0.0
    total = sum(max(_author_name_similarity(name, other) for other in offered) for name in wanted)
    return total / len(wanted)


def _group_score(group: AuthorEvidence, candidate_authors: list[str]) -> float:
    if group.kind == AUTHORS_COAUTHORS:
        return _author_coverage(group.names, candidate_authors)
    return _author_match_score(list(group.names), candidate_authors)


def author_evidence_score(signals: IdentificationInput, candidate_authors: list[str]) -> float:
    """How well a candidate's authors match the file's typed author evidence.

    Alternative groups contribute their best single match. A coauthor group the
    candidate matches at all decides the score by its coverage, so a candidate that
    omits a genuine coauthor cannot be lifted back to full credit by a filename guess
    naming only one of them. A coauthor group the candidate does not match at all is
    left to the author-conflict guard (#92) and does not zero out the filename hints.
    """
    best_alternative = 0.0
    matched_coverage: list[float] = []
    for group in signals.author_groups():
        score = _group_score(group, candidate_authors)
        if group.kind == AUTHORS_COAUTHORS:
            if score > 0:
                matched_coverage.append(score)
        else:
            best_alternative = max(best_alternative, score)
    if matched_coverage:
        return min(matched_coverage)
    return best_alternative


def missing_coauthors(signals: IdentificationInput, candidate_authors: list[str]) -> list[str]:
    """Genuine coauthors (from coauthor groups) the candidate does not name.

    Only groups the candidate matches at least partly are checked; a group it does not
    match at all is an author conflict, not a missing coauthor.
    """
    offered = [normalize_text(a) for a in candidate_authors if normalize_text(a)]
    missing: list[str] = []
    for group in signals.author_groups():
        if group.kind != AUTHORS_COAUTHORS or _group_score(group, candidate_authors) <= 0:
            continue
        for name in group.names:
            key = normalize_text(name)
            if key and not any(_author_name_similarity(key, other) > 0 for other in offered):
                missing.append(name)
    return list(dict.fromkeys(missing))


def title_similarity(left: str, right: str) -> float:
    """Public form of the candidate title score, for comparing two metadata sources."""
    return _title_match_score(left, right)


def author_similarity(left: list[str], right: list[str]) -> float:
    """Public form of the candidate author score (0.0 when no name corresponds)."""
    return _author_match_score(left, right)


# Creator values that say nothing about who wrote the book: office-suite and PDF
# tool defaults, account names, and "unknown" in a few languages. Compared after
# normalize_text().
_PLACEHOLDER_AUTHORS = frozenset(
    {
        "unknown",
        "unknown author",
        "unbekannt",
        "unbekannter autor",
        "inconnu",
        "anonymous",
        "anonym",
        "anon",
        "na",
        "administrator",
        "admin",
        "user",
        "owner",
        "author",
        "n/a",
        "none",
        "various",
        "various authors",
        "multiple authors",
        "microsoft office user",
        "microsoft word",
        "windows user",
        "default",
    }
)


def is_placeholder_author(name: str) -> bool:
    """True for creator values such as "Microsoft Office User" that name nobody."""
    return normalize_text(name).strip(" .,;:-_") in _PLACEHOLDER_AUTHORS


def evidence_author_names(metadata: dict[str, Any]) -> list[str]:
    """Author names the file's own embedded metadata gives, without placeholders.

    This is evidence, not truth: it is only used to check a provider match and to
    suggest authors to a reviewer, never written onto a Book automatically.
    """
    raw_authors = metadata.get("authors")
    if not isinstance(raw_authors, list):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for value in raw_authors:
        if not isinstance(value, str):
            continue
        name = " ".join(value.split()).strip(" ,;")
        key = normalize_text(name)
        if not key or is_placeholder_author(name) or key in seen:
            continue
        seen.add(key)
        names.append(name)
    return names


def _author_names_agree(left: str, right: str) -> bool:
    """Same person per the scorer's name matching, or a bare surname on one side."""
    if _author_name_similarity(normalize_text(left), normalize_text(right)) > 0:
        return True
    left_tokens = _author_name_tokens(normalize_text(left))
    right_tokens = _author_name_tokens(normalize_text(right))
    if not left_tokens or not right_tokens:
        return False
    # "Tolkien" vs "J. R. R. Tolkien": a surname-only credit is not a contradiction.
    single_token = len(left_tokens) == 1 or len(right_tokens) == 1
    return single_token and left_tokens[-1] == right_tokens[-1]


def authors_conflict(candidate_authors: list[str], evidence_authors: list[str]) -> bool:
    """True when both sides name authors and none of them is the same person.

    Names are compared with the scorer's author matching (``_author_name_similarity``),
    so "Banks, Iain" and "Iain M. Banks" agree while "Frank Herbert" and "Brian
    Herbert" do not. Missing authors on either side are not a conflict.
    """
    candidates = [name for name in candidate_authors if normalize_text(name)]
    evidence = [name for name in evidence_authors if normalize_text(name)]
    if not candidates or not evidence:
        return False
    return not any(
        _author_names_agree(candidate, other) for candidate in candidates for other in evidence
    )


def _apply_scoring(
    signals: IdentificationInput,
    candidate: IdentificationCandidate,
) -> IdentificationCandidate:
    score = 0.0

    if signals.isbns:
        isbn_matches = {
            value for value in signals.isbns if value in {candidate.isbn10, candidate.isbn13}
        }
        if isbn_matches:
            score += 0.95

    title_scores = [
        _title_match_score(title_hint, candidate.title) for title_hint in signals.title_hints
    ]
    if title_scores:
        score += 0.55 * max(title_scores)

    author_score = author_evidence_score(signals, candidate.authors)
    score += 0.3 * author_score

    if signals.language and candidate.language and signals.language == candidate.language:
        score += 0.05

    if signals.publisher and candidate.publisher and signals.publisher == candidate.publisher:
        score += 0.05

    if signals.description and candidate.description:
        normalized_signal = normalize_text(signals.description)
        if normalized_signal and normalized_signal in normalize_text(candidate.description):
            score += 0.05

    normalized_candidate = IdentificationCandidate(
        provider_id=candidate.provider_id,
        provider_ref=candidate.provider_ref,
        title=candidate.title,
        authors=candidate.authors,
        isbn10=candidate.isbn10,
        isbn13=candidate.isbn13,
        language=candidate.language,
        description=candidate.description,
        publisher=candidate.publisher,
        edition=candidate.edition,
        publication_year=candidate.publication_year,
        score=min(score, 1.0),
        raw=candidate.raw,
        subjects=candidate.subjects,
    )
    return normalized_candidate


def best_candidate(
    candidates: list[IdentificationCandidate],
) -> IdentificationCandidate | None:
    if not candidates:
        return None
    if candidates[0].score < REVIEW_SCORE:
        return candidates[0]
    return candidates[0]


def score_candidate(
    signals: IdentificationInput,
    candidate: IdentificationCandidate,
) -> IdentificationCandidate:
    """Score any candidate (e.g. an existing library Book) with the provider scorer."""
    return _apply_scoring(signals, candidate)


def same_book(left: IdentificationCandidate, right: IdentificationCandidate) -> bool:
    """Whether two candidates describe the same book.

    Shared ISBN, or near-equal titles with compatible authors (authors are only
    compared when both sides list some).
    """
    left_isbns = {value for value in (left.isbn10, left.isbn13) if value}
    right_isbns = {value for value in (right.isbn10, right.isbn13) if value}
    expanded_left = {isbn for value in left_isbns for isbn in normalize_isbns(value)}
    expanded_right = {isbn for value in right_isbns for isbn in normalize_isbns(value)}
    if expanded_left & expanded_right:
        return True
    if _title_match_score(left.title, right.title) < SAME_BOOK_TITLE_MIN_SCORE:
        return False
    if left.authors and right.authors:
        return _author_coverage(left.authors, right.authors) >= AUTHOR_VARIANT_MATCH_SCORE
    return True


@dataclass(frozen=True)
class StrongMatchSettings:
    enabled: bool
    min_title_score: float
    min_author_score: float
    tie_margin: float
    year_tolerance: int


def _strong_match_settings() -> StrongMatchSettings:
    def setting(name: str, default: Any) -> Any:
        return getattr(django_settings, f"BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_{name}", default)

    return StrongMatchSettings(
        enabled=bool(setting("ENABLED", True)),
        min_title_score=float(setting("MIN_TITLE", 1.0)),
        min_author_score=float(setting("MIN_AUTHOR", 0.9)),
        tie_margin=float(setting("TIE_MARGIN", 0.05)),
        year_tolerance=int(setting("YEAR_TOLERANCE", 2)),
    )


def _normalize_language(value: str) -> str:
    code = normalize_text(value).replace("_", "-").split("-")[0]
    return _LANGUAGE_ALIASES.get(code, code)


def _candidate_languages(candidate: IdentificationCandidate) -> set[str]:
    values = _as_str_list(candidate.raw.get("language"))
    if candidate.language:
        values.append(candidate.language)
    return {_normalize_language(value) for value in values if normalize_text(value)}


def _candidate_isbns(candidate: IdentificationCandidate) -> set[str]:
    values = [candidate.isbn10 or "", candidate.isbn13 or ""]
    values.extend(_as_str_list(candidate.raw.get("isbn")))
    found: set[str] = set()
    for value in values:
        found.update(normalize_isbns(value))
    return found


def _strong_match_assessment(
    signals: IdentificationInput,
    candidate: IdentificationCandidate,
    config: StrongMatchSettings,
) -> dict[str, Any]:
    """Check one candidate against the exact-title + strong-author rule.

    The author side uses the typed author evidence (``author_evidence_score``):
    filename guesses are alternatives, so the dilution a filename split causes
    ("Author - Title" yields both halves as author hints) costs nothing, while
    embedded/layout coauthors must all be covered. A candidate that omits a genuine
    coauthor, or matches none of an explicit author list, is never promoted. The
    matched title and author must come from different hints, and the matched title
    must not be contradicted by stronger title evidence (an embedded title beats a
    filename guess).
    """
    title_score, title_hint = _matched_title_hint(signals, candidate)
    author_names = [name for group in signals.author_groups() for name in group.names]
    author_hint = max(
        author_names,
        key=lambda name: _author_match_score([name], candidate.authors),
        default="",
    )
    author_score = author_evidence_score(signals, candidate.authors)
    missing = missing_coauthors(signals, candidate.authors)
    title_source, strongest_source, title_agrees = _title_provenance(signals, title_hint, candidate)
    blocked_by: list[str] = []
    if title_score < config.min_title_score:
        blocked_by.append("title_not_exact")
    if not title_agrees:
        blocked_by.append(TITLE_EVIDENCE_CONFLICT)
    if author_score < config.min_author_score:
        blocked_by.append("author_not_strong")
    if missing:
        blocked_by.append("missing_coauthor")
    if _explicit_authors_unmatched(signals, candidate.authors):
        blocked_by.append("author_evidence_conflict")
    if title_hint and default_process(title_hint) == default_process(author_hint):
        blocked_by.append("title_and_author_from_same_hint")

    if signals.isbns:
        candidate_isbns = _candidate_isbns(candidate)
        if not candidate_isbns:
            blocked_by.append("file_isbn_unconfirmed")
        elif not candidate_isbns.intersection(signals.isbns):
            blocked_by.append("conflicting_isbn")

    if signals.language:
        candidate_languages = _candidate_languages(candidate)
        if candidate_languages and _normalize_language(signals.language) not in candidate_languages:
            blocked_by.append("conflicting_language")

    if (
        signals.publication_year is not None
        and candidate.publication_year is not None
        and signals.publication_year < candidate.publication_year - config.year_tolerance
    ):
        # A file dated before the work's first publication is a different book; a later
        # year is normal for reprints and is not treated as a conflict.
        blocked_by.append("publication_year_mismatch")

    return {
        "title_score": round(title_score, 4),
        "title_hint": title_hint,
        "title_source": title_source,
        "strongest_title_source": strongest_source,
        "author_score": round(author_score, 4),
        "author_hint": author_hint,
        "missing_coauthors": missing,
        "blocked_by": blocked_by,
    }


def _title_forms(title: str) -> list[str]:
    """A title plus its cheap variants and its " - " segments, for agreement checks."""
    forms = _title_variants(title)
    segments = [s.strip() for s in _FILENAME_SEPARATOR.split(title) if s.strip()]
    return list(dict.fromkeys([*forms, *(s for s in segments if len(segments) > 1)]))


def _title_provenance(
    signals: IdentificationInput,
    title_hint: str,
    candidate: IdentificationCandidate,
) -> tuple[str, str, bool]:
    """Source of the matched title hint, the strongest title source, and whether they agree.

    The matched hint may promote only when it is the strongest title evidence, or when
    the strongest evidence names the candidate's title too (same title, a subtitle
    variant, or a " - " segment of it). "Dune - Frank Herbert.epub" whose embedded
    title is "The Dragon in the Sea" does not agree with a Dune candidate.
    """
    titles = signals.title_sources()
    source = next((t.source for t in titles if t.title == title_hint), EVIDENCE_UNKNOWN)
    strongest = strongest_title_evidence(signals)
    if not strongest:
        return source, source, True
    strongest_source = strongest[0].source
    if EVIDENCE_RANK.get(source, 0) >= EVIDENCE_RANK.get(strongest_source, 0):
        return source, strongest_source, True
    agrees = any(
        _title_match_score(form, candidate.title) >= SAME_BOOK_TITLE_MIN_SCORE
        for evidence in strongest
        for form in _title_forms(evidence.title)
    )
    return source, strongest_source, agrees


def _matched_title_hint(
    signals: IdentificationInput, candidate: IdentificationCandidate
) -> tuple[float, str]:
    """The title hint that best matches the candidate's title, with its match score."""
    return max(
        ((_title_match_score(hint, candidate.title), hint) for hint in signals.title_hints),
        default=(0.0, ""),
    )


# The candidate's title matches only weaker title evidence (e.g. a filename guess)
# while stronger evidence (e.g. the embedded title) names another book.
TITLE_EVIDENCE_CONFLICT = "stronger_title_evidence_disagrees"


def title_evidence_conflict(
    signals: IdentificationInput,
    candidate: IdentificationCandidate,
) -> dict[str, Any] | None:
    """Why stronger title evidence contradicts ``candidate``, or ``None`` when it does not.

    Shared by the strong-match rule (before promotion) and by the intake auto-accept
    decision, which runs it on every candidate at or above the threshold however the
    score got there (title + author + language/publisher bonuses, an ISBN, or the
    rule). "Dune - Frank Herbert.epub" embedding "The Dragon in the Sea" is never
    accepted as Dune: the Dune match comes from the filename only.
    """
    _, title_hint = _matched_title_hint(signals, candidate)
    title_source, strongest_source, agrees = _title_provenance(signals, title_hint, candidate)
    if agrees:
        return None
    return {
        "candidate_title": candidate.title,
        "title_hint": title_hint,
        "title_source": title_source,
        "strongest_title_source": strongest_source,
        "strongest_titles": [evidence.title for evidence in strongest_title_evidence(signals)],
    }


def _explicit_authors_unmatched(signals: IdentificationInput, candidate_authors: list[str]) -> bool:
    """True when an explicit (coauthor) author list names nobody the candidate names.

    Same test as the intake author-conflict guard (#92, ``authors_conflict``), applied
    to embedded and folder-layout author lists before a score is promoted.
    """
    named = [a for a in candidate_authors if not is_placeholder_author(a)]
    return any(
        group.kind == AUTHORS_COAUTHORS and authors_conflict(named, list(group.names))
        for group in signals.author_groups()
    )


def _apply_strong_match_rule(
    signals: IdentificationInput,
    candidates: list[IdentificationCandidate],
) -> list[IdentificationCandidate]:
    """Let an unambiguous exact-title + strong-author match reach auto-accept.

    Title and author alone score at most 0.85, below the auto-accept threshold, so
    books without ISBN, language or publisher metadata (pre-ISBN books, plain-text
    files) could never be accepted automatically. When the top candidate passes
    ``_strong_match_assessment`` with no conflicting signal, and no other candidate is
    near-tied with it or also passes, its score is raised to the auto-accept
    threshold. The decision and its inputs are recorded in the candidate's raw payload
    under ``STRONG_MATCH_EVIDENCE_KEY`` whether or not the rule applied.
    """
    if not candidates:
        return candidates
    config = _strong_match_settings()
    top = candidates[0]
    if not config.enabled or top.score >= AUTO_ACCEPT_SCORE:
        return candidates

    assessment = _strong_match_assessment(signals, top, config)
    blocked_by = list(assessment["blocked_by"])
    runner_up_score = candidates[1].score if len(candidates) > 1 else None
    if runner_up_score is not None and top.score - runner_up_score <= config.tie_margin:
        blocked_by.append("near_tied_candidate")
    elif any(
        not _strong_match_assessment(signals, other, config)["blocked_by"]
        for other in candidates[1:]
    ):
        blocked_by.append("multiple_qualifying_candidates")

    applied = not blocked_by
    evidence = {
        "rule": STRONG_MATCH_RULE,
        "applied": applied,
        "blocked_by": blocked_by,
        "base_score": round(top.score, 4),
        "auto_accept_threshold": AUTO_ACCEPT_SCORE,
        "runner_up_score": round(runner_up_score, 4) if runner_up_score is not None else None,
        "tie_margin": config.tie_margin,
        "title_score": assessment["title_score"],
        "title_hint": assessment["title_hint"],
        "author_score": assessment["author_score"],
        "author_hint": assessment["author_hint"],
        "title_source": assessment["title_source"],
        "strongest_title_source": assessment["strongest_title_source"],
        "missing_coauthors": assessment["missing_coauthors"],
    }
    if applied:
        logger.info(
            "identification_strong_match_applied",
            rule=STRONG_MATCH_RULE,
            provider_id=top.provider_id,
            base_score=top.score,
        )
    promoted = replace(
        top,
        score=max(top.score, AUTO_ACCEPT_SCORE) if applied else top.score,
        raw={**top.raw, STRONG_MATCH_EVIDENCE_KEY: evidence},
    )
    return [promoted, *candidates[1:]]
