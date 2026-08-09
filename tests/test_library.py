from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.http import StreamingHttpResponse
from django.test import Client

from ebook_server.core.models import (
    Author,
    Book,
    Category,
    CoverImage,
    FileVariant,
    Genre,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage


def _configure_storage(tmp_path: Path) -> LocalFilesystemStorage:
    django_settings.APP_DATA_DIR = tmp_path
    storage_root = tmp_path / "library-artifacts"
    storage = LocalFilesystemStorage(str(storage_root))
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return storage


def _seed_library_variant(
    book: Book,
    storage: LocalFilesystemStorage,
    *,
    filename: str,
    fmt: str,
    mime: str,
    contents: bytes,
) -> FileVariant:
    source = storage.resolve_location(f"seed-source-{uuid4()}.bin")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(contents)
    destination = storage.generate_destination(f"books/{uuid4()}", filename)
    storage.store(str(source), destination)

    return FileVariant.objects.create(
        book=book,
        format=fmt,
        mime_type=mime,
        original_filename=filename,
        storage_location=StorageLocation.objects.get(identifier="default"),
        storage_identifier=destination,
        size_bytes=len(contents),
        sha256="0" * 64,
        source_type=FileVariant.SourceType.UPLOAD,
    )


def _seed_cover_image(
    book: Book,
    storage: LocalFilesystemStorage,
    *,
    position: str,
    filename: str,
) -> None:
    source = storage.resolve_location(f"seed-cover-{uuid4()}.bin")
    source.write_bytes(b"cover")
    destination = storage.generate_destination(f"books/{uuid4()}", filename)
    storage.store(str(source), destination)
    image = CoverImage.objects.create(
        book=book,
        position=position,
        storage_location=StorageLocation.objects.get(identifier="default"),
        storage_identifier=destination,
        mime_type="image/png",
        size_bytes=5,
        sha256="f" * 64,
    )
    if position == CoverImage.CoverPosition.FRONT:
        book.front_cover = image
    else:
        book.back_cover = image
    book.save(update_fields=["front_cover_id", "back_cover_id"])


@pytest.mark.django_db(transaction=True)
def test_library_search_filters_and_detail(tmp_path: Path) -> None:
    profile = get_or_create_profile("reader")
    storage = _configure_storage(tmp_path)

    book_alpha = Book.objects.create(
        title="Alpha PDF Guide",
        language="en",
        subtitle="A practical introduction",
        publisher="Acme",
        description="book about workflows",
    )
    Author.objects.create(full_name="Alice")
    author_alice = Author.objects.get(full_name="Alice")
    book_alpha.authors.add(author_alice)
    Category.objects.create(name="Reference")
    book_alpha.categories.add(Category.objects.get(name="Reference"))
    Genre.objects.create(name="Manual")
    book_alpha.genres.add(Genre.objects.get(name="Manual"))
    _seed_library_variant(
        book_alpha,
        storage,
        filename="alpha.pdf",
        fmt="pdf",
        mime="application/pdf",
        contents=b"%PDF-1.4\n",
    )

    book_beta = Book.objects.create(
        title="Beta epub Guide",
        language="fr",
        subtitle="Another one",
        publisher="Acme",
    )
    Author.objects.create(full_name="Bob")
    book_beta.authors.add(Author.objects.get(full_name="Bob"))
    _seed_library_variant(
        book_beta,
        storage,
        filename="beta.epub",
        fmt="epub",
        mime="application/epub+zip",
        contents=b"epub-content",
    )

    client = Client()
    client.force_login(profile.user)

    response = client.get("/library/books/?q=alpha")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["title"] == "Alpha PDF Guide"

    response = client.get("/library/books/?author=Alice")
    assert response.status_code == 200
    assert response.json()["count"] == 1

    response = client.get("/library/books/?format=epub")
    assert response.status_code == 200
    assert response.json()["count"] == 1
    assert response.json()["items"][0]["title"] == "Beta epub Guide"

    detail_response = client.get(f"/library/books/{book_alpha.id}/")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["id"] == book_alpha.id
    assert detail["variants"] and detail["variants"][0]["format"] == "pdf"


@pytest.mark.django_db(transaction=True)
def test_library_book_cover_and_download(tmp_path: Path) -> None:
    profile = get_or_create_profile("reader-downloader")
    storage = _configure_storage(tmp_path)

    book = Book.objects.create(
        title="Downloadable",
        language="en",
    )
    _seed_library_variant(
        book,
        storage,
        filename="download.epub",
        fmt="epub",
        mime="application/epub+zip",
        contents=b"dummy-epub",
    )
    _seed_cover_image(
        book,
        storage,
        position=CoverImage.CoverPosition.FRONT,
        filename="front.png",
    )

    client = Client()
    client.force_login(profile.user)
    response = client.get(f"/library/books/{book.id}/")
    assert response.status_code == 200
    detail = response.json()
    assert detail["cover_front"]["position"] == "front"
    assert detail["cover_front"]["storage_identifier"]

    variant_id = detail["variants"][0]["id"]
    response = client.get(f"/library/books/{book.id}/download/?variant_id={variant_id}")
    assert response.status_code == 200
    assert response["Content-Type"] == "application/epub+zip"
    assert response["Content-Length"] == str(len(b"dummy-epub"))
    streaming_response = cast(StreamingHttpResponse, response)
    chunks = cast(Iterable[bytes], streaming_response.streaming_content)
    assert b"".join(chunks) == b"dummy-epub"


@pytest.mark.django_db(transaction=True)
def test_library_permissions(tmp_path: Path) -> None:
    book = Book.objects.create(title="Open")
    profile = get_or_create_profile("blocked-reader", is_disabled=True)
    client = Client()
    client.force_login(profile.user)

    assert client.get("/library/books/").status_code == 403
    assert client.get(f"/library/books/{book.id}/").status_code == 403
    assert client.get(f"/library/books/{book.id}/download/?format=pdf").status_code == 403
