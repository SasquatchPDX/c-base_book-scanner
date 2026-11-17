"""Library search and browse at scale (#28, phase 1).

Three parts:

- **Search documents.** `index_books` rewrites one denormalised
  `BookSearchDocument` row (plus its `BookSearchFacet` rows) per Book from the
  Book, its authors, identifiers, genres, categories and COMPLETE file
  variants. Model signals queue the affected book ids and index them once,
  after the surrounding transaction commits; `rebuild_search_index` recreates
  every row.
- **Backends** behind the small `SearchBackend` protocol, so an external engine
  (Meilisearch) could replace PostgreSQL later without touching the views:
  `PostgresSearchBackend` uses full-text search (`simple` configuration over
  accent- and case-folded text), `pg_trgm` substring and fuzzy matching, and
  the B-tree/GIN indexes of migration 0032. `DatabaseFallbackSearchBackend`
  is the phase 0 `icontains` query over `Book`, used on SQLite (development and
  unit tests) and on PostgreSQL until the index has been built.
- **Keyset pagination.** Browse sorts (newest, oldest, title A-Z/Z-A) page with
  signed `(sort key, id)` cursors, constant time at any depth. Relevance-ranked
  search pages by offset, capped at `MAX_RANKED_RESULTS`.

Query text is never logged.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Final, Protocol

import structlog
from django.contrib.postgres.search import SearchQuery, SearchRank, TrigramWordSimilarity
from django.core import signing
from django.core.cache import cache
from django.db import connection, transaction
from django.db.models import Count, F, Q, QuerySet
from django.db.models.functions import Greatest
from django.db.models.signals import m2m_changed, post_delete, post_save, pre_delete

from . import genres
from .models import (
    Author,
    Book,
    BookGenre,
    BookSearchDocument,
    BookSearchFacet,
    Category,
    CoverImage,
    FileVariant,
    Genre,
    Identifier,
)

logger = structlog.get_logger(__name__)

DEFAULT_SORT: Final = "-created_at"
RELEVANCE: Final = "relevance"
# Browse sorts: name -> (Book field for the fallback, search-document field, descending).
BROWSE_SORTS: Final[dict[str, tuple[str, str, bool]]] = {
    "-created_at": ("created_at", "book_created_at", True),
    "created_at": ("created_at", "book_created_at", False),
    "title": ("title", "title_sort", False),
    "-title": ("title", "title_sort", True),
}
FACETS: Final = ("language", "format", "genre", "category", "author", "decade")
MAX_RANKED_RESULTS: Final = 1000
FACET_LIMIT: Final = 50
# Global (unfiltered) counts barely change between imports; the design accepts
# up to five minutes of staleness for them (#28, Q3).
GLOBAL_CACHE_SECONDS: Final = 300
INDEX_BATCH_SIZE: Final = 2000
DESCRIPTION_INDEX_CHARS: Final = 20_000
FUZZY_WORD_SIMILARITY: Final = 0.45

_CURSOR_SALT: Final = "ebook_server.library.books.cursor"
_READY_CACHE_KEY: Final = "library-search:index-ready:v1"
_COUNT_CACHE_KEY: Final = "library-search:total:v1"
_FACET_CACHE_KEY: Final = "library-search:facets:v1:{facet}"
_ISBN_LIKE = re.compile(r"[0-9Xx][0-9Xx-]{8,16}[0-9Xx]")
_TOKEN = re.compile(r"[^\W_]+")


# --------------------------------------------------------------------------- text


def fold(text: str | None) -> str:
    """Accent-, case- and whitespace-fold text the same way for documents and queries.

    Folding happens in Python, not with the `unaccent` extension, so the stored
    columns, the query text and any future external engine see exactly the same
    normalisation ("Straße" and "Strasse", "Émile" and "emile" are equal).
    """
    if not text:
        return ""
    if text.isascii():  # the common case, and much faster
        return " ".join(text.lower().split())
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return " ".join(stripped.casefold().split())


def _compact_identifier(value: str) -> str:
    return re.sub(r"[^0-9a-z]", "", fold(value))


def query_tokens(query: str) -> list[str]:
    """Search tokens of a free-text query; ISBN-like chunks keep their digits together."""
    tokens: list[str] = []
    for chunk in fold(query).split():
        if _ISBN_LIKE.fullmatch(chunk):
            tokens.append(_compact_identifier(chunk))
        else:
            tokens.extend(_TOKEN.findall(chunk))
    return tokens


def _tsquery_text(tokens: Sequence[str]) -> str:
    """AND of the tokens, with the last one as a prefix ("java patt" finds "patterns")."""
    terms = [*tokens[:-1], f"{tokens[-1]}:*"]
    return " & ".join(terms)


# ---------------------------------------------------------------------- documents


@dataclass
class _BuiltDocument:
    document: BookSearchDocument
    facets: list[BookSearchFacet]
    weighted_text: tuple[str, str, str, str]


@dataclass
class BookSource:
    """The indexed fields of one Book and its relations, read as plain values.

    Indexing reads values rather than model instances: a full rebuild touches
    every book, author link and variant, and instantiating them all dominated
    the rebuild time.
    """

    book_id: int
    title: str
    subtitle: str
    publisher: str
    language: str
    description: str
    publication_year: int | None
    publication_date: date | None
    front_cover_id: int | None
    created_at: datetime
    authors: list[tuple[int, str]] = field(default_factory=list)
    genres: list[tuple[int, str]] = field(default_factory=list)
    categories: list[tuple[int, str]] = field(default_factory=list)
    identifiers: list[tuple[str, str, str]] = field(default_factory=list)
    formats: set[str] = field(default_factory=set)

    @property
    def year(self) -> int | None:
        if self.publication_year is not None:
            return self.publication_year
        return self.publication_date.year if self.publication_date else None


_ISBN_KINDS: Final = frozenset(
    {Identifier.IdentifierType.ISBN10.value, Identifier.IdentifierType.ISBN13.value}
)


def build_document(source: BookSource) -> _BuiltDocument:
    """Search row, facet rows and weighted FTS text for one book."""
    authors = sorted(source.authors, key=lambda author: author[1])
    author_names = [fold(name) for _pk, name in authors]
    formats = sorted({fold(value) for value in source.formats if value})
    year = source.year
    language = fold(source.language)[:32]
    book_id = source.book_id

    document = BookSearchDocument(
        book_id=book_id,
        title_sort=fold(source.title)[:255],
        author_sort=author_names[0][:255] if author_names else "",
        # Newline-separated so a substring match cannot span two names.
        author_text="\n".join(author_names),
        language=language,
        publication_decade=(year // 10) * 10 if year else None,
        has_cover=source.front_cover_id is not None,
        book_created_at=source.created_at,
    )
    facets = [
        BookSearchFacet(book_id=book_id, kind=BookSearchFacet.Kind.FORMAT, value=value[:64])
        for value in formats
    ]
    facets += [
        BookSearchFacet(book_id=book_id, kind=kind, value=str(related_pk))
        for kind, rows in (
            (BookSearchFacet.Kind.AUTHOR, authors),
            (BookSearchFacet.Kind.GENRE, source.genres),
            (BookSearchFacet.Kind.CATEGORY, source.categories),
        )
        for related_pk in sorted({pk for pk, _name in rows})
    ]
    identifier_text = " ".join(
        _compact_identifier(normalized or value) if kind in _ISBN_KINDS else fold(value)
        for kind, value, normalized in source.identifiers
    )
    weighted_text = (
        # A: what a member most likely types; B: who wrote it; C: classification; D: blurb.
        " ".join([fold(source.title), fold(source.subtitle), identifier_text]),
        " ".join(author_names),
        " ".join(
            [
                fold(source.publisher),
                language,
                *formats,
                *(fold(name) for _pk, name in source.genres),
                *(fold(name) for _pk, name in source.categories),
            ]
        ),
        fold(source.description[:DESCRIPTION_INDEX_CHARS]),
    )
    document.content_digest = content_digest(weighted_text)
    return _BuiltDocument(document=document, facets=facets, weighted_text=weighted_text)


def content_digest(weighted_text: Sequence[str]) -> str:
    """SHA-256 of the canonical weighted FTS text of one book.

    The `tsvector` exists only on PostgreSQL and loses the input text, so the
    digest is what lets `verify_search_index` see drift in the full-text content
    (a changed description, publisher or identifier) on every database.
    """
    canonical = json.dumps(list(weighted_text), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _book_sources(book_ids: Sequence[int]) -> list[BookSource]:
    """Read everything `build_document` needs for these books in six queries."""
    sources = {
        row[0]: BookSource(*row)
        for row in Book.objects.filter(pk__in=book_ids)
        .order_by()
        .values_list(
            "id",
            "title",
            "subtitle",
            "publisher",
            "language",
            "description",
            "publication_year",
            "publication_date",
            "front_cover_id",
            "created_at",
        )
    }
    ids = list(sources)
    for book_id, author_id, name in (
        Book.authors.through.objects.filter(book_id__in=ids)
        .order_by()
        .values_list("book_id", "author_id", "author__full_name")
    ):
        sources[book_id].authors.append((author_id, name))
    for book_id, genre_id, name in (
        Book.genres.through.objects.filter(book_id__in=ids)
        .order_by()
        .values_list("book_id", "genre_id", "genre__name")
    ):
        sources[book_id].genres.append((genre_id, name))
    for book_id, category_id, name in (
        Book.categories.through.objects.filter(book_id__in=ids)
        .order_by()
        .values_list("book_id", "category_id", "category__name")
    ):
        sources[book_id].categories.append((category_id, name))
    for book_id, kind, value, normalized in (
        Identifier.objects.filter(book_id__in=ids)
        .order_by()
        .values_list("book_id", "kind", "value", "normalized_value")
    ):
        sources[book_id].identifiers.append((kind, value, normalized))
    for book_id, book_format in (
        FileVariant.objects.filter(
            book_id__in=ids, processing_state=FileVariant.ProcessingState.COMPLETE
        )
        .order_by()
        .values_list("book_id", "format")
    ):
        sources[book_id].formats.add(book_format)
    return list(sources.values())


_DOCUMENT_UPDATE_FIELDS: Final = [
    "title_sort",
    "author_sort",
    "author_text",
    "language",
    "publication_decade",
    "has_cover",
    "book_created_at",
    "content_digest",
    "indexed_at",
]


def index_books(book_ids: Iterable[int]) -> int:
    """(Re)write the search rows of these books; rows of deleted books are removed.

    Idempotent and safe to call for any id. Returns the number of books indexed.
    """
    pending = sorted({int(book_id) for book_id in book_ids})
    indexed = 0
    for start in range(0, len(pending), INDEX_BATCH_SIZE):
        chunk = pending[start : start + INDEX_BATCH_SIZE]
        sources = _book_sources(chunk)
        built = [build_document(source) for source in sources]
        found = {source.book_id for source in sources}
        own_transaction = not connection.in_atomic_block
        with transaction.atomic():
            if own_transaction and connection.vendor == "postgresql":
                # Only derived, rebuildable rows are written here: a crash may lose
                # the last moments of them, never Book data, and skipping the
                # flush wait per batch roughly halves a rebuild.
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL synchronous_commit TO OFF")
            gone = set(chunk) - found
            if gone:
                BookSearchDocument.objects.filter(book_id__in=gone).delete()
            if built:
                if connection.vendor == "postgresql":
                    _upsert_documents_postgresql(built)
                else:
                    BookSearchDocument.objects.bulk_create(
                        [item.document for item in built],
                        update_conflicts=True,
                        unique_fields=["book"],
                        update_fields=_DOCUMENT_UPDATE_FIELDS,
                    )
            _sync_facets(chunk, [facet for item in built for facet in item.facets])
        indexed += len(built)
    return indexed


def _sync_facets(book_ids: Sequence[int], wanted: Sequence[BookSearchFacet]) -> None:
    """Make the facet rows of these books equal `wanted`, touching only the differences.

    A nightly rebuild of an unchanged library then reads facets but writes none.
    """
    wanted_keys = {(facet.book_id, facet.kind, facet.value) for facet in wanted}
    stale: list[int] = []
    existing: set[tuple[int, str, str]] = set()
    for pk, book_id, kind, value in BookSearchFacet.objects.filter(
        book_id__in=book_ids
    ).values_list("pk", "book_id", "kind", "value"):
        key = (book_id, kind, value)
        if key in wanted_keys:
            existing.add(key)
        else:
            stale.append(pk)
    if stale:
        BookSearchFacet.objects.filter(pk__in=stale).delete()
    missing = [
        facet for facet in wanted if (facet.book_id, facet.kind, facet.value) not in existing
    ]
    if missing:
        BookSearchFacet.objects.bulk_create(missing)


# The weighted `tsvector` of one book from its four weighted text columns. Used by
# the upsert and, to detect vector drift, by `verify_search_index`.
_VECTOR_SQL: Final = """
    setweight(to_tsvector('simple', src.a), 'A')
    || setweight(to_tsvector('simple', src.b), 'B')
    || setweight(to_tsvector('simple', src.c), 'C')
    || setweight(to_tsvector('simple', src.d), 'D')
