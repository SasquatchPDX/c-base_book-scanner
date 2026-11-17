"""Publication year stored without fabricated day precision (#38)."""

from __future__ import annotations

import importlib
from datetime import date
from typing import Any

import pytest
from django.apps import apps
from django.test import Client

from ebook_server.core import intake
from ebook_server.core.identification import IdentificationCandidate
from ebook_server.core.models import Book, clean_publication_year, get_or_create_profile

migration = importlib.import_module("ebook_server.core.migrations.0020_book_publication_year")


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (2003, 2003),
        ("1857", 1857),
        (0, None),
        (10000, None),
        ("n/a", None),
        (None, None),
        (True, None),
    ],
)
def test_clean_publication_year(value: object, expected: int | None) -> None:
    assert clean_publication_year(value) == expected


def _candidate(**overrides: Any) -> IdentificationCandidate:
    values: dict[str, Any] = {
        "provider_id": "openlibrary",
        "provider_ref": "/works/OL1W",
        "title": "Absolution Gap",
        "authors": ["Alastair Reynolds"],
        "isbn10": None,
        "isbn13": None,
        "language": "eng",
        "description": None,
        "publisher": None,
        "edition": None,
        "publication_year": 2003,
        "score": 1.0,
        "raw": {},
    }
    values.update(overrides)
    return IdentificationCandidate(**values)


@pytest.mark.django_db
def test_provisioned_book_stores_year_not_first_of_january() -> None:
    book = intake._provision_book_from_metadata(
        candidate=_candidate(), fallback_title="x", extra_metadata={}
    )
    book.refresh_from_db()
    assert book.publication_year == 2003
    assert book.publication_date is None


@pytest.mark.django_db
def test_out_of_range_provider_year_is_dropped() -> None:
    book = intake._provision_book_from_metadata(
        candidate=_candidate(publication_year=0), fallback_title="x", extra_metadata={}
    )
    book.refresh_from_db()
    assert book.publication_year is None


@pytest.mark.django_db
def test_manual_metadata_stores_year() -> None:
    book = intake._provision_book_from_manual_metadata(title="Momo", publication_year=1973)
    book.refresh_from_db()
    assert (book.publication_year, book.publication_date) == (1973, None)


@pytest.mark.django_db
def test_book_api_and_page_show_year_without_fabricated_day() -> None:
    profile = get_or_create_profile("reader")
    book = Book.objects.create(title="Absolution Gap", publication_year=2003)
    client = Client()
    client.force_login(profile.user)
    payload = client.get(f"/library/books/{book.id}/").json()
    assert payload["publication_year"] == 2003
    assert payload["publication_date"] is None
    page = client.get(f"/library/books/{book.id}/?view=html").content.decode()
    assert "<dd>2003</dd>" in page
    assert "Jan" not in page


@pytest.mark.django_db
def test_book_api_keeps_a_genuine_full_date() -> None:
    profile = get_or_create_profile("reader")
    book = Book.objects.create(title="Dated", publication_date=date(1999, 6, 15))
    client = Client()
    client.force_login(profile.user)
    payload = client.get(f"/library/books/{book.id}/").json()
    assert payload["publication_date"] == "1999-06-15"
    assert payload["publication_year"] == 1999


@pytest.mark.django_db
def test_migration_moves_year_only_dates_and_reverses() -> None:
    year_only = Book.objects.create(title="Year only", publication_date=date(2003, 1, 1))
    full = Book.objects.create(title="Full", publication_date=date(1999, 6, 15))
    undated = Book.objects.create(title="Undated")

    migration.split_year_only_dates(apps, None)
    for book in (year_only, full, undated):
        book.refresh_from_db()
    assert (year_only.publication_year, year_only.publication_date) == (2003, None)
    assert (full.publication_year, full.publication_date) == (1999, date(1999, 6, 15))
    assert (undated.publication_year, undated.publication_date) == (None, None)

    migration.restore_year_only_dates(apps, None)
    year_only.refresh_from_db()
    assert year_only.publication_date == date(2003, 1, 1)
