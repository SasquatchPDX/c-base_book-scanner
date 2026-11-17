"""Bibliographic identification helpers and metadata provider abstraction."""

from __future__ import annotations

import importlib
import json
import re
import time
from dataclasses import dataclass, field
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

from .rate_limit import throttle_provider

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


@dataclass(frozen=True)
class IdentificationInput:
    title_hints: list[str]
    author_hints: list[str]
    isbns: list[str]
    folder_hints: list[str]
    language: str | None
    publisher: str | None
    description: str | None

    def best_title(self) -> str | None:
        return self.title_hints[0] if self.title_hints else None

    def best_author(self) -> str | None:
        return self.author_hints[0] if self.author_hints else None


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
_PLACEHOLDER_AUTHORS = frozenset(
    {"unknown", "anonymous", "anon", "various", "unbekannt", "n/a", "na", "none"},
)
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


def invert_last_first_name(value: str) -> str | None:
    """ "Corey, James S. A." -> "James S. A. Corey" when it reads as a person's name.

    Returns ``None`` for anything else, including the library-sort title form
    "Hobbit, The" and comma titles such as "Python Development, Second Edit".
    """
    if value.count(",") != 1:
        return None
    last, first = (part.strip() for part in value.split(","))
    if not last or not first or first.lower() in _TITLE_ARTICLES:
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


def is_placeholder_author(value: str) -> bool:
    """ "Unknown", "Anonymous", ...: an explicit "no author" marker, not a name."""
    return value.strip().lower().rstrip(".") in _PLACEHOLDER_AUTHORS


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
    is_stem_fallback = isinstance(metadata_title, str) and normalize_text(
        metadata_title,
    ) == normalize_text(Path(original_filename).stem)
    if isinstance(metadata_title, str) and normalize_text(metadata_title) and not is_stem_fallback:
        title_hints = [metadata_title] + filename_title_hints
    else:
        title_hints = filename_title_hints

    author_hints = filename_author_hints
    metadata_authors = extracted_metadata.get("authors")
    if isinstance(metadata_authors, list):
        for raw_author in metadata_authors:
            if isinstance(raw_author, str):
                normalized_author = raw_author.strip()
                if normalized_author and normalized_author not in author_hints:
                    author_hints.insert(0, normalized_author)

    title_hints, author_hints = _apply_path_layout_hints(
        extracted_metadata, list(title_hints), list(author_hints)
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
    )


def _apply_path_layout_hints(
    extracted_metadata: dict[str, Any],
    title_hints: list[str],
    author_hints: list[str],
) -> tuple[list[str], list[str]]:
    """Merge folder-layout hints (evidence, not truth) into the title/author hints.

    A layout title goes after an embedded title (usually the best signal) and before
    the raw filename guesses. A layout author replaces the filename "A - B" split
    guesses, which add *both* halves as authors, but never outranks embedded authors.
    """
    layout = extracted_metadata.get(PATH_LAYOUT_EVIDENCE_KEY)
    if not isinstance(layout, dict):
        return title_hints, author_hints

    metadata_title = extracted_metadata.get("title")
    has_embedded_title = isinstance(metadata_title, str) and bool(normalize_text(metadata_title))
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
        if isbn:
            isbn_candidate = self._query_isbn(isbn)
            if isbn_candidate:
                return [isbn_candidate]

        # The folder name is only a last-resort hint: for web uploads it is a constant
        # such as "uploads", and adding it to the query makes real titles match nothing.
        query_parts = [part for part in (title, author) if part]
        if not query_parts and folder_hint:
            query_parts = [folder_hint]
        if not query_parts:
            return []
        return self._query_search(" ".join(query_parts))

    def _query_isbn(self, isbn: str) -> dict[str, Any] | None:
        url = f"https://openlibrary.org/isbn/{isbn}.json"
        try:
            payload = self._get_json(url)
        except (OSError, ValueError) as exc:
            logger.warning(
                "metadata_provider_request_failed",
                provider_id=self.provider_id,
                operation="isbn",
                error_type=type(exc).__name__,
            )
            return None
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
            logger.warning(
                "metadata_provider_request_failed",
                provider_id=self.provider_id,
                operation="search",
                error_type=type(exc).__name__,
            )
            return []
        docs = payload.get("docs", []) if isinstance(payload, dict) else []
        return docs if isinstance(docs, list) else []

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
    raw_candidates: list[tuple[MetadataProvider, dict[str, Any]]] = []
    for provider in _provider_instances():
        started_at = perf_counter()
        try:
            raw_list = _query_provider(provider, signals)
        except Exception as exc:  # provider boundary: one provider must not abort intake
            logger.exception(
                "metadata_provider_failed",
                provider_id=provider.provider_id,
                duration_seconds=perf_counter() - started_at,
                error_type=type(exc).__name__,
            )
            continue
        logger.info(
            "metadata_provider_completed",
            provider_id=provider.provider_id,
            candidate_count=len(raw_list),
            duration_seconds=perf_counter() - started_at,
        )
        for candidate in raw_list:
            raw_candidates.append((provider, candidate))

    normalized: list[IdentificationCandidate] = []
    for provider, payload in raw_candidates:
        normalized.append(_normalize_candidate(provider.provider_id, payload, signals))

    normalized.sort(key=lambda item: item.score, reverse=True)
    return normalized


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

    author_score = _author_match_score(signals.author_hints, candidate.authors)
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
        return _author_match_score(left.authors, right.authors) >= AUTHOR_VARIANT_MATCH_SCORE
    return True
