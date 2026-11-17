"""Query shape of the library browse views (#28 phase 0).

The list view used to issue one `formats_available` query per book and a
`SELECT DISTINCT` over every column for both the page and the count. These
tests pin the narrower replacement: identical results to the old queryset, a
query count that does not grow with the page size, and no `DISTINCT`.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection
from django.db.models import Q, QuerySet
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ebook_server.core.models import (
    Author,
    Book,
    Category,
    FileVariant,
    Genre,
    StorageLocation,
    get_or_create_profile,
)

_BOOKS: list[dict[str, Any]] = [
    # title, language, authors, categories, genres, (format, state) variants, description
    {
        "title": "Alpha Java Patterns",
        "language": "en",
        "authors": ["Ursula Le Guin", "Alan Turing"],
        "categories": ["Reference", "Computing"],
        "genres": ["Manual"],
        "variants": [("epub", "complete"), ("pdf", "complete"), ("pdf", "complete")],
        "description": "",
    },
    {
        "title": "Beta Gardening",
        "language": "de",
        "authors": ["Grete Gruen"],
        "categories": ["Hobby"],
        "genres": ["Manual", "Nonfiction"],
        "variants": [("epub", "complete"), ("mobi", "failed")],
        "description": "A book that mentions java once.",
    },
    {
        "title": "Gamma Earthsea",
        "language": "EN",
        "authors": ["Ursula Le Guin"],
        "categories": ["Fiction"],
        "genres": ["Fantasy", "Novel"],
        "variants": [("epub", "complete"), ("mobi", "complete")],
        "description": "",
    },
    {
        "title": "Delta Without Files",
        "language": "fr",
        "authors": [],
        "categories": [],
        "genres": [],
        "variants": [],
        "description": "",
    },
    {
        "title": "Epsilon Failed Only",
        "language": "de",
        "authors": ["Alan Turing"],
        "categories": ["Computing"],
        "genres": ["Nonfiction"],
        "variants": [("epub", "failed"), ("pdf", "queued")],
        "description": "",
    },
    {
        "title": "Zeta Java Gardens",
        "language": "en",
        "authors": ["Grete Gruen", "Ursula Le Guin"],
        "categories": ["Hobby", "Computing"],
        "genres": ["Manual"],
        "variants": [("PDF", "complete"), ("epub", "complete")],
        "description": "",
    },
    {
        "title": "Eta Subtitle Hit",
        "language": "en",
        "authors": ["Nobody Special"],
        "categories": ["Reference"],
        "genres": ["Novel"],
        "variants": [("txt", "complete")],
        "description": "",
        "subtitle": "Learning Java the long way",
    },
    {
        "title": "Theta Computing History",
        "language": "de",
        "authors": ["Alan Turing", "Grete Gruen"],
        "categories": ["Computing", "Reference"],
        "genres": ["Nonfiction", "Manual"],
        "variants": [("pdf", "complete"), ("djvu", "complete"), ("epub", "failed")],
        "description": "",
    },
]

# Representative filter combinations, including ones whose old joins fanned out.
_FILTER_COMBOS: list[dict[str, str]] = [
    {},
    {"sort": "created_at"},
    {"sort": "title"},
    {"sort": "-title"},
    {"sort": "bogus"},
    {"q": "java"},
    {"q": "ursula"},
    {"q": "java", "author": "grete"},
    {"q": "zzzznomatch"},
    {"title": "a"},
    {"author": "Ursula"},
    {"author": "turing", "sort": "title"},
    {"category": "computing"},
    {"category": "o", "genre": "a"},
    {"genre": "manual"},
    {"genre": "Manual", "language": "en"},
    {"language": "de"},
    {"language": "en", "format": "epub"},
    {"format": "pdf"},
    {"format": "PDF", "sort": "-title"},
    {"format": "mobi"},
    {"format": "epub", "author": "Alan", "category": "Computing"},
    {"q": "a", "author": "e", "category": "e", "genre": "n", "format": "epub", "sort": "title"},
]


def _seed_books() -> None:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    base_time = timezone.now() - timedelta(days=30)
    counter = 0
    for index, spec in enumerate(_BOOKS):
        book = Book.objects.create(
            title=spec["title"],
            subtitle=spec.get("subtitle", ""),
            language=spec["language"],
            description=spec["description"],
        )
        # Distinct timestamps: the old queryset had no tiebreaker.
        Book.objects.filter(pk=book.pk).update(created_at=base_time + timedelta(hours=index * 7))
        for name in spec["authors"]:
            book.authors.add(Author.objects.get_or_create(full_name=name)[0])
        for name in spec["categories"]:
            book.categories.add(Category.objects.get_or_create(name=name)[0])
        for name in spec["genres"]:
            book.genres.add(Genre.objects.get_or_create(name=name)[0])
        for fmt, state in spec["variants"]:
            counter += 1
            FileVariant.objects.create(
                book=book,
                format=fmt,
                mime_type="application/octet-stream",
                original_filename=f"file-{counter}.{fmt.lower()}",
                storage_location=location,
                storage_identifier=f"books/{counter}.{fmt.lower()}",
                size_bytes=counter,
                sha256=f"{counter:064x}",
                source_type=FileVariant.SourceType.UPLOAD,
                processing_state=state,
            )


def _legacy_books(params: dict[str, str]) -> QuerySet[Book]:
    """The pre-#28 queryset of `library_books`, kept verbatim as the oracle."""
    query = params.get("q", "")
    books = Book.objects.all()
    if query:
        books = books.filter(
            Q(title__icontains=query)
            | Q(subtitle__icontains=query)
            | Q(description__icontains=query)
            | Q(authors__full_name__icontains=query),
        )
    if params.get("title"):
        books = books.filter(title__icontains=params["title"])
    if params.get("author"):
        books = books.filter(authors__full_name__icontains=params["author"])
    if params.get("category"):
        books = books.filter(categories__name__icontains=params["category"])
    if params.get("genre"):
        books = books.filter(genres__name__icontains=params["genre"])
    if params.get("language"):
        books = books.filter(language__iexact=params["language"])
    if params.get("format"):
        books = books.filter(
            file_variants__format__iexact=params["format"],
            file_variants__processing_state=FileVariant.ProcessingState.COMPLETE,
        )
    sort_map = {
        "created_at": "created_at",
        "-created_at": "-created_at",
        "title": "title",
        "-title": "-title",
    }
    return books.distinct().order_by(sort_map.get(params.get("sort", ""), "-created_at"))