"""


def _upsert_documents_postgresql(built: Sequence[_BuiltDocument]) -> None:
    """Insert or update a batch of rows, weighted `tsvector` included, in one statement.

    Computing the vector in the same statement writes each row (and its GIN
    entries) once instead of inserting it and then updating the vector.
    """
    columns: list[list[Any]] = [[] for _ in range(13)]
    for item in built:
        document = item.document
        values = (
            document.book_id,
            *item.weighted_text,
            document.title_sort,
            document.author_sort,
            document.author_text,
            document.language,
            document.publication_decade,
            document.has_cover,
            document.book_created_at,
            document.content_digest,
        )
        for column, value in zip(columns, values, strict=True):
            column.append(value)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            INSERT INTO core_booksearchdocument (
                book_id, document, title_sort, author_sort, author_text, language,
                publication_decade, has_cover, book_created_at, content_digest, indexed_at
            )
            SELECT src.book_id, {_VECTOR_SQL},
                   src.title_sort, src.author_sort, src.author_text, src.language,
                   src.decade, src.has_cover, src.created_at, src.digest, now()
              FROM unnest(
                   %s::bigint[], %s::text[], %s::text[], %s::text[], %s::text[],
                   %s::text[], %s::text[], %s::text[], %s::text[],
                   %s::smallint[], %s::boolean[], %s::timestamptz[], %s::text[]
                   ) AS src(book_id, a, b, c, d, title_sort, author_sort, author_text,
                            language, decade, has_cover, created_at, digest)
            ON CONFLICT (book_id) DO UPDATE SET
                   document = EXCLUDED.document,
                   title_sort = EXCLUDED.title_sort,
                   author_sort = EXCLUDED.author_sort,
                   author_text = EXCLUDED.author_text,
                   language = EXCLUDED.language,
                   publication_decade = EXCLUDED.publication_decade,
                   has_cover = EXCLUDED.has_cover,
                   book_created_at = EXCLUDED.book_created_at,
                   content_digest = EXCLUDED.content_digest,
                   indexed_at = EXCLUDED.indexed_at
             -- Rewrite (and re-index) only rows whose content changed; `indexed_at`
             -- is therefore the time of the last change.
             WHERE (core_booksearchdocument.document, core_booksearchdocument.title_sort,
                    core_booksearchdocument.author_sort, core_booksearchdocument.author_text,
                    core_booksearchdocument.language,
                    core_booksearchdocument.publication_decade,
                    core_booksearchdocument.has_cover, core_booksearchdocument.book_created_at,
                    core_booksearchdocument.content_digest)
                   IS DISTINCT FROM
                   (EXCLUDED.document, EXCLUDED.title_sort, EXCLUDED.author_sort,
                    EXCLUDED.author_text, EXCLUDED.language, EXCLUDED.publication_decade,
                    EXCLUDED.has_cover, EXCLUDED.book_created_at, EXCLUDED.content_digest)
            """,
            columns,
        )


