"""Map subject tags to Genres and attach them to Books with provenance (#50).

Decisions (see docs/metadata-providers.md, "Genres and categories"):

- Tags are matched to an existing ``Genre`` name or ``GenreAlias`` by ``genre_key``
  (case/accent/punctuation/space folding). Unknown tags are never turned into new
  genres; they stay evidence until an admin adds a genre or alias.
- Provider subjects of the accepted candidate are preferred; embedded file subjects
  are used only when the provider's tags map to no genre.
- Automatic assignment only adds links. It never removes links and never runs on a
  Book that already has reviewer- or admin-set genres.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from django.db import transaction

from .models import Book, BookGenre, Genre, GenreAlias, genre_key

CURATED_SOURCES: frozenset[str] = frozenset({BookGenre.Source.REVIEWER, BookGenre.Source.ADMIN})

_SOURCE_TAG_LENGTH = 255

# Trailing hierarchy segments that carry no genre information ("Fiction / General").
_GENERIC_SEGMENTS = frozenset({"general"})


@dataclass(frozen=True)
class TagMatch:
    tag: str
    genre: Genre | None


@dataclass
class GenreSuggestion:
    """Genres suggested for one candidate, and which evidence they came from."""

    source: str = ""
    matches: list[TagMatch] = field(default_factory=list)

    @property
    def genres(self) -> list[Genre]:
        seen: dict[int, Genre] = {}
        for match in self.matches:
            if match.genre is not None:
                seen.setdefault(match.genre.pk, match.genre)
        return list(seen.values())

    @property
    def genre_ids(self) -> list[int]:
        return [genre.pk for genre in self.genres]


def build_index() -> dict[str, Genre]:
    """Lookup key -> Genre for every genre name and alias. Genre names win clashes."""
    index: dict[str, Genre] = {}
    for alias in GenreAlias.objects.select_related("genre"):
        if alias.key:
            index[alias.key] = alias.genre
    for genre in Genre.objects.all():
        key = genre_key(genre.name)
        if key:
            index[key] = genre
    return index


def _strip_generic_tail(parts: list[str]) -> list[str]:
    while len(parts) > 1 and genre_key(parts[-1]) in _GENERIC_SEGMENTS:
        parts = parts[:-1]
    return parts


def lookup_keys(tag: str) -> list[str]:
    """Keys to try for ``tag``, most specific first.

    Besides the tag itself: trailing "General" segments are dropped ("Fiction / Science
    Fiction / General", "Fiction, science fiction, general"), and BISAC-style "/"
    hierarchies are walked up one level at a time ("FICTION / Science Fiction / Space
    Opera" -> "FICTION / Science Fiction" -> "FICTION"). Comma-separated Open Library
    strings are *not* truncated further, because "Science fiction, history and
    criticism" is not a science fiction book.
    """
    keys: list[str] = []

    def add(text: str) -> None:
        key = genre_key(text)
        if key and key not in keys:
            keys.append(key)

    add(tag)
    add(",".join(_strip_generic_tail([part.strip() for part in tag.split(",")])))
    levels = _strip_generic_tail([part.strip() for part in tag.split("/") if part.strip()])
    for end in range(len(levels), 0, -1):
        add("/".join(levels[:end]))
    return keys


def resolve_tags(tags: Iterable[Any], index: dict[str, Genre] | None = None) -> list[TagMatch]:
    """Map each tag to a genre (or ``None``), keeping the input order."""
    if index is None:
        index = build_index()
    matches: list[TagMatch] = []
    for tag in tags:
        if not isinstance(tag, str) or not tag.strip():
            continue
        genre = next((index[key] for key in lookup_keys(tag) if key in index), None)
        matches.append(TagMatch(tag=tag.strip(), genre=genre))
    return matches


def suggest(
    provider_subjects: Sequence[Any] | None,
    embedded_subjects: Sequence[Any] | None,
    index: dict[str, Genre] | None = None,
) -> GenreSuggestion:
    """Genres for a candidate: its provider tags, else the file's embedded tags."""
    if index is None:
        index = build_index()
    provider_matches = resolve_tags(_as_list(provider_subjects), index)
    if any(match.genre is not None for match in provider_matches):
        return GenreSuggestion(source=BookGenre.Source.PROVIDER, matches=provider_matches)
    embedded_matches = resolve_tags(_as_list(embedded_subjects), index)
    if any(match.genre is not None for match in embedded_matches):
        return GenreSuggestion(source=BookGenre.Source.EMBEDDED, matches=embedded_matches)
    # Nothing maps: still report the provider tags (or the file's) so reviewers see why.
    return GenreSuggestion(source="", matches=provider_matches or embedded_matches)


def embedded_subjects(evidence: Any) -> list[str]:
    if not isinstance(evidence, dict):
        return []
    return [tag for tag in _as_list(evidence.get("subjects")) if isinstance(tag, str)]


def has_curated_genres(book: Book) -> bool:
    return BookGenre.objects.filter(book=book, source__in=CURATED_SOURCES).exists()


@transaction.atomic
def assign_automatic(
    book: Book,
    *,
    provider_subjects: Sequence[Any] | None,
    embedded_subjects: Sequence[Any] | None,
) -> list[Genre]:
    """Attach suggested genres to ``book``; returns the genres newly linked."""
    if has_curated_genres(book):
        return []
    suggestion = suggest(provider_subjects, embedded_subjects)
    added: list[Genre] = []
    seen: set[int] = set()
    for match in suggestion.matches:
        if match.genre is None or match.genre.pk in seen:
            continue
        seen.add(match.genre.pk)
        _, created = BookGenre.objects.get_or_create(
            book=book,
            genre=match.genre,
            defaults={
                "source": suggestion.source,
                "source_tag": match.tag[:_SOURCE_TAG_LENGTH],
            },
        )
        if created:
            added.append(match.genre)
    return added


@transaction.atomic
def assign_reviewer(
    book: Book,
    genres: Iterable[Genre],
    *,
    suggestion: GenreSuggestion | None = None,
) -> list[Genre]:
    """Link the reviewer's chosen genres; automatic links to them become reviewer links.

    ``suggestion`` only supplies the tag a confirmed genre came from. Admin links are
    left untouched, and genres the reviewer did not choose are not removed from an
    existing Book (other files of it may justify them).
    """
    tags: dict[int, str] = {}
    for match in suggestion.matches if suggestion is not None else []:
        if match.genre is not None:
            tags.setdefault(match.genre.pk, match.tag[:_SOURCE_TAG_LENGTH])
    linked: list[Genre] = []
    for genre in genres:
        link, created = BookGenre.objects.get_or_create(
            book=book,
            genre=genre,
            defaults={
                "source": BookGenre.Source.REVIEWER,
                "source_tag": tags.get(genre.pk, ""),
            },
        )
        if not created and link.source not in CURATED_SOURCES:
            link.source = BookGenre.Source.REVIEWER
            link.save(update_fields=["source"])
        linked.append(genre)
    return linked


def genre_from_filter(value: str) -> Genre | None:
    """The genre a browse filter value names, through names or aliases."""
    key = genre_key(value)
    if not key:
        return None
    return build_index().get(key)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list | tuple):
        return list(value)
    return []