def _legacy_formats(book: Book) -> list[str]:
    return sorted(
        {
            variant.format
            for variant in book.file_variants.filter(
                processing_state=FileVariant.ProcessingState.COMPLETE,
            )
        }
    )


def _client() -> Client:
    profile = get_or_create_profile("browse-reader")
    client = Client()
    client.force_login(profile.user)
    return client


@pytest.mark.django_db
@pytest.mark.parametrize("params", _FILTER_COMBOS, ids=lambda params: repr(params))
def test_library_books_matches_legacy_queryset(params: dict[str, str]) -> None:
    _seed_books()
    client = _client()

    response = client.get("/library/books/", {**params, "page_size": "100"})

    assert response.status_code == 200
    payload = response.json()
    expected = list(_legacy_books(params))
    assert payload["count"] == len(expected)
    assert [item["id"] for item in payload["items"]] == [book.id for book in expected]
    assert {item["id"]: item["formats_available"] for item in payload["items"]} == {
        book.id: _legacy_formats(book) for book in expected
    }


@pytest.mark.django_db
def test_library_books_pages_match_legacy_queryset() -> None:
    _seed_books()
    client = _client()
    expected = [book.id for book in _legacy_books({"sort": "title"})]

    seen: list[int] = []
    for page in (1, 2, 3):
        response = client.get("/library/books/", {"sort": "title", "page_size": 3, "page": page})
        assert response.json()["count"] == len(expected)
        seen.extend(item["id"] for item in response.json()["items"])

    assert seen == expected


def _count_queries(client: Client, path: str, params: dict[str, Any]) -> list[str]:
    with CaptureQueriesContext(connection) as captured:
        response = client.get(path, params)
    assert response.status_code == 200
    return [query["sql"] for query in captured.captured_queries]


@pytest.mark.django_db
@pytest.mark.parametrize(
    "params",
    [{}, {"q": "a"}, {"author": "e", "format": "epub", "genre": "a", "category": "o"}],
    ids=["all", "search", "relation-filters"],
)
def test_library_books_query_count_is_independent_of_page_size(params: dict[str, str]) -> None:
    _seed_books()
    client = _client()
    client.get("/library/books/")  # warm session/profile caches

    small = _count_queries(client, "/library/books/", {**params, "page_size": 1})
    large = _count_queries(client, "/library/books/", {**params, "page_size": 100})
    html = _count_queries(client, "/library/books/", {**params, "page_size": 100, "view": "html"})

    assert len(small) == len(large)
    assert len(html) == len(large)
    # count + page + four prefetches, plus session/auth/profile lookups.
    assert len(large) <= 10
    assert not [sql for sql in large if "DISTINCT" in sql.upper()]


@pytest.mark.django_db
def test_library_home_query_count_is_independent_of_book_count() -> None:
    cache.clear()
    client = _client()
    client.get("/library/")
    _seed_books()
    # The first landing request computes the cached global facets and language shelves.
    cold = _count_queries(client, "/library/", {"view": "html"})
    few = _count_queries(client, "/library/", {"view": "html"})

    for index in range(10):
        book = Book.objects.create(title=f"Extra {index}", language="en")
        book.authors.add(Author.objects.get_or_create(full_name="Extra Author")[0])
        book.genres.add(Genre.objects.get_or_create(name="Extra")[0])
    many = _count_queries(client, "/library/", {"view": "html"})

    assert len(few) == len(many)
    # Design acceptance (#28 phase 3): the landing page renders in at most 10 queries.
    assert len(many) <= 10
    assert len(cold) <= 25
    cache.clear()