def rebuild_search_index(*, batch_size: int = INDEX_BATCH_SIZE) -> int:
    """Rebuild every search row (management command and nightly job)."""
    del batch_size  # index_books batches internally; kept for the command's interface.
    BookSearchDocument.objects.exclude(book_id__in=Book.objects.values("pk")).delete()
    total = 0
    last_id = 0
    while True:
        ids = list(
            Book.objects.filter(pk__gt=last_id)
            .order_by("pk")
            .values_list("pk", flat=True)[: INDEX_BATCH_SIZE * 10]
        )
        if not ids:
            break
        total += index_books(ids)
        last_id = ids[-1]
    invalidate_cached_counts()
    cache.set(_READY_CACHE_KEY, True, GLOBAL_CACHE_SECONDS)
    return total


@dataclass
class IndexReport:
    missing_book_ids: list[int]
    orphan_count: int
    sampled: int
    drifted_book_ids: list[int]

    @property
    def ok(self) -> bool:
        return not (self.missing_book_ids or self.orphan_count or self.drifted_book_ids)


def _comparable(document: BookSearchDocument, facets: Iterable[BookSearchFacet]) -> tuple[Any, ...]:
    return (
        document.title_sort,
        document.author_sort,
        document.author_text,
        document.language,
        document.publication_decade,
        document.has_cover,
        document.book_created_at,
        document.content_digest,
        sorted((facet.kind, facet.value) for facet in facets),
    )


