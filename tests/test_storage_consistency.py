from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from ebook_server.core.models import Book, FileVariant, StorageLocation


def _variant(tmp_path: Path, payload: bytes) -> FileVariant:
    settings.APP_DATA_DIR = tmp_path
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    artifact = tmp_path / "library-artifacts" / "books" / "1" / "book.pdf"
    artifact.parent.mkdir(parents=True)
    artifact.write_bytes(payload)
    return FileVariant.objects.create(
        book=Book.objects.create(title="Storage check"),
        format="pdf",
        mime_type="application/pdf",
        original_filename="book.pdf",
        storage_location=location,
        storage_identifier="books/1/book.pdf",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
    )


@pytest.mark.django_db(transaction=True)
def test_verify_storage_accepts_existing_artifact(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _variant(tmp_path, b"valid")

    call_command("verify_storage", verify_hashes=True)

    assert "checked=1 missing=0 mismatched=0 errors=0" in capsys.readouterr().out


@pytest.mark.django_db(transaction=True)
def test_verify_storage_reports_missing_artifact(tmp_path: Path) -> None:
    variant = _variant(tmp_path, b"missing")
    (tmp_path / "library-artifacts" / variant.storage_identifier).unlink()

    with pytest.raises(CommandError, match="storage verification failed"):
        call_command("verify_storage")


@pytest.mark.django_db(transaction=True)
def test_verify_storage_reports_hash_mismatch(tmp_path: Path) -> None:
    variant = _variant(tmp_path, b"original")
    (tmp_path / "library-artifacts" / variant.storage_identifier).write_bytes(b"changed")

    with pytest.raises(CommandError, match="storage verification failed"):
        call_command("verify_storage", verify_hashes=True)
