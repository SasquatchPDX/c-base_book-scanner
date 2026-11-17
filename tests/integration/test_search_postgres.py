"""PostgreSQL library search (#28 phase 1): FTS, pg_trgm, GIN index use, row upkeep.

Runs in the infrastructure job against the pinned PostgreSQL service.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection, transaction
from django.test import Client
from django.utils import timezone

from ebook_server.core import scan_pipeline, search
from ebook_server.core.genres import assign_automatic
from ebook_server.core.models import (
    Author,
    Book,
    BookGenre,
    BookSearchDocument,
    BookSearchFacet,
    FileVariant,
    Genre,
    GenreAlias,
    ScanJob,
    StorageLocation,
    get_or_create_profile,
)

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]


@pytest.fixture(autouse=True)
def _fresh_search_cache() -> Iterator[None]:
    cache.clear()
    search._pending.book_ids.clear()
    yield
    cache.clear()


def _client() -> Client:
    profile = get_or_create_profile("pg-search-reader")
    client = Client()
    client.force_login(profile.user)
    return client


_counter = 0


def _book(
    title: str,
    *,
    authors: tuple[str, ...] = (),
    language: str = "en",
    description: str = "",
    formats: tuple[str, ...] = (),
    genres: tuple[str, ...] = (),
) -> Book:
    """Create a book through the ORM; each statement commits, so signals index it."""
    global _counter
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    with transaction.atomic():
        book = Book.objects.create(title=title, language=language, description=description)
        for name in authors:
            book.authors.add(Author.objects.get_or_create(full_name=name)[0])
        for name in genres:
            book.genres.add(Genre.objects.get_or_create(name=name)[0])
        for fmt in formats:
            _counter += 1
            FileVariant.objects.create(
                book=book,
                format=fmt,
                mime_type="application/octet-stream",
                original_filename=f"pg-{_counter}.{fmt}",
                storage_location=location,
                storage_identifier=f"pg/{_counter}.{fmt}",
                size_bytes=_counter,
                sha256=f"{_counter:064x}",
                source_type=FileVariant.SourceType.UPLOAD,
                processing_state=FileVariant.ProcessingState.COMPLETE,
            )
    return book


def _ids(payload: dict[str, Any]) -> list[int]:
    return [item["id"] for item in payload["items"]]


@pytest.mark.django_db(transaction=True)
def test_extension_and_indexes_exist_after_migrate() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT extname FROM pg_extension WHERE extname = 'pg_trgm'")
        assert cursor.fetchone() == ("pg_trgm",)
        cursor.execute(
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = 'core_booksearchdocument'"
        )
        definitions = dict(cursor.fetchall())
    assert "USING gin (document)" in definitions["booksearch_document_gin"]
    assert "gin_trgm_ops" in definitions["booksearch_title_trgm"]
    assert "gin_trgm_ops" in definitions["booksearch_author_trgm"]
    assert "booksearch_created_idx" in definitions


@pytest.mark.django_db(transaction=True)
def test_search_row_and_tsvector_follow_committed_changes() -> None:
    book = _book("Die Straße", authors=("Jürgen Müller",), formats=("epub",), language="de")

    document = BookSearchDocument.objects.get(book=book)
    assert document.document is not None
    assert document.title_sort == "die strasse"
    assert set(
        BookSearchFacet.objects.filter(book=book, kind="format").values_list("value", flat=True)
    ) == {"epub"}
    assert isinstance(search.get_search_backend(), search.PostgresSearchBackend)

    book.title = "Der Garten"
    book.save()
    FileVariant.objects.filter(book=book).update(processing_state="failed")
    search.schedule_reindex([book.pk])  # `.update()` sends no signal

    client = _client()
    assert _ids(client.get("/library/books/", {"q": "garten"}).json()) == [book.pk]
    assert _ids(client.get("/library/books/", {"q": "strasse"}).json()) == []
    assert _ids(client.get("/library/books/", {"format": "epub"}).json()) == []

    book.delete()
    assert not BookSearchDocument.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_full_text_ranks_title_matches_above_description_matches() -> None:
    in_description = _book("Garden Notes", description="A long aside about anatomy.")
    in_title = _book("Anatomy of Melancholy")
    by_author = _book("Collected Letters", authors=("Anna Anatomy",))
    _book("Unrelated")

    payload = _client().get("/library/books/", {"q": "anatomy"}).json()

    assert payload["sort"] == "relevance"
    assert payload["count"] == 3
    assert _ids(payload) == [in_title.pk, by_author.pk, in_description.pk]


@pytest.mark.django_db(transaction=True)
def test_accents_prefixes_isbns_and_substrings_match() -> None:
    muller = _book("Kochbuch", authors=("Jürgen Müller",))
    patterns = _book("Design Patterns Explained")
    _book("Something Else")
    client = _client()

    assert _ids(client.get("/library/books/", {"q": "jurgen muller"}).json()) == [muller.pk]
    assert _ids(client.get("/library/books/", {"q": "MÜLLER"}).json()) == [muller.pk]
    # Prefix of the last word (type-ahead) and a substring inside a word (trigram LIKE).
    assert _ids(client.get("/library/books/", {"q": "design patt"}).json()) == [patterns.pk]
    assert _ids(client.get("/library/books/", {"q": "atter"}).json()) == [patterns.pk]
    assert _ids(client.get("/library/books/", {"author": "ller"}).json()) == [muller.pk]


@pytest.mark.django_db(transaction=True)
def test_trigram_fuzzy_match_when_nothing_matches_exactly() -> None:
    anatomie = _book("Anatomie des Menschen", language="de")
    _book("Gartenbau", language="de")

    payload = _client().get("/library/books/", {"q": "anatomei"}).json()

    assert payload["fuzzy"] is True
    assert _ids(payload) == [anatomie.pk]


@pytest.mark.django_db(transaction=True)
def test_keyset_browse_and_filters_match_the_book_table() -> None:
    base = timezone.now() - timedelta(days=3)
    for index in range(9):
        book = _book(
            f"Title {index % 3}",
            language="de" if index % 2 else "en",
            formats=("epub",) if index % 3 else ("pdf",),
        )
        Book.objects.filter(pk=book.pk).update(created_at=base + timedelta(hours=index // 2))
    search.rebuild_search_index()  # created_at was changed with `.update()`
    client = _client()

    sorts: list[tuple[str, tuple[str, str]]] = [
        ("-created_at", ("-created_at", "-id")),
        ("title", ("title", "id")),
    ]
    for sort, order in sorts:
        seen: list[int] = []
        response = client.get("/library/books/", {"sort": sort, "page_size": "2"}).json()
        while True:
            seen += _ids(response)
            if not response["next_cursor"]:
                break
            response = client.get(
                "/library/books/",
                {"sort": sort, "page_size": "2", "cursor": response["next_cursor"]},
            ).json()
        assert seen == list(Book.objects.order_by(*order).values_list("pk", flat=True))

    filtered = client.get(
        "/library/books/", {"language": "de", "format": "epub", "facets": "format,language"}
    ).json()
    expected = Book.objects.filter(language="de", file_variants__format="epub").order_by(
        "-created_at", "-id"
    )
    assert _ids(filtered) == [book.pk for book in expected]
    assert filtered["facets"]["format"] == [{"value": "epub", "count": len(expected)}]
    assert filtered["facets"]["language"] == [{"value": "de", "count": len(expected)}]


def _plan(sql: str, params: list[Any]) -> str:
    """EXPLAIN with sequential scans discouraged, so tiny test tables still show index use."""
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute("SET LOCAL enable_seqscan = off")
        cursor.execute(f"EXPLAIN {sql}", params)
        return "\n".join(row[0] for row in cursor.fetchall())


@pytest.mark.django_db(transaction=True)
def test_search_queries_use_the_gin_and_btree_indexes() -> None:
    for index in range(20):
        _book(f"Pattern Book {index}", authors=(f"Author {index}",), formats=("epub",))
    backend = search.PostgresSearchBackend()

    fts, _query = backend._text_match(BookSearchDocument.objects.all(), "pattern")
    fts_sql, fts_params = fts.values("book_id").query.sql_with_params()
    assert "booksearch_document_gin" in _plan(fts_sql, list(fts_params))
    assert "booksearch_title_trgm" in _plan(fts_sql, list(fts_params))

    author = backend._filtered(search.SearchRequest(author="thor 1"))
    author_sql, author_params = author.values("book_id").query.sql_with_params()
    assert "booksearch_author_trgm" in _plan(author_sql, list(author_params))

    browse = BookSearchDocument.objects.order_by("-book_created_at", "-book_id").values("book_id")[
        :25
    ]
    browse_sql, browse_params = browse.query.sql_with_params()
    assert "booksearch_created_idx" in _plan(browse_sql, list(browse_params))

    by_format = backend._filtered(search.SearchRequest(book_format="EPUB", language="en"))
    format_sql, format_params = by_format.values("book_id").query.sql_with_params()
    format_plan = _plan(format_sql, list(format_params))
    assert "booksearchfacet_lookup_idx" in format_plan
    assert "upper(" not in format_plan.lower()


@pytest.mark.django_db(transaction=True)
def test_rescan_invalidation_in_autocommit_removes_the_last_format() -> None:
    """The reindex runs at once outside a transaction, so it must follow the update."""
    book = _book("Rescanned Atlas", formats=("pdf",))
    variant = FileVariant.objects.get(book=book)
    client = _client()
    assert _ids(client.get("/library/books/", {"format": "pdf"}).json()) == [book.pk]
    assert connection.get_autocommit()

    scan_pipeline._invalidate_file_variants_and_descendants({variant.pk})

    assert not BookSearchFacet.objects.filter(book=book, kind="format").exists()
    assert _ids(client.get("/library/books/", {"format": "pdf"}).json()) == []
    facets = client.get("/library/books/", {"facets": "format"}).json()["facets"]["format"]
    assert facets == []
    assert search.verify_search_index(sample=100).ok


@pytest.mark.django_db(transaction=True)
def test_raw_scan_books_are_never_indexed_or_found() -> None:
    """#127: a book that exists only through an unfinished scan is not in the index."""
    library = _book("Atlas of Rivers", formats=("epub",))
    raw = _book("Atlas of Mountains")
    job = ScanJob.objects.create(state=ScanJob.State.CAPTURED, book=raw)
    location = StorageLocation.objects.get(identifier="default")
    FileVariant.objects.create(
        book=raw,
        format="pdf",
        mime_type="application/pdf",
        original_filename="cleaned.pdf",
        storage_location=location,
        storage_identifier="pg/raw-scan-cleaned.pdf",
        size_bytes=1,
        sha256="f" * 64,
        source_type=FileVariant.SourceType.CLEANED,
        processing_state=FileVariant.ProcessingState.COMPLETE,
        converter_options={"scan_job_id": str(job.pk)},
    )
    client = _client()

    assert not BookSearchDocument.objects.filter(book=raw).exists()
    assert isinstance(search.get_search_backend(), search.PostgresSearchBackend)
    assert _ids(client.get("/library/books/", {"q": "atlas"}).json()) == [library.pk]
    assert _ids(client.get("/library/books/", {"format": "pdf"}).json()) == []
    assert _ids(client.get("/library/books/").json()) == [library.pk]
    search.rebuild_search_index()
    assert not BookSearchDocument.objects.filter(book=raw).exists()
    assert search.verify_search_index(sample=100).ok

    job.state = ScanJob.State.COMPLETE
    job.save()
    cache.clear()

    assert sorted(_ids(client.get("/library/books/", {"q": "atlas"}).json())) == sorted(
        [library.pk, raw.pk]
    )
    assert _ids(client.get("/library/books/", {"format": "pdf"}).json()) == [raw.pk]