def _vector_drift(built: Sequence[_BuiltDocument]) -> set[int]:
    """Books whose stored PostgreSQL `tsvector` differs from a freshly computed one.

    Catches a vector written by other means (a manual repair, a changed text
    search configuration) even when the stored digest still matches.
    """
    if connection.vendor != "postgresql" or not built:
        return set()
    columns: list[list[Any]] = [[] for _ in range(5)]
    for item in built:
        for column, value in zip(
            columns, (item.document.book_id, *item.weighted_text), strict=True
        ):
            column.append(value)
    with connection.cursor() as cursor:
        cursor.execute(
            f"""
            SELECT src.book_id
              FROM unnest(%s::bigint[], %s::text[], %s::text[], %s::text[], %s::text[])
                   AS src(book_id, a, b, c, d)
              JOIN core_booksearchdocument stored ON stored.book_id = src.book_id
             WHERE stored.document IS DISTINCT FROM ({_VECTOR_SQL})
            """,
            columns,
        )
        return {row[0] for row in cursor.fetchall()}


def verify_search_index(*, sample: int = 500) -> IndexReport:
    """Compare the search rows with their books: missing, orphaned and drifted rows.

    Drift is checked on a random sample of `sample` books (all of them when the
    library is smaller) by rebuilding their rows in memory and comparing every
    column, the facets and the digest of the weighted full-text content; on
    PostgreSQL the stored `tsvector` is also compared with a freshly computed one.
    """
    missing = list(
        Book.objects.filter(search_document__isnull=True)
        .order_by("pk")
        .values_list("pk", flat=True)[:1000]
    )
    orphan_count = BookSearchDocument.objects.exclude(book_id__in=Book.objects.values("pk")).count()
    sample_ids = list(
        BookSearchDocument.objects.order_by("?").values_list("book_id", flat=True)[:sample]
    )
    stored = BookSearchDocument.objects.in_bulk(sample_ids)
    stored_facets: dict[int, list[BookSearchFacet]] = {}
    for facet in BookSearchFacet.objects.filter(book_id__in=sample_ids):
        stored_facets.setdefault(facet.book_id, []).append(facet)
    drifted: set[int] = set()
    rebuilt = [build_document(source) for source in _book_sources(sample_ids)]
    for built in rebuilt:
        book_id = built.document.book_id
        current = stored.get(book_id)
        if current is None or _comparable(current, stored_facets.get(book_id, [])) != _comparable(
            built.document, built.facets
        ):
            drifted.add(book_id)
    drifted |= _vector_drift(rebuilt)
    return IndexReport(
        missing_book_ids=missing,
        orphan_count=orphan_count,
        sampled=len(sample_ids),
        drifted_book_ids=sorted(drifted),
    )


def invalidate_cached_counts() -> None:
    cache.delete_many(
        [_COUNT_CACHE_KEY, _READY_CACHE_KEY, *(_FACET_CACHE_KEY.format(facet=f) for f in FACETS)]
    )


def search_index_ready() -> bool:
    """Whether every Book has a search row, so the PostgreSQL backend sees them all.

    Cached for a few minutes; the fallback answers until `rebuild_search_index`
    has run once after migration 0032.
    """
    ready = cache.get(_READY_CACHE_KEY)
    if ready is None:
        ready = not Book.objects.filter(search_document__isnull=True).exists()
        cache.set(_READY_CACHE_KEY, ready, GLOBAL_CACHE_SECONDS if ready else 60)
        if not ready:
            logger.warning("library_search_index_incomplete", backend="fallback")
    return bool(ready)


# ---------------------------------------------------------------- maintenance


class _PendingBooks(threading.local):
    def __init__(self) -> None:
        self.book_ids: set[int] = set()


_pending = _PendingBooks()


def schedule_reindex(book_ids: Iterable[int | None]) -> None:
    """Index these books once the current transaction commits (immediately outside one).

    Calls within one transaction coalesce: a book saved, given three authors and
    two variants in one import is indexed once. Call it *after* the write it
    reflects: in autocommit mode `on_commit` runs the reindex right away.
    """
    ids = {int(book_id) for book_id in book_ids if book_id}
    if not ids:
        return
    _pending.book_ids.update(ids)
    transaction.on_commit(_flush_pending)


def _flush_pending() -> None:
    ids = set(_pending.book_ids)
    _pending.book_ids.clear()
    if not ids:
        return
    try:
        index_books(ids)
    except Exception:  # never fail the write that triggered indexing
        cache.delete(_READY_CACHE_KEY)
        logger.exception("library_search_index_failed", book_count=len(ids))


def _on_book_saved(sender: type[Book], instance: Book, **kwargs: Any) -> None:
    schedule_reindex([instance.pk])


