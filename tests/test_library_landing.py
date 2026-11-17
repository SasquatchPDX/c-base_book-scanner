"""Search-first landing page, facet links, author A-Z index (#28 phases 2-3).

These run on SQLite through the portable fallback backend; the same views on
the PostgreSQL backend are covered by `tests/integration/test_search_postgres.py`.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import timedelta
from html import unescape
from typing import Any
from urllib.parse import parse_qs, urlparse

import pytest
from django.core.cache import cache
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ebook_server.core import search
from ebook_server.core.models import (
    AppUserProfile,
    Author,
    Book,
    Category,
    CoverImage,
    FileVariant,
    Genre,
    ImportJob,
    ReviewTask,
    StorageLocation,
    get_or_create_profile,
)

HTML = {"Accept": "text/html"}


@pytest.fixture(autouse=True)
def _clean_cache() -> Iterator[None]:
    cache.clear()
    search._pending.book_ids.clear()
    yield
    search._pending.book_ids.clear()
    cache.clear()


def _client(username: str = "landing-reader") -> tuple[Client, AppUserProfile]:
    profile = get_or_create_profile(username)
    client = Client()
    client.force_login(profile.user)
    return client, profile


def _location() -> StorageLocation:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return location


def _variant(book: Book, fmt: str) -> FileVariant:
    return FileVariant.objects.create(
        book=book,
        format=fmt,
        mime_type="application/octet-stream",
        original_filename=f"{book.pk}.{fmt}",
        storage_location=_location(),
        storage_identifier=f"books/{book.pk}.{fmt}",
        size_bytes=1,
        sha256=f"{book.pk:064x}",
        source_type=FileVariant.SourceType.UPLOAD,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )


def _cover(book: Book) -> CoverImage:
    cover = CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.FRONT,
        storage_location=_location(),
        storage_identifier=f"books/{book.pk}/cover.jpg",
        mime_type="image/jpeg",
        size_bytes=1,
        sha256=f"{book.pk:064x}",
    )
    book.front_cover = cover
    book.save(update_fields=["front_cover"])
    return cover


def _library() -> dict[str, Any]:
    """Nine books across languages, formats, decades, genres and categories."""
    now = timezone.now()
    ada = Author.objects.create(full_name="Ada Lovelace")
    bob = Author.objects.create(full_name="Bob Byte")
    cyd = Author.objects.create(full_name="Cyd Charisse")
    Author.objects.create(full_name="Orphan Author")  # no books: never listed
    fantasy = Genre.objects.create(name="Fantasy")
    manual = Genre.objects.create(name="Manual")
    computing = Category.objects.create(name="Computing")
    rows = [
        ("Analytical Engines", "en", 1843, [ada], [manual], [computing], ["epub", "pdf"], True),
        ("Bits and Bytes", "en", 1995, [bob], [manual], [computing], ["epub"], False),
        ("Charming Dances", "de", 1952, [cyd], [], [], ["pdf"], True),
        ("Dragons of Code", "en", 1998, [ada, bob], [fantasy], [computing], ["epub"], False),
        ("Elfen und Bytes", "de", 1991, [bob], [fantasy], [], ["mobi"], False),
        ("Fables", "fr", None, [], [fantasy], [], [], False),
        ("Gears", "de", 1847, [ada], [], [], ["epub"], True),
        ("Hello Machines", "fr", 1999, [bob, cyd], [manual], [computing], ["pdf"], False),
        ("Isolated", "en", 2001, [], [], [], [], False),
    ]
    books = []
    for index, (title, language, year, authors, genres, categories, formats, cover) in enumerate(
        rows
    ):
        book = Book.objects.create(title=title, language=language, publication_year=year)
        Book.objects.filter(pk=book.pk).update(created_at=now - timedelta(hours=len(rows) - index))
        book.authors.set(authors)
        book.genres.set(genres)
        book.categories.set(categories)
        for fmt in formats:
            _variant(book, fmt)
        if cover:
            _cover(book)
        books.append(book)
    return {
        "books": books,
        "ada": ada,
        "bob": bob,
        "cyd": cyd,
        "fantasy": fantasy,
        "manual": manual,
        "computing": computing,
    }


def _titles(client: Client, query: str) -> list[str]:
    response = client.get(f"/library/books/?{query}")
    assert response.status_code == 200, query
    return [item["title"] for item in response.json()["items"]]


def _facet_hrefs(page: str) -> dict[str, str]:
    """Facet link label -> query string, from the landing page sidebar."""
    sidebar = page.split('class="facet-sidebar"', 1)[1]
    return {
        unescape(label): unescape(href).split("?", 1)[1]
        for href, label in re.findall(r'<li><a href="([^"]+)">([^<]+)</a>', sidebar)
    }


# ------------------------------------------------------------------ landing


@pytest.mark.django_db
def test_landing_page_is_search_first_with_shelves_facets_and_author_index() -> None:
    data = _library()
    client, _profile = _client()

    page = client.get("/library/", headers=HTML).content.decode()

    assert 'role="search"' in page
    assert 'name="q"' in page and 'action="/library/books/"' in page
    assert "Recently added" in page
    # Newest first; the newest book leads the shelf.
    recent = page.split('id="shelf-recently-added"', 1)[1].split("</section>", 1)[0]
    assert recent.index("Isolated") < recent.index("Hello Machines") < recent.index("Analytical")
    # Only identified-through-review books get the second shelf; none yet.
    assert "Recently identified" not in page
    # "New in <language>" skips the most common language (en: 4 books).
    assert "New in DE" in page and "New in FR" in page and "New in EN" not in page
    assert "Authors A–Z" in page and "/library/authors/?view=html&amp;letter=M" in page

    hrefs = _facet_hrefs(page)
    assert parse_qs(hrefs["EPUB"]) == {"view": ["html"], "format": ["epub"]}
    assert parse_qs(hrefs["Fantasy"])["genre_id"] == [str(data["fantasy"].pk)]
    assert parse_qs(hrefs["Computing"])["category_id"] == [str(data["computing"].pk)]
    assert parse_qs(hrefs["Bob Byte"])["author_id"] == [str(data["bob"].pk)]
    assert parse_qs(hrefs["1990s"])["decade"] == ["1990"]
    assert parse_qs(hrefs["With cover"])["has_cover"] == ["1"]
    assert parse_qs(hrefs["Without cover"])["has_cover"] == ["0"]
    # Counts are shown next to each link.
    assert re.search(r">EPUB</a> <span class=\"facet-count\"[^>]*>4</span>", page)
    assert re.search(r">1990s</a> <span class=\"facet-count\"[^>]*>4</span>", page)


@pytest.mark.django_db
def test_every_landing_facet_link_lists_exactly_the_counted_books() -> None:
    _library()
    client, _profile = _client()
    page = client.get("/library/", headers=HTML).content.decode()
    counts = {
        unescape(label): int(count)
        for label, count in re.findall(
            r'<li><a href="[^"]+">([^<]+)</a> <span class="facet-count"[^>]*>(\d+)</span>', page
        )
    }

    hrefs = _facet_hrefs(page)
    assert len(hrefs) >= 15
    for label, query in hrefs.items():
        params = {key: values[0] for key, values in parse_qs(query).items() if key != "view"}
        response = client.get("/library/books/", {**params, "page_size": "100"})
        assert response.status_code == 200, label
        assert response.json()["count"] == counts[label], label


@pytest.mark.django_db
def test_facet_counts_api_returns_keys_and_matches_brute_force() -> None:
    data = _library()
    client, _profile = _client()

    payload = client.get(
        "/library/books/", {"facets": "author,genre,has_cover,decade", "language": "en"}
    ).json()

    english = Book.objects.filter(language="en")
    authors = payload["facets"]["author"]
    assert {item["value"]: item["count"] for item in authors} == {
        "Ada Lovelace": 2,
        "Bob Byte": 2,
    }
    assert {item["key"] for item in authors} == {str(data["ada"].pk), str(data["bob"].pk)}
    assert {item["value"]: item["count"] for item in payload["facets"]["has_cover"]} == {
        "true": english.filter(front_cover__isnull=False).count(),
        "false": english.filter(front_cover__isnull=True).count(),
    }
    assert {item["value"]: item["count"] for item in payload["facets"]["decade"]} == {
        "1840": 1,
        "1990": 2,
        "2000": 1,
    }


@pytest.mark.django_db
def test_exact_filters_combine_and_reject_garbage() -> None:
    data = _library()
    client, _profile = _client()

    assert _titles(client, f"author_id={data['bob'].pk}&language=de") == ["Elfen und Bytes"]
    assert _titles(client, f"genre_id={data['manual'].pk}&sort=title") == [
        "Analytical Engines",
        "Bits and Bytes",
        "Hello Machines",
    ]
    assert _titles(client, "decade=1840&sort=title") == ["Analytical Engines", "Gears"]
    assert _titles(client, "decade=1990s&has_cover=1") == []
    assert _titles(client, "has_cover=yes&sort=title") == [
        "Analytical Engines",
        "Charming Dances",
        "Gears",
    ]
    # Unparseable values are ignored rather than failing the page.
    assert len(_titles(client, "author_id=abc&decade=x&has_cover=maybe")) == 9


@pytest.mark.django_db
def test_browse_list_shows_removable_filter_chips_and_keeps_exact_filters() -> None:
    data = _library()
    client, _profile = _client()

    page = client.get(
        "/library/books/",
        {"view": "html", "author_id": data["ada"].pk, "format": "epub"},
        headers=HTML,
    ).content.decode()

    assert "Author: Ada Lovelace" in page
    assert "Format: EPUB" in page
    # The remove link's name is generic, so it never repeats a book title or author.
    removal = re.search(
        r'Author: Ada Lovelace <a href="([^"]+)" aria-label="Remove author filter"', page
    )
    assert removal is not None
    assert "author_id" not in unescape(removal.group(1))
    # The search form keeps the exact filters as hidden inputs.
    assert f'name="author_id" value="{data["ada"].pk}"' in page


@pytest.mark.django_db
def test_recently_identified_shelf_lists_books_resolved_in_review() -> None:
    data = _library()
    client, profile = _client()
    now = timezone.now()
    for minutes, book in ((5, data["books"][0]), (1, data["books"][2])):
        job = ImportJob.objects.create(
            source_type=ImportJob.SourceType.UPLOAD,
            state=ImportJob.State.COMPLETE,
            uploader=profile.user,
            source_path="/staging/x.epub",
            original_filename="x.epub",
            normalized_filename="x.epub",
            detected_format="epub",
            mime_type="application/epub+zip",
        )
        ReviewTask.objects.create(
            import_job=job,
            source_filename="x.epub",
            state=ReviewTask.State.RESOLVED,
            decision=ReviewTask.Decision.ACCEPT_CANDIDATE,
            resolved_book=book,
            decided_at=now - timedelta(minutes=minutes),
        )

    page = client.get("/library/", headers=HTML).content.decode()

    shelf = page.split('id="shelf-recently-identified"', 1)[1].split("</section>", 1)[0]
    assert shelf.index("Charming Dances") < shelf.index("Analytical Engines")
    assert "Bits and Bytes" not in shelf


@pytest.mark.django_db
def test_empty_library_landing_hides_facets_and_invites_uploads() -> None:
    client, _profile = _client()

    page = client.get("/library/", headers=HTML).content.decode()

    assert "The collection is empty" in page
    assert "facet-sidebar" not in page


@pytest.mark.django_db
def test_landing_page_query_count_does_not_grow_with_shelf_contents() -> None:
    _library()
    client, _profile = _client()
    client.get("/library/", headers=HTML)  # fills the five-minute facet cache

    with CaptureQueriesContext(connection) as small:
        client.get("/library/", headers=HTML)
    for index in range(20):
        book = Book.objects.create(title=f"More {index}", language="de")
        book.authors.add(Author.objects.get_or_create(full_name=f"More Author {index}")[0])
        _cover(book)
    with CaptureQueriesContext(connection) as large:
        client.get("/library/", headers=HTML)

    assert len(small) == len(large)
    assert len(large) <= 10


# ------------------------------------------------------------- author index


def _author_page(client: Client, **params: Any) -> dict[str, Any]:
    response = client.get("/library/authors/", params)
    assert response.status_code == 200
    payload: dict[str, Any] = response.json()
    return payload


@pytest.mark.django_db
def test_author_index_walks_every_author_with_books_in_name_order() -> None:
    for index in range(23):
        author = Author.objects.create(full_name=f"Author {index:02d}")
        for copy in range(index % 3 + 1):
            Book.objects.create(title=f"Book {index}-{copy}").authors.add(author)
    Author.objects.create(full_name="Author 99 (no books)")
    client, _profile = _client()

    seen: list[str] = []
    payload = _author_page(client, page_size=5)
    while True:
        seen += [item["name"] for item in payload["items"]]
        if not payload["next_cursor"]:
            break
        payload = _author_page(client, page_size=5, cursor=payload["next_cursor"])

    assert seen == [f"Author {index:02d}" for index in range(23)]
    assert payload["items"][-1]["book_count"] == 22 % 3 + 1

    # Previous from the last page returns the page before it.
    previous = _author_page(client, page_size=5, cursor=payload["previous_cursor"])
    assert [item["name"] for item in previous["items"]] == [
        f"Author {index:02d}" for index in range(15, 20)
    ]


@pytest.mark.django_db
def test_author_index_letter_jump_and_cursor_tampering() -> None:
    for name in ("Anna Alpha", "Berta Beta", "Moritz Mu", "Nina Nu", "Zora Zeta"):
        Book.objects.create(title=name).authors.add(Author.objects.create(full_name=name))
    client, _profile = _client()

    jumped = _author_page(client, letter="m", page_size=2)
    assert [item["name"] for item in jumped["items"]] == ["Moritz Mu", "Nina Nu"]
    assert jumped["previous_cursor"] and jumped["next_cursor"]
    before = _author_page(client, cursor=jumped["previous_cursor"], page_size=2)
    assert [item["name"] for item in before["items"]] == ["Anna Alpha", "Berta Beta"]

    token = jumped["next_cursor"]
    tampered = token[:-1] + ("A" if token[-1] != "A" else "B")
    assert client.get("/library/authors/", {"cursor": tampered}).status_code == 400
    # A browse cursor is not an author cursor.
    browse_cursor = search.Cursor("title", "x", 1).encode()
    assert client.get("/library/authors/", {"cursor": browse_cursor}).status_code == 400


@pytest.mark.django_db
def test_author_index_html_links_to_filtered_book_list() -> None:
    data = _library()
    client, _profile = _client()

    page = client.get("/library/authors/", {"view": "html", "letter": "B"}).content.decode()

    assert f'href="/library/books/?view=html&amp;author_id={data["bob"].pk}"' in page
    assert "Ada Lovelace" not in page  # before the letter
    assert "Orphan Author" not in page
    assert 'aria-current="true">B</a>' in page


@pytest.mark.django_db
def test_author_index_query_count_is_independent_of_page_size() -> None:
    for index in range(40):
        Book.objects.create(title=f"B{index}").authors.add(
            Author.objects.create(full_name=f"Writer {index:02d}")
        )
    client, _profile = _client()
    client.get("/library/authors/")

    with CaptureQueriesContext(connection) as small:
        client.get("/library/authors/", {"page_size": "2"})
    with CaptureQueriesContext(connection) as large:
        client.get("/library/authors/", {"page_size": "40", "view": "html"})

    assert len(small) == len(large) <= 6


@pytest.mark.django_db
def test_author_sort_keyset_walk_matches_first_author_order() -> None:
    _library()
    client, _profile = _client()

    walked: list[str] = []
    payload = client.get("/library/books/", {"sort": "author", "page_size": "2"}).json()
    while True:
        walked += [item["title"] for item in payload["items"]]
        if not payload["next_cursor"]:
            break
        payload = client.get(
            "/library/books/",
            {"sort": "author", "page_size": 2, "cursor": payload["next_cursor"]},
        ).json()

    def first_author(book: Book) -> str:
        names = sorted(author.full_name for author in book.authors.all())
        return names[0] if names else ""

    expected = sorted(Book.objects.all(), key=lambda book: (first_author(book), book.pk))
    assert walked == [book.title for book in expected]


@pytest.mark.django_db
def test_landing_links_round_trip_through_the_browse_html() -> None:
    data = _library()
    client, _profile = _client()
    page = client.get("/library/", headers=HTML).content.decode()
    query = _facet_hrefs(page)["Ada Lovelace"]

    listing = client.get(f"/library/books/?{query}", headers=HTML).content.decode()

    assert urlparse(f"?{query}").query
    assert "Analytical Engines" in listing and "Gears" in listing
    assert "Bits and Bytes" not in listing
    assert data["ada"].full_name in listing


@pytest.mark.django_db
def test_seed_synthetic_library_builds_a_browsable_library(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from io import StringIO

    from django.core.management import call_command
    from django.core.management.base import CommandError

    from ebook_server.core.models import CoverThumbnail
    from ebook_server.settings import settings as app_settings

    call_command("seed_synthetic_library", "--books", "40", stdout=StringIO())

    assert Book.objects.count() == 40
    assert Book.objects.filter(search_document__isnull=True).count() == 0
    assert CoverThumbnail.objects.count() == 2 * Book.objects.exclude(front_cover=None).count()
    client, _profile = _client()
    page = client.get("/library/", headers=HTML).content.decode()
    assert "Recently identified" in page and "/library/covers/" in page

    monkeypatch.setattr(app_settings, "environment", "production")
    with pytest.raises(CommandError):
        call_command("seed_synthetic_library", "--books", "1", stdout=StringIO())