@pytest.mark.django_db(transaction=True)
def test_verifier_detects_stale_weighted_full_text_content() -> None:
    book = _book("Quiet Harbour", description="lighthouse keepers")
    other = _book("Busy Port", description="container cranes")
    client = _client()
    assert _ids(client.get("/library/books/", {"q": "lighthouse"}).json()) == [book.pk]
    assert search.verify_search_index(sample=100).ok

    # Bulk change: no signal, and only the weight-D (description) FTS text differs.
    Book.objects.filter(pk=book.pk).update(description="smugglers tunnels")
    assert _ids(client.get("/library/books/", {"q": "smugglers"}).json()) == []
    report = search.verify_search_index(sample=100)
    assert report.drifted_book_ids == [book.pk]

    search.index_books(report.drifted_book_ids)
    assert _ids(client.get("/library/books/", {"q": "smugglers"}).json()) == [book.pk]
    assert search.verify_search_index(sample=100).ok

    # A vector changed behind the digest's back (manual repair, other config) is drift too.
    with connection.cursor() as cursor:
        cursor.execute(
            "UPDATE core_booksearchdocument SET document = to_tsvector('simple', 'tampered') "
            "WHERE book_id = %s",
            [other.pk],
        )
    assert search.verify_search_index(sample=100).drifted_book_ids == [other.pk]
    search.index_books([other.pk])
    assert _ids(client.get("/library/books/", {"q": "cranes"}).json()) == [other.pk]
    assert search.verify_search_index(sample=100).ok


@pytest.mark.django_db(transaction=True)
def test_book_genre_rows_and_aliases_on_postgresql() -> None:
    book = _book("Green Towers")
    genre = Genre.objects.create(name="Solarpunk utopias")
    GenreAlias.objects.create(genre=genre, alias="Solar-Punk")

    assert assign_automatic(book, provider_subjects=["SolarPunk"], embedded_subjects=None)
    assert set(
        BookSearchFacet.objects.filter(book=book, kind="genre").values_list("value", flat=True)
    ) == {str(genre.pk)}
    client = _client()
    assert _ids(client.get("/library/books/", {"genre": "solar punk"}).json()) == [book.pk]
    assert _ids(client.get("/library/books/", {"genre": "utopia"}).json()) == [book.pk]

    BookGenre.objects.filter(book=book).delete()
    assert not BookSearchFacet.objects.filter(book=book, kind="genre").exists()
    assert _ids(client.get("/library/books/", {"genre": "solar punk"}).json()) == []
    assert search.verify_search_index(sample=100).ok