def _on_book_relation_changed(
    sender: type[Any],
    instance: Any,
    action: str,
    reverse: bool,
    pk_set: set[int] | None,
    **kwargs: Any,
) -> None:
    if not reverse:
        if action in ("post_add", "post_remove", "post_clear"):
            schedule_reindex([instance.pk])
        return
    # Reverse side: `instance` is an Author/Genre/Category and pk_set holds book ids.
    if action in ("post_add", "post_remove"):
        schedule_reindex(pk_set or ())
    elif action == "pre_clear":
        schedule_reindex(instance.books.values_list("pk", flat=True))


def _on_related_entity_saved(
    sender: type[Any], instance: Any, created: bool, **kwargs: Any
) -> None:
    # A renamed author/genre/category changes the text of every book that uses it.
    if not created:
        schedule_reindex(instance.books.values_list("pk", flat=True))


def _on_related_entity_deleted(sender: type[Any], instance: Any, **kwargs: Any) -> None:
    # The m2m rows disappear with the entity, without m2m_changed signals.
    schedule_reindex(list(instance.books.values_list("pk", flat=True)))


def _on_book_child_changed(sender: type[Any], instance: Any, **kwargs: Any) -> None:
    schedule_reindex([instance.book_id])


def connect_signals() -> None:
    """Keep search rows current on every ORM write (called from `CoreConfig.ready`).

    Queryset `.update()`/`bulk_*` calls bypass signals; callers that change
    indexed data that way call `schedule_reindex` themselves.
    """
    uid = "library-search"
    post_save.connect(_on_book_saved, sender=Book, dispatch_uid=f"{uid}-book")
    for relation in (Book.authors.through, Book.genres.through, Book.categories.through):
        m2m_changed.connect(
            _on_book_relation_changed,
            sender=relation,
            dispatch_uid=f"{uid}-m2m-{relation.__name__}",
        )
    for entity in (Author, Genre, Category):
        post_save.connect(
            _on_related_entity_saved, sender=entity, dispatch_uid=f"{uid}-save-{entity.__name__}"
        )
        pre_delete.connect(
            _on_related_entity_deleted,
            sender=entity,
            dispatch_uid=f"{uid}-delete-{entity.__name__}",
        )
    # `BookGenre` is the explicit genre link model (#50): `genres.assign_*` and the
    # admin inline create, re-source and delete its rows directly, without m2m_changed.
    for child in (FileVariant, Identifier, CoverImage, BookGenre):
        post_save.connect(
            _on_book_child_changed, sender=child, dispatch_uid=f"{uid}-save-{child.__name__}"
        )
        post_delete.connect(
            _on_book_child_changed, sender=child, dispatch_uid=f"{uid}-delete-{child.__name__}"
        )


# -------------------------------------------------------------------- cursors


class InvalidCursorError(ValueError):
    """A `cursor` parameter was tampered with, expired in format, or names another sort."""


@dataclass(frozen=True)
class Cursor:
    sort: str
    key: str
    book_id: int
    backward: bool = False

    def encode(self) -> str:
        payload = {"s": self.sort, "k": self.key, "i": self.book_id, "b": int(self.backward)}
        return signing.dumps(payload, salt=_CURSOR_SALT, compress=True)

    @classmethod
    def decode(cls, token: str, *, sort: str) -> Cursor:
        try:
            payload = signing.loads(token, salt=_CURSOR_SALT)
        except signing.BadSignature as exc:
            raise InvalidCursorError("bad signature") from exc
        if not isinstance(payload, dict):
            raise InvalidCursorError("malformed")
        key, book_id, backward = payload.get("k"), payload.get("i"), payload.get("b")
        if payload.get("s") != sort:
            raise InvalidCursorError("cursor belongs to another sort")
        if not isinstance(key, str) or not isinstance(book_id, int) or backward not in (0, 1):
            raise InvalidCursorError("malformed")
        return cls(sort=sort, key=key, book_id=book_id, backward=bool(backward))


def _key_to_text(value: Any) -> str:
    return value.isoformat() if isinstance(value, datetime) else str(value)


def _key_from_text(field_name: str, text: str) -> Any:
    if field_name in ("created_at", "book_created_at"):
        try:
            return datetime.fromisoformat(text)
        except ValueError as exc:
            raise InvalidCursorError("malformed key") from exc
    return text


# ------------------------------------------------------------------ requests


@dataclass(frozen=True)
class SearchRequest:
    query: str = ""
    title: str = ""
    author: str = ""
    category: str = ""
    genre: str = ""
    language: str = ""
    book_format: str = ""
    sort: str = ""
    cursor: str = ""
    page: int | None = None
    page_size: int = 25

    @property
    def has_filters(self) -> bool:
        return any(
            (
                self.query,
                self.title,
                self.author,
                self.category,
                self.genre,
                self.language,
                self.book_format,
            )
        )

    def effective_sort(self, *, ranked: bool) -> str:
        """Relevance only where ranking exists; otherwise newest first (#28 Q4)."""
        if self.sort in BROWSE_SORTS:
            return self.sort
        if self.query and ranked and self.sort in ("", RELEVANCE):
            return RELEVANCE
        return DEFAULT_SORT


@dataclass
class SearchPage:
    book_ids: list[int]
    count: int
    sort: str
    page: int | None = None
    next_cursor: str | None = None
    previous_cursor: str | None = None
    fuzzy: bool = False
    backend: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FacetCount:
    value: str
    count: int


class SearchBackend(Protocol):
    """What the library views need from a search engine.

    PostgreSQL implements it today; a Meilisearch backend would implement the
    same methods, with `index_books` pushing documents to the engine.
    """

    name: str

    def search(self, request: SearchRequest) -> SearchPage: ...

    def facet_counts(
        self, request: SearchRequest, facets: Sequence[str]
    ) -> dict[str, list[FacetCount]]: ...

    def index_books(self, book_ids: Iterable[int]) -> int: ...

    def rebuild(self) -> int: ...


