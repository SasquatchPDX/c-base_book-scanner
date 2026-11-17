"""Library search service (#28 phase 1): portable parts, on the SQLite test database.

PostgreSQL full-text, trigram and index-usage behaviour is covered by
`tests/integration/test_search_postgres.py` in the infrastructure job.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import timedelta
from io import StringIO
from typing import Any, cast

import pytest
from django.core.cache import cache
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
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
    Category,
    FileVariant,
    Genre,
    GenreAlias,
    Identifier,
    StorageLocation,
    get_or_create_profile,
)

OnCommit = Callable[..., AbstractContextManager[list[Callable[[], Any]]]]


@pytest.fixture(autouse=True)
def _clean_search_state() -> Iterator[None]:
    cache.clear()
    search._pending.book_ids.clear()
    yield
    search._pending.book_ids.clear()
    cache.clear()


def _client() -> Client:
    profile = get_or_create_profile("search-reader")
    client = Client()
    client.force_login(profile.user)
    return client


def _location() -> StorageLocation:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return location


_variant_counter = 0


def _variant(book: Book, fmt: str, state: str = "complete") -> FileVariant:
    global _variant_counter
    _variant_counter += 1
    return FileVariant.objects.create(
        book=book,
        format=fmt,
        mime_type="application/octet-stream",
        original_filename=f"file-{_variant_counter}.{fmt}",
        storage_location=_location(),
        storage_identifier=f"books/{_variant_counter}.{fmt}",
        size_bytes=_variant_counter,
        sha256=f"{_variant_counter:064x}",
        source_type=FileVariant.SourceType.UPLOAD,
        processing_state=state,
    )


# ------------------------------------------------------------------ text folding


@pytest.mark.parametrize(
    ("raw", "folded"),
    [
        ("Straße", "strasse"),
        ("  Émile   ZOLA ", "emile zola"),
        ("Müller", "muller"),
        ("", ""),
        (None, ""),
    ],
)
def test_fold_removes_case_accents_and_extra_whitespace(raw: str | None, folded: str) -> None:
    assert search.fold(raw) == folded


def test_query_tokens_keep_isbns_together_and_drop_operators() -> None:
    assert search.query_tokens("Le Guin: Earthsea!") == ["le", "guin", "earthsea"]
    assert search.query_tokens("978-3-16-148410-0") == ["9783161484100"]
    assert search.query_tokens("a & b | !c") == ["a", "b", "c"]
    assert search._tsquery_text(["java", "patt"]) == "java & patt:*"


# ----------------------------------------------------------------------- cursors


def test_cursor_round_trip_and_tamper_rejection() -> None:
    token = search.Cursor("title", "alpha", 42).encode()
    assert search.Cursor.decode(token, sort="title") == search.Cursor("title", "alpha", 42)

    with pytest.raises(search.InvalidCursorError):
        search.Cursor.decode(
            token[:-2] + ("A" if token[-2] != "A" else "B") + token[-1], sort="title"
        )
    with pytest.raises(search.InvalidCursorError):
        search.Cursor.decode(token, sort="-created_at")
    with pytest.raises(search.InvalidCursorError):
        search.Cursor.decode("not-a-cursor", sort="title")


# ------------------------------------------------------------- keyset browsing


def _books_with_tied_timestamps(count: int = 11) -> list[Book]:
    """Books whose created_at values collide in groups of three (the id breaks ties)."""
    base = timezone.now() - timedelta(days=5)
    books = []
    for index in range(count):
        book = Book.objects.create(title=f"Book {index % 4}", language="en")
        Book.objects.filter(pk=book.pk).update(created_at=base + timedelta(hours=index // 3))
        books.append(book)
    return books


def _walk(client: Client, params: dict[str, Any]) -> tuple[list[int], list[str]]:
    seen: list[int] = []
    cursors: list[str] = []
    response = client.get("/library/books/", params)
    while True:
        payload = response.json()
        seen.extend(item["id"] for item in payload["items"])
        if not payload["next_cursor"]:
            return seen, cursors
        cursors.append(payload["next_cursor"])
        response = client.get("/library/books/", {**params, "cursor": payload["next_cursor"]})


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("sort", "order"),
    [
        ("-created_at", ("-created_at", "-id")),
        ("created_at", ("created_at", "id")),
        ("title", ("title", "id")),
        ("-title", ("-title", "-id")),
    ],
)
def test_keyset_pages_visit_every_book_once_in_sort_order(
    sort: str, order: tuple[str, str]
) -> None:
    _books_with_tied_timestamps()
    client = _client()

    seen, _cursors = _walk(client, {"sort": sort, "page_size": 3})

    assert seen == list(Book.objects.order_by(*order).values_list("pk", flat=True))


@pytest.mark.django_db
def test_previous_cursor_returns_the_previous_page() -> None:
    _books_with_tied_timestamps()
    client = _client()
    first = client.get("/library/books/", {"page_size": 4}).json()
    assert first["page"] == 1
    assert first["previous_cursor"] is None
    second = client.get("/library/books/", {"page_size": 4, "cursor": first["next_cursor"]}).json()
    third = client.get("/library/books/", {"page_size": 4, "cursor": second["next_cursor"]}).json()

    back_to_second = client.get(
        "/library/books/", {"page_size": 4, "cursor": third["previous_cursor"]}
    ).json()
    back_to_first = client.get(
        "/library/books/", {"page_size": 4, "cursor": back_to_second["previous_cursor"]}
    ).json()

    assert [i["id"] for i in back_to_second["items"]] == [i["id"] for i in second["items"]]
    assert [i["id"] for i in back_to_first["items"]] == [i["id"] for i in first["items"]]
    assert back_to_first["previous_cursor"] is None
    assert third["next_cursor"] is None


@pytest.mark.django_db
def test_keyset_respects_filters_and_counts_all_matches() -> None:
    for index in range(7):
        Book.objects.create(title=f"Java {index}", language="de" if index % 2 else "en")
    Book.objects.create(title="Other", language="de")
    client = _client()

    seen, cursors = _walk(client, {"q": "java", "language": "de", "page_size": 2})

    expected = Book.objects.filter(title__startswith="Java", language="de").order_by(
        "-created_at", "-id"
    )
    assert seen == [book.pk for book in expected]
    assert len(cursors) == 1
    assert client.get("/library/books/", {"q": "java", "language": "de"}).json()["count"] == 3


@pytest.mark.django_db
def test_invalid_cursor_is_rejected() -> None:
    Book.objects.create(title="Only")
    client = _client()
    title_cursor = search.Cursor("title", "only", 1).encode()

    assert client.get("/library/books/", {"cursor": "garbage"}).status_code == 400
    # A cursor is only valid for the sort it was issued for.
    assert client.get("/library/books/", {"cursor": title_cursor}).status_code == 400


@pytest.mark.django_db
def test_page_parameter_still_works_but_is_deprecated() -> None:
    _books_with_tied_timestamps()
    client = _client()

    response = client.get("/library/books/", {"page": 2, "page_size": 4})

    assert response.status_code == 200
    assert response["Deprecation"] == "true"
    expected = list(Book.objects.order_by("-created_at", "-id").values_list("pk", flat=True))
    assert [item["id"] for item in response.json()["items"]] == expected[4:8]
    assert "Deprecation" not in client.get("/library/books/").headers


@pytest.mark.django_db
def test_html_pagination_links_keep_the_filters() -> None:
    for index in range(5):
        Book.objects.create(title=f"Garden {index}", language="de")
    client = _client()

    params: dict[str, str | int] = {
        "view": "html",
        "language": "de",
        "sort": "title",
        "page_size": 2,
    }
    response = client.get("/library/books/", params)

    html = response.content.decode()
    assert response.status_code == 200
    assert 'rel="next"' in html
    assert "language=de" in html
    assert "sort=title" in html
    assert "cursor=" in html


@pytest.mark.django_db
def test_sqlite_uses_the_fallback_backend() -> None:
    if connection.vendor != "sqlite":
        pytest.skip("SQLite-specific backend selection")
    assert isinstance(search.get_search_backend(), search.DatabaseFallbackSearchBackend)


# -------------------------------------------------------------------- facets


@pytest.mark.django_db
def test_fallback_facet_counts_match_brute_force() -> None:
    manual = Genre.objects.create(name="Manual")
    novel = Genre.objects.create(name="Novel")
    for index, (language, formats, genres) in enumerate(
        [
            ("en", ["epub", "pdf"], [manual]),
            ("en", ["epub"], [novel, manual]),
            ("de", ["pdf"], [novel]),
            ("de", [], []),
        ]
    ):
        book = Book.objects.create(title=f"Facet {index}", language=language)
        book.genres.set(genres)
        for fmt in formats:
            _variant(book, fmt)
        _variant(book, "mobi", state="failed")
    client = _client()

    payload = client.get("/library/books/", {"facets": "language,format,genre,bogus"}).json()
    filtered = client.get("/library/books/", {"language": "en", "facets": "format"}).json()

    assert payload["facets"] == {
        "language": [{"value": "de", "count": 2}, {"value": "en", "count": 2}],
        "format": [{"value": "epub", "count": 2}, {"value": "pdf", "count": 2}],
        "genre": [{"value": "Manual", "count": 2}, {"value": "Novel", "count": 2}],
    }
    assert filtered["facets"] == {
        "format": [{"value": "epub", "count": 2}, {"value": "pdf", "count": 1}],
    }
    assert "facets" not in client.get("/library/books/").json()


# ------------------------------------------------------- search-row upkeep


def _document(book: Book) -> BookSearchDocument:
    return BookSearchDocument.objects.get(book=book)


def _facets(book: Book, kind: str) -> set[str]:
    return set(BookSearchFacet.objects.filter(book=book, kind=kind).values_list("value", flat=True))


@pytest.mark.django_db
def test_search_row_follows_book_changes(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        book = Book.objects.create(
            title="Die Straße", language=" DE ", publication_year=1987, publisher="Verlag"
        )
        author = Author.objects.create(full_name="Jürgen Müller")
        book.authors.add(author, Author.objects.create(full_name="Anna Zola"))
        genre = Genre.objects.create(name="Novel")
        book.genres.add(genre)
        book.categories.add(Category.objects.create(name="Fiction"))
        Identifier.objects.create(book=book, kind="isbn13", value="978-3-16-148410-0")
        _variant(book, "EPUB")
        _variant(book, "pdf", state="failed")

    document = _document(book)
    assert document.title_sort == "die strasse"
    assert document.author_text == "anna zola\njurgen muller"
    assert document.author_sort == "anna zola"
    assert document.language == "de"
    assert document.publication_decade == 1980
    assert document.book_created_at == book.created_at
    assert _facets(book, "format") == {"epub"}
    assert _facets(book, "genre") == {str(genre.pk)}
    assert len(_facets(book, "author")) == 2

    with django_capture_on_commit_callbacks(execute=True):
        author.full_name = "Jürgen Meyer"
        author.save()
        book.title = "Neuer Titel"
        book.save()
        book.genres.clear()
        _variant(book, "mobi")
    document = _document(book)
    assert document.title_sort == "neuer titel"
    assert "jurgen meyer" in document.author_text
    assert _facets(book, "genre") == set()
    assert _facets(book, "format") == {"epub", "mobi"}

    with django_capture_on_commit_callbacks(execute=True):
        FileVariant.objects.filter(book=book, format="mobi").get().delete()
        cast(Any, genre).books.add(book)  # reverse side of the m2m
        author.delete()
    assert _facets(book, "format") == {"epub"}
    assert _facets(book, "genre") == {str(genre.pk)}
    assert _document(book).author_text == "anna zola"

    with django_capture_on_commit_callbacks(execute=True):
        book.delete()
    assert not BookSearchDocument.objects.exists()
    assert not BookSearchFacet.objects.exists()


@pytest.mark.django_db
def test_indexing_is_coalesced_per_transaction(
    django_capture_on_commit_callbacks: OnCommit,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[set[int]] = []
    original = search.index_books

    def recording(book_ids: Any) -> int:
        ids = set(book_ids)
        calls.append(ids)
        return original(ids)

    monkeypatch.setattr(search, "index_books", recording)
    with django_capture_on_commit_callbacks(execute=True):
        book = Book.objects.create(title="One import")
        book.authors.add(Author.objects.create(full_name="Some One"))
        _variant(book, "epub")
        _variant(book, "pdf")

    assert calls == [{book.pk}]


@pytest.mark.django_db
def test_scan_variant_invalidation_reindexes_formats(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        book = Book.objects.create(title="Scanned")
        variant = _variant(book, "pdf")
    assert _facets(book, "format") == {"pdf"}

    with django_capture_on_commit_callbacks(execute=True):
        scan_pipeline._invalidate_file_variants_and_descendants({variant.pk})

    assert _facets(book, "format") == set()


@pytest.mark.django_db
def test_rebuild_and_verify_commands() -> None:
    # Created inside the test transaction: no on-commit indexing has run.
    books = [Book.objects.create(title=f"Rebuild {index}") for index in range(3)]
    _variant(books[0], "epub")
    out = StringIO()

    with pytest.raises(CommandError):
        call_command("verify_search_index", stdout=out)
    assert "missing=3" in out.getvalue()

    call_command("rebuild_search_index", stdout=out)
    assert "Indexed 3 book(s)" in out.getvalue()
    assert BookSearchDocument.objects.count() == 3
    assert _facets(books[0], "format") == {"epub"}

    BookSearchDocument.objects.filter(book=books[1]).update(title_sort="stale")
    with pytest.raises(CommandError):
        call_command("verify_search_index", stdout=out)
    call_command("verify_search_index", "--fix", stdout=out)
    assert _document(books[1]).title_sort == "rebuild 1"
    call_command("verify_search_index", stdout=out)
    assert "Search index is consistent." in out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_scan_invalidation_in_autocommit_drops_the_format() -> None:
    """Outside a transaction `on_commit` runs at once; it must see the FAILED state."""
    book = Book.objects.create(title="Rescanned")
    variant = _variant(book, "pdf")
    assert _facets(book, "format") == {"pdf"}
    assert connection.get_autocommit()

    scan_pipeline._invalidate_file_variants_and_descendants({variant.pk})

    assert _facets(book, "format") == set()
    assert _client().get("/library/books/", {"format": "pdf"}).json()["count"] == 0
    assert search.verify_search_index(sample=100).ok


@pytest.mark.django_db
def test_verify_detects_drift_in_weighted_full_text_content() -> None:
    books = [Book.objects.create(title=f"Digest {index}") for index in range(3)]
    search.rebuild_search_index()
    assert search.verify_search_index(sample=100).ok

    # A bulk change sends no signal and touches only full-text (weight D) content.
    Book.objects.filter(pk=books[1].pk).update(description="A brand new blurb")
    report = search.verify_search_index(sample=100)
    assert report.drifted_book_ids == [books[1].pk]
    assert not report.ok

    out = StringIO()
    call_command("verify_search_index", "--fix", stdout=out)
    assert "Reindexed 1 book(s)" in out.getvalue()
    assert search.verify_search_index(sample=100).ok


def test_content_digest_covers_every_weighted_column() -> None:
    base = ("title", "author", "publisher", "description")
    digests = {search.content_digest(base)}
    for index in range(len(base)):
        changed = list(base)
        changed[index] += " changed"
        digests.add(search.content_digest(changed))
    # Moving text from one weight to another changes the digest too.
    digests.add(search.content_digest(("title author", "", "publisher", "description")))
    assert len(digests) == len(base) + 2


@pytest.mark.django_db
def test_book_genre_rows_written_directly_keep_the_index_current(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    with django_capture_on_commit_callbacks(execute=True):
        book = Book.objects.create(title="Dune")
        solarpunk = Genre.objects.create(name="Solarpunk utopias")
        GenreAlias.objects.create(genre=solarpunk, alias="Solar-Punk")
    assert _facets(book, "genre") == set()

    # `assign_automatic` writes BookGenre rows directly: no m2m_changed.
    with django_capture_on_commit_callbacks(execute=True):
        added = assign_automatic(book, provider_subjects=["SolarPunk"], embedded_subjects=None)
    assert added == [solarpunk]
    assert _facets(book, "genre") == {str(solarpunk.pk)}
    assert search.verify_search_index(sample=100).ok

    client = _client()
    # A genre alias selects its genre, as master's browse filter does (#50).
    by_alias = client.get("/library/books/", {"genre": "solar punk"}).json()
    assert [item["id"] for item in by_alias["items"]] == [book.pk]
    assert client.get("/library/books/", {"genre": "utopia"}).json()["count"] == 1

    with django_capture_on_commit_callbacks(execute=True):
        BookGenre.objects.filter(book=book).delete()
    assert _facets(book, "genre") == set()

    with django_capture_on_commit_callbacks(execute=True):
        BookGenre.objects.create(book=book, genre=solarpunk)
    assert _facets(book, "genre") == {str(solarpunk.pk)}
    assert search.verify_search_index(sample=100).ok
