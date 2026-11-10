from __future__ import annotations

import os
import time
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.test import override_settings

from ebook_server.core import intake
from ebook_server.core.models import (
    Author,
    Book,
    ImportJob,
    SourceLocation,
    StorageLocation,
)
from ebook_server.storage import LocalFilesystemStorage, cap_filename


def _location(root: Path) -> SourceLocation:
    return SourceLocation.objects.create(
        name=f"src-{root.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        recursive=True,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
        is_active=True,
    )


def test_normalize_filename_caps_length_and_keeps_extension() -> None:
    name = intake.normalize_filename("a" * 400 + ".epub")
    assert len(name) <= 150
    assert name.endswith(".epub")
    assert cap_filename("b" * 300 + ".pdf").endswith(".pdf")


def test_generate_destination_stays_well_under_column_limit(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    destination = storage.generate_destination(
        "books/12345678-1234-1234-1234-123456789012",
        "x" * 400 + ".pdf",
    )
    assert len(destination) < 255


@pytest.mark.django_db(transaction=True)
def test_book_fields_are_truncated_to_column_limits() -> None:
    book = intake._provision_book_from_manual_metadata(
        title="T" * 600,
        authors=["A" * 400],
        publisher="P" * 400,
    )
    assert len(book.title) == 255
    assert len(book.publisher) == 255
    assert len(Author.objects.get().full_name) == 255
    assert Book.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_discovery_skips_hidden_temp_and_recent_files(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    root.mkdir()
    old = time.time() - 3600
    for name in ("good.txt", ".hidden.txt", "~lock.txt"):
        path = root / name
        path.write_text("hello world")
        os.utime(path, (old, old))
    (root / ".hidden-dir").mkdir()
    inside = root / ".hidden-dir" / "inner.txt"
    inside.write_text("hidden")
    os.utime(inside, (old, old))
    (root / "fresh.txt").write_text("being written")

    with override_settings(BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS=60):
        names = {p.name for p in intake.discover_source_paths(_location(root))}

    assert names == {"good.txt"}


@pytest.mark.django_db(transaction=True)
def test_complete_job_with_unchanged_file_is_not_recreated(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    root.mkdir()
    source = root / "book.txt"
    source.write_text("some book text")
    location = _location(root)

    first = intake.discover_source_jobs_for_location(location)
    assert len(first) == 1
    first[0].state = ImportJob.State.COMPLETE
    first[0].save(update_fields=["state"])

    assert intake.discover_source_jobs_for_location(location) == []

    source.write_text("changed contents, new size")
    assert len(intake.discover_source_jobs_for_location(location)) == 1


@pytest.mark.django_db(transaction=True)
def test_failed_jobs_back_off_and_are_capped(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    root.mkdir()
    (root / "book.txt").write_text("some book text")
    location = _location(root)

    job = intake.discover_source_jobs_for_location(location)[0]
    job.state = ImportJob.State.FAILED
    job.save(update_fields=["state"])
    assert intake.discover_source_jobs_for_location(location) == []  # backing off

    ImportJob.objects.filter(pk=job.pk).update(updated_at=datetime.now(UTC) - timedelta(days=1))
    second = intake.discover_source_jobs_for_location(location)
    assert len(second) == 1

    with override_settings(BOOK_SCANNER_INGEST_MAX_AUTO_ATTEMPTS=1):
        second[0].state = ImportJob.State.FAILED
        second[0].save(update_fields=["state"])
        ImportJob.objects.update(updated_at=datetime.now(UTC) - timedelta(days=30))
        assert intake.discover_source_jobs_for_location(location) == []


def _failed_job(tmp_path: Path, *, next_retry_at: datetime | None, attempts: int) -> ImportJob:
    source = tmp_path / f"retry-me-{attempts}.txt"
    source.write_text("A plain text book that should import on retry.")
    user, _ = get_user_model().objects.get_or_create(username="retry-owner")
    return ImportJob.objects.create(
        source_type=ImportJob.SourceType.UPLOAD,
        state=ImportJob.State.FAILED,
        uploader=user,
        source_path=str(source),
        original_filename=source.name,
        normalized_filename=source.name,
        attempt_count=attempts,
        next_retry_at=next_retry_at,
        last_error="boom",
    )


@pytest.mark.django_db(transaction=True)
def test_failed_job_is_rerun_by_queue_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [],
    )
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(tmp_path / "library-artifacts")),
    )
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    job = _failed_job(tmp_path, next_retry_at=datetime.now(UTC), attempts=1)

    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.LOW_CONFIDENCE


@pytest.mark.django_db(transaction=True)
def test_failed_job_without_retry_budget_stays_failed(tmp_path: Path) -> None:
    exhausted = _failed_job(tmp_path, next_retry_at=datetime.now(UTC), attempts=99)
    unscheduled = _failed_job(tmp_path, next_retry_at=None, attempts=0)

    intake.process_import_job(str(exhausted.id))
    intake.process_import_job(str(unscheduled.id))

    exhausted.refresh_from_db()
    unscheduled.refresh_from_db()
    assert exhausted.state == ImportJob.State.FAILED
    assert unscheduled.state == ImportJob.State.FAILED


def test_epub_identifier_scan_prefers_isbn_over_leading_uuid(tmp_path: Path) -> None:
    epub = tmp_path / "calibre.epub"
    container = (
        '<?xml version="1.0"?><container version="1.0" '
        'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
        '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
        "</rootfiles></container>"
    )
    opf = (
        '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">'
        "<dc:title>Dune</dc:title>"
        '<dc:identifier opf:scheme="calibre">3f2504e0-4f89-11d3-9a0c-0305e82c3301</dc:identifier>'
        '<dc:identifier opf:scheme="ISBN">9780306406157</dc:identifier>'
        "</metadata></package>"
    )
    with zipfile.ZipFile(epub, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr("META-INF/container.xml", container)
        archive.writestr("content.opf", opf)

    metadata = intake.extract_metadata(str(epub), "epub", original_filename="calibre.epub")

    assert metadata["identifier"] == "9780306406157"