def _keyset_page(
    queryset: QuerySet[Any],
    *,
    request: SearchRequest,
    sort: str,
    key_field: str,
    id_field: str,
    descending: bool,
) -> tuple[list[int], str | None, str | None]:
    """One page of `(key, id)` keyset pagination over `queryset`.

    The condition `key <= v AND (key < v OR id < i)` (mirrored for ascending)
    is equivalent to the row comparison `(key, id) < (v, i)`, but the leading
    `key <= v` lets PostgreSQL start an index range scan right at the cursor.
    """
    cursor = Cursor.decode(request.cursor, sort=sort) if request.cursor else None
    backward = cursor.backward if cursor else False
    # Walking backwards reverses the comparison and the order, then the rows.
    walk_descending = descending != backward
    if cursor is not None:
        key = _key_from_text(key_field, cursor.key)
        if walk_descending:
            queryset = queryset.filter(
                Q(**{f"{key_field}__lte": key})
                & (Q(**{f"{key_field}__lt": key}) | Q(**{f"{id_field}__lt": cursor.book_id}))
            )
        else:
            queryset = queryset.filter(
                Q(**{f"{key_field}__gte": key})
                & (Q(**{f"{key_field}__gt": key}) | Q(**{f"{id_field}__gt": cursor.book_id}))
            )
    prefix = "-" if walk_descending else ""
    rows = list(
        queryset.order_by(f"{prefix}{key_field}", f"{prefix}{id_field}").values_list(
            key_field, id_field
        )[: request.page_size + 1]
    )
    has_more = len(rows) > request.page_size
    rows = rows[: request.page_size]
    if backward:
        rows.reverse()
    has_next = has_more if not backward else True
    has_previous = (cursor is not None) if not backward else has_more
    next_cursor = previous_cursor = None
    if rows and has_next:
        last_key, last_id = rows[-1]
        next_cursor = Cursor(sort, _key_to_text(last_key), last_id).encode()
    if rows and has_previous:
        first_key, first_id = rows[0]
        previous_cursor = Cursor(sort, _key_to_text(first_key), first_id, backward=True).encode()
    return [row_id for _key, row_id in rows], next_cursor, previous_cursor


