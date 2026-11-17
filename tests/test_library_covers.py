"""Front covers on the browse list and the library home page."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from ebook_server.core.models import Book, CoverImage, get_or_create_profile
from tests.test_library import _configure_storage, _seed_cover_image

HTML = {"Accept": "text/html"}


def _client() -> Client:
    client = Client()
    client.force_login(get_or_create_profile("cover-reader").user)
    return client


@pytest.mark.django_db(transaction=True)
def test_browse_list_and_home_show_front_cover_or_placeholder(tmp_path: Path) -> None:
    storage = _configure_storage(tmp_path)
    (tmp_path / "library-artifacts").mkdir(exist_ok=True)
    with_cover = Book.objects.create(title="Neuromancer")
    _seed_cover_image(
        with_cover, storage, position=CoverImage.CoverPosition.FRONT, filename="c.png"
    )
    without_cover = Book.objects.create(title="Zebra Without Cover")
    client = _client()

    for url in ("/library/books/?view=html", "/library/"):
        page = client.get(url, headers=HTML).content.decode()
        cover_url = f"/library/books/{with_cover.id}/covers/front/"
        assert f'src="{cover_url}"' in page, url
        assert 'alt="Front cover of Neuromancer"' in page, url
        assert 'loading="lazy"' in page, url
        # The book without a cover keeps its letter placeholder.
        assert "Zebra Without Cover" in page, url
        assert f"/library/books/{without_cover.id}/covers/front/" not in page, url
        assert 'aria-hidden="true">Z</div>' in page, url

    image = client.get(f"/library/books/{with_cover.id}/covers/front/")
    assert image.status_code == 200
    assert image["Content-Type"] == "image/png"


@pytest.mark.django_db(transaction=True)
def test_browse_list_cover_lookup_does_not_query_per_book(tmp_path: Path) -> None:
    storage = _configure_storage(tmp_path)
    (tmp_path / "library-artifacts").mkdir(exist_ok=True)
    client = _client()

    def query_count() -> int:
        with CaptureQueriesContext(connection) as queries:
            assert client.get("/library/books/?view=html", headers=HTML).status_code == 200
        return len(queries)

    first = Book.objects.create(title="Book 1")
    _seed_cover_image(first, storage, position=CoverImage.CoverPosition.FRONT, filename="1.png")
    baseline = query_count()
    for index in range(2, 7):
        book = Book.objects.create(title=f"Book {index}")
        _seed_cover_image(book, storage, position=CoverImage.CoverPosition.FRONT, filename="c.png")
        _seed_cover_image(book, storage, position=CoverImage.CoverPosition.BACK, filename="b.png")
    more = query_count()

    # Covers come with the book query; per-book work is limited to the existing
    # format lookup (one query per book), not extra cover queries.
    assert more - baseline <= 5
