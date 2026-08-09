from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from django.conf import settings as django_settings
from django.test import Client

from ebook_server.core.models import (
    Book,
    CoverImage,
    FileVariant,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage


def _seed_deletable_book(tmp_path: Path) -> tuple[Book, LocalFilesystemStorage, list[str]]:
    django_settings.APP_DATA_DIR = tmp_path
    storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    location = StorageLocation.objects.create(
        identifier="default",
        backend=StorageLocation.BackendType.LOCAL,
        base_path="library-artifacts",
    )
    book = Book.objects.create(title="Delete Me Exactly")
    identifiers: list[str] = []
    for name, payload, format_name, mime_type in [
        ("book.epub", b"epub-delete-payload", "epub", "application/epub+zip"),
        ("book.pdf", b"%PDF-delete-payload", "pdf", "application/pdf"),
    ]:
        source = tmp_path / f"source-{name}"
        source.write_bytes(payload)
        identifier = storage.generate_destination(f"books/{book.id}", name)
        storage.store(str(source), identifier)
        identifiers.append(identifier)
        FileVariant.objects.create(
            book=book,
            format=format_name,
            mime_type=mime_type,
            original_filename=name,
            storage_location=location,
            storage_identifier=identifier,
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            source_type=FileVariant.SourceType.UPLOAD,
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )

    cover_payload = b"front-cover"
    cover_source = tmp_path / "front.png"
    cover_source.write_bytes(cover_payload)
    cover_identifier = storage.generate_destination(f"books/{book.id}", "front.png")
    storage.store(str(cover_source), cover_identifier)
    identifiers.append(cover_identifier)
    cover = CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.FRONT,
        storage_location=location,
        storage_identifier=cover_identifier,
        mime_type="image/png",
        size_bytes=len(cover_payload),
        sha256=hashlib.sha256(cover_payload).hexdigest(),
    )
    book.front_cover = cover
    book.save(update_fields=["front_cover"])
    return book, storage, identifiers


@pytest.mark.django_db(transaction=True)
def test_only_admin_can_open_or_submit_book_deletion(tmp_path: Path) -> None:
    book, storage, identifiers = _seed_deletable_book(tmp_path)
    profile = get_or_create_profile("ordinary-deleter")
    client = Client()
    client.force_login(profile.user)

    assert client.get(f"/library/books/{book.id}/delete/").status_code == 403
    assert (
        client.post(
            f"/library/books/{book.id}/delete/",
            {"confirm_title": book.title},
        ).status_code
        == 403
    )
    assert Book.objects.filter(pk=book.id).exists()
    assert all(storage.exists(identifier) for identifier in identifiers)


@pytest.mark.django_db(transaction=True)
def test_admin_deletion_requires_exact_title_and_hash_integrity(tmp_path: Path) -> None:
    book, storage, identifiers = _seed_deletable_book(tmp_path)
    admin = get_or_create_profile("book-admin", role="admin")
    client = Client()
    client.force_login(admin.user)

    wrong_title = client.post(
        f"/library/books/{book.id}/delete/",
        {"confirm_title": "delete me exactly"},
    )
    assert wrong_title.status_code == 400

    storage.resolve_location(identifiers[0]).write_bytes(b"tampered")
    bad_hash = client.post(
        f"/library/books/{book.id}/delete/",
        {"confirm_title": book.title},
    )
    assert bad_hash.status_code == 400
    assert b"hash mismatch" in bad_hash.content
    assert Book.objects.filter(pk=book.id).exists()


@pytest.mark.django_db(transaction=True)
def test_admin_deletion_quarantines_then_removes_database_and_files(tmp_path: Path) -> None:
    book, storage, identifiers = _seed_deletable_book(tmp_path)
    admin = get_or_create_profile("book-admin-success", role="admin")
    client = Client()
    client.force_login(admin.user)

    confirmation = client.get(
        f"/library/books/{book.id}/delete/?view=html",
        HTTP_ACCEPT="text/html",
    )
    response = client.post(
        f"/library/books/{book.id}/delete/",
        {"confirm_title": book.title, "verify_hashes": "true"},
    )

    assert confirmation.status_code == 200
    assert b"Type the exact title" in confirmation.content
    assert response.status_code == 200
    assert response.json()["artifact_count"] == 3
    assert not Book.objects.filter(pk=book.id).exists()
    assert all(not storage.exists(identifier) for identifier in identifiers)