def _offset_window(request: SearchRequest, *, cap: int | None = None) -> tuple[int, int, int]:
    page = max(request.page or 1, 1)
    if cap is not None:
        last_page = max((cap + request.page_size - 1) // request.page_size, 1)
        page = min(page, last_page)
    start = (page - 1) * request.page_size
    stop = start + request.page_size
    if cap is not None:
        stop = min(stop, cap)
    return page, start, stop


# ------------------------------------------------------------------ fallback


def _book_ids_with_author(name: str) -> QuerySet[Any]:
    return Book.authors.through.objects.filter(author__full_name__icontains=name).values("book_id")


def _genre_filter_ids(text: str) -> QuerySet[Any]:
    """Ids of the genres a `genre` filter selects (#50), as a lazy subquery.

    A genre name or alias ("sci-fi") selects exactly that genre; anything else is
    a substring match on genre names, as before.
    """
    match = genres.genre_from_filter(text)
    selected = (
        Genre.objects.filter(pk=match.pk)
        if match is not None
        else Genre.objects.filter(name__icontains=text)
    )
    return selected.values_list("pk", flat=True)


def legacy_filtered_books(request: SearchRequest) -> QuerySet[Book]:
    """The phase 0 `icontains` filters over `Book` (#28): no joins, no DISTINCT.

    Filters over to-many relations are uncorrelated `pk IN (subquery)`
    conditions, so no row is duplicated and the count stays a plain
    `COUNT(*)` over `core_book`. Each relation filter matches on its own:
    `?q=` and `?author=` may match different authors of the same book.
    """
    books = Book.objects.all()
    if request.query:
        books = books.filter(
            Q(title__icontains=request.query)
            | Q(subtitle__icontains=request.query)
            | Q(description__icontains=request.query)
            | Q(pk__in=_book_ids_with_author(request.query)),
        )
    if request.title:
        books = books.filter(title__icontains=request.title)
    if request.author:
        books = books.filter(pk__in=_book_ids_with_author(request.author))
    if request.category:
        books = books.filter(
            pk__in=Book.categories.through.objects.filter(
                category__name__icontains=request.category,
            ).values("book_id"),
        )
    if request.genre:
        books = books.filter(
            pk__in=BookGenre.objects.filter(genre_id__in=_genre_filter_ids(request.genre)).values(
                "book_id"
            ),
        )
    if request.language:
        books = books.filter(language__iexact=request.language)
    if request.book_format:
        books = books.filter(
            pk__in=FileVariant.objects.filter(
                format__iexact=request.book_format,
                processing_state=FileVariant.ProcessingState.COMPLETE,
            ).values("book_id"),
        )
    return books


def _named_counts(rows: Iterable[tuple[Any, int]]) -> list[FacetCount]:
    return [FacetCount(value=str(value), count=count) for value, count in rows if value]


class DatabaseFallbackSearchBackend:
    """Portable `icontains` search over `Book` (SQLite, or before the first rebuild)."""

    name = "fallback"

    def search(self, request: SearchRequest) -> SearchPage:
        sort = request.effective_sort(ranked=False)
        books = legacy_filtered_books(request)
        book_field, _document_field, descending = BROWSE_SORTS[sort]
        count = books.count()
        if request.page is not None:
            page, start, stop = _offset_window(request)
            prefix = "-" if descending else ""
            ids = list(
                books.order_by(f"{prefix}{book_field}", f"{prefix}id").values_list("id", flat=True)[
                    start:stop
                ]
            )
            return SearchPage(book_ids=ids, count=count, sort=sort, page=page, backend=self.name)
        ids, next_cursor, previous_cursor = _keyset_page(
            books,
            request=request,
            sort=sort,
            key_field=book_field,
            id_field="id",
            descending=descending,
        )
        return SearchPage(
            book_ids=ids,
            count=count,
            sort=sort,
            page=None if request.cursor else 1,
            next_cursor=next_cursor,
            previous_cursor=previous_cursor,
            backend=self.name,
        )

    def facet_counts(
        self, request: SearchRequest, facets: Sequence[str]
    ) -> dict[str, list[FacetCount]]:
        book_ids = legacy_filtered_books(request).values("pk")
        rows: Any
        results: dict[str, list[FacetCount]] = {}
        for facet in facets:
            if facet == "language":
                rows = (
                    Book.objects.filter(pk__in=book_ids)
                    .values_list("language")
                    .annotate(n=Count("pk"))
                    .order_by("-n", "language")
                )
            elif facet == "format":
                rows = (
                    FileVariant.objects.filter(
                        book_id__in=book_ids,
                        processing_state=FileVariant.ProcessingState.COMPLETE,
                    )
                    .values_list("format")
                    .annotate(n=Count("book_id", distinct=True))
                    .order_by("-n", "format")
                )
            elif facet in ("genre", "category", "author"):
                relation: Any = {
                    "genre": Book.genres.through,
                    "category": Book.categories.through,
                    "author": Book.authors.through,
                }[facet]
                name_field = f"{facet}__full_name" if facet == "author" else f"{facet}__name"
                rows = (
                    relation.objects.filter(book_id__in=book_ids)
                    .values_list(name_field)
                    .annotate(n=Count("book_id"))
                    .order_by("-n", name_field)
                )
            elif facet == "decade":
                results[facet] = _decade_counts(
                    Book.objects.filter(pk__in=book_ids).values_list(
                        "publication_year", "publication_date"
                    )
                )
                continue
            else:
                continue
            results[facet] = _named_counts(rows[:FACET_LIMIT])
        return results

    def index_books(self, book_ids: Iterable[int]) -> int:
        return index_books(book_ids)

    def rebuild(self) -> int:
        return rebuild_search_index()


def _decade_counts(rows: Iterable[tuple[int | None, Any]]) -> list[FacetCount]:
    counts: dict[int, int] = {}
    for year, full_date in rows:
        value = year if year is not None else (full_date.year if full_date else None)
        if value:
            decade = (value // 10) * 10
            counts[decade] = counts.get(decade, 0) + 1
    ordered = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [FacetCount(value=str(decade), count=n) for decade, n in ordered[:FACET_LIMIT]]


# ------------------------------------------------------------------ postgres


def _facet_book_ids(kind: str, values: Iterable[str]) -> QuerySet[Any]:
    return BookSearchFacet.objects.filter(kind=kind, value__in=list(values)).values("book_id")


class PostgresSearchBackend:
    """Full-text, trigram and keyset search over `BookSearchDocument`."""

    name = "postgresql"

    def _filtered(self, request: SearchRequest) -> QuerySet[BookSearchDocument]:
        documents = BookSearchDocument.objects.all()
        if request.title:
            documents = documents.filter(title_sort__contains=fold(request.title))
        if request.author:
            documents = documents.filter(author_text__contains=fold(request.author))
        # Taxonomy tables are small; resolve the names to ids first.
        if request.category:
            category_ids = Category.objects.filter(name__icontains=request.category).values_list(
                "pk", flat=True
            )
            documents = documents.filter(
                book_id__in=_facet_book_ids(
                    BookSearchFacet.Kind.CATEGORY, (str(pk) for pk in category_ids)
                )
            )
        if request.genre:
            documents = documents.filter(
                book_id__in=_facet_book_ids(
                    BookSearchFacet.Kind.GENRE,
                    (str(pk) for pk in _genre_filter_ids(request.genre)),
                )
            )
        if request.language:
            documents = documents.filter(language=fold(request.language))
        if request.book_format:
            documents = documents.filter(
                book_id__in=_facet_book_ids(
                    BookSearchFacet.Kind.FORMAT, [fold(request.book_format)]
                )
            )
        return documents

    def _text_match(
        self, documents: QuerySet[BookSearchDocument], query: str
    ) -> tuple[QuerySet[BookSearchDocument], SearchQuery | None]:
        tokens = query_tokens(query)
        folded = fold(query)
        condition = Q()
        search_query: SearchQuery | None = None
        if tokens:
            search_query = SearchQuery(_tsquery_text(tokens), search_type="raw", config="simple")
            condition |= Q(document=search_query)
        if len(folded) >= 3:
            # Partial words inside titles/author names: trigram-indexed LIKE.
            condition |= Q(title_sort__contains=folded) | Q(author_text__contains=folded)
        if not condition:
            return documents.none(), None
        return documents.filter(condition), search_query

    def _fuzzy_ids(self, documents: QuerySet[BookSearchDocument], query: str) -> list[int]:
        """Typo-tolerant title/author matches (pg_trgm word similarity) for zero-hit queries."""
        folded = fold(query)
        if len(folded) < 3:
            return []
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SET LOCAL pg_trgm.word_similarity_threshold = %s", [FUZZY_WORD_SIMILARITY]
            )
            return list(
                documents.filter(
                    Q(title_sort__trigram_word_similar=folded)
                    | Q(author_text__trigram_word_similar=folded)
                )
                .annotate(
                    similarity=Greatest(
                        TrigramWordSimilarity(folded, "title_sort"),
                        TrigramWordSimilarity(folded, "author_text"),
                    )
                )
                .order_by("-similarity", "-book_created_at", "-book_id")
                .values_list("book_id", flat=True)[:MAX_RANKED_RESULTS]
            )

    def _matching(self, request: SearchRequest) -> tuple[QuerySet[BookSearchDocument], Any]:
        documents = self._filtered(request)
        search_query = None
        if request.query:
            documents, search_query = self._text_match(documents, request.query)
        return documents, search_query

    def _count(self, request: SearchRequest, documents: QuerySet[BookSearchDocument]) -> int:
        if request.has_filters:
            return documents.count()
        total = cache.get(_COUNT_CACHE_KEY)
        if total is None:
            total = documents.count()
            cache.set(_COUNT_CACHE_KEY, total, GLOBAL_CACHE_SECONDS)
        return int(total)

    def search(self, request: SearchRequest) -> SearchPage:
        sort = request.effective_sort(ranked=True)
        documents, search_query = self._matching(request)
        count = self._count(request, documents)

        if request.query and count == 0:
            fuzzy = self._fuzzy_ids(self._filtered(request), request.query)
            if fuzzy:
                page, start, stop = _offset_window(request, cap=len(fuzzy))
                return SearchPage(
                    book_ids=fuzzy[start:stop],
                    count=len(fuzzy),
                    sort=RELEVANCE,
                    page=page,
                    fuzzy=True,
                    backend=self.name,
                )

        if sort == RELEVANCE:
            page, start, stop = _offset_window(request, cap=MAX_RANKED_RESULTS)
            ranked = documents
            if search_query is not None:
                ranked = documents.annotate(rank=SearchRank(F("document"), search_query))
                order: tuple[str, ...] = ("-rank", "-book_created_at", "-book_id")
            else:
                order = ("-book_created_at", "-book_id")
            ids = list(ranked.order_by(*order).values_list("book_id", flat=True)[start:stop])
            return SearchPage(
                book_ids=ids,
                count=count,
                sort=sort,
                page=page,
                backend=self.name,
                extra={"max_results": MAX_RANKED_RESULTS},
            )

        _book_field, document_field, descending = BROWSE_SORTS[sort]
        if request.page is not None:
            page, start, stop = _offset_window(request)
            prefix = "-" if descending else ""
            ids = list(
                documents.order_by(f"{prefix}{document_field}", f"{prefix}book_id").values_list(
                    "book_id", flat=True
                )[start:stop]
            )
            return SearchPage(book_ids=ids, count=count, sort=sort, page=page, backend=self.name)
        ids, next_cursor, previous_cursor = _keyset_page(
            documents,
            request=request,
            sort=sort,
            key_field=document_field,
            id_field="book_id",
            descending=descending,
        )
        return SearchPage(
            book_ids=ids,
            count=count,
            sort=sort,
            page=None if request.cursor else 1,
            next_cursor=next_cursor,
            previous_cursor=previous_cursor,
            backend=self.name,
        )

    def facet_counts(
        self, request: SearchRequest, facets: Sequence[str]
    ) -> dict[str, list[FacetCount]]:
        results: dict[str, list[FacetCount]] = {}
        for facet in facets:
            if facet not in FACETS:
                continue
            if not request.has_filters:
                cached = cache.get(_FACET_CACHE_KEY.format(facet=facet))
                if cached is not None:
                    results[facet] = cached
                    continue
            documents, _search_query = self._matching(request)
            counts = self._facet(documents, facet)
            if not request.has_filters:
                cache.set(_FACET_CACHE_KEY.format(facet=facet), counts, GLOBAL_CACHE_SECONDS)
            results[facet] = counts
        return results

    def _facet(self, documents: QuerySet[BookSearchDocument], facet: str) -> list[FacetCount]:
        if facet in ("language", "decade"):
            column = "language" if facet == "language" else "publication_decade"
            rows = (
                documents.exclude(**{f"{column}__isnull": True})
                .values_list(column)
                .annotate(n=Count("pk"))
                .order_by("-n", column)[:FACET_LIMIT]
            )
            return _named_counts(rows)
        facet_rows = BookSearchFacet.objects.filter(kind=facet)
        if documents.query.where:
            facet_rows = facet_rows.filter(book_id__in=documents.values("book_id"))
        counted = list(
            facet_rows.values_list("value")
            .annotate(n=Count("pk"))
            .order_by("-n", "value")[:FACET_LIMIT]
        )
        if facet == BookSearchFacet.Kind.FORMAT:
            return _named_counts(counted)
        model: Any = {
            "author": Author,
            "genre": Genre,
            "category": Category,
        }[facet]
        name_field = "full_name" if model is Author else "name"
        names = dict(
            model.objects.filter(pk__in=[int(value) for value, _n in counted]).values_list(
                "pk", name_field
            )
        )
        return [
            FacetCount(value=str(names[int(value)]), count=n)
            for value, n in counted
            if int(value) in names
        ]

    def index_books(self, book_ids: Iterable[int]) -> int:
        return index_books(book_ids)

    def rebuild(self) -> int:
        return rebuild_search_index()


def get_search_backend() -> SearchBackend:
    """PostgreSQL search once its index is complete; the portable fallback otherwise."""
    if connection.vendor == "postgresql" and search_index_ready():
        return PostgresSearchBackend()
    return DatabaseFallbackSearchBackend()
