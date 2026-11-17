"""Scanner logs (libreflip ``scan.log``) kept next to the book they belong to (#7).

A scanner uploads its log for a scan job. When the job is already linked to a Book the
log is written straight into that book's archive folder on the attached storage
(``<Author>/<Title>/scan.log`` under the default layout). Otherwise it waits in scan
storage and ``place_scan_logs_in_book_folder`` moves it there during finalization,
deleting the scan-storage copy only after the archive copy's SHA-256 is verified.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import structlog
from django.conf import settings as django_settings
from django.db import IntegrityError

from ebook_server.storage import LocalFilesystemStorage

from . import archive_layout, intake
from .models import ScanJob, ScanLog, StorageLocation
from .storage_registry import filesystem_backend_for_location

logger = structlog.get_logger(__name__)

# Logs are small text files; anything bigger is not a scanner log.
MAX_SCAN_LOG_BYTES = 8 * 1024 * 1024
SCAN_STORAGE_DIRNAME = "scan-artifacts"
SCAN_LOG_FILENAME = "scan.log"


class ScanLogError(ValueError):
    """Raised for an upload that is not an acceptable scanner log."""


def _scan_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(str(Path(django_settings.APP_DATA_DIR) / SCAN_STORAGE_DIRNAME))


def _scan_location() -> StorageLocation:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="scan",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": SCAN_STORAGE_DIRNAME,
        },
    )
    return location


def _book_destination(storage: LocalFilesystemStorage, job: ScanJob) -> str:
    assert job.book is not None
    return archive_layout.derived_destination(
        storage,
        job.book,
        SCAN_LOG_FILENAME,
        legacy_namespace=f"books/{job.book_id}/scan-logs",
        legacy_filename=f"{uuid.uuid4().hex[:8]}-{SCAN_LOG_FILENAME}",
    )


def _validate_staged_log(path: Path) -> None:
    size = path.stat().st_size
    if size == 0:
        raise ScanLogError("empty log")
    if size > MAX_SCAN_LOG_BYTES:
        raise ScanLogError("log too large")
    with path.open("rb") as handle:
        if b"\x00" in handle.read(64 * 1024):
            raise ScanLogError("log is not a text file")


def store_scan_log(
    job: ScanJob, uploaded_file: Any, uploader_id: int | None
) -> tuple[ScanLog, bool]:
    """Store one uploaded scanner log; returns ``(log, created)``.

    Idempotent: re-uploading the same content for the same job returns the existing
    record without writing a second file.
    """
    declared_size = getattr(uploaded_file, "size", None)
    if isinstance(declared_size, int) and declared_size > MAX_SCAN_LOG_BYTES:
        raise ScanLogError("log too large")
    staging_root = Path(django_settings.APP_DATA_DIR) / SCAN_STORAGE_DIRNAME / "uploads"
    staged_path, _ = intake.write_upload_to_staging(uploaded_file, staging_root)
    staged = Path(staged_path)
    try:
        _validate_staged_log(staged)
        sha256 = intake.compute_sha256(staged_path)
        existing = ScanLog.objects.filter(scan_job=job, sha256=sha256).first()
        if existing is not None:
            return existing, False
        if job.book_id is not None:
            storage = intake._default_storage()
            location = intake._default_storage_location()
            destination = _book_destination(storage, job)
        else:
            storage = _scan_storage()
            location = _scan_location()
            destination = storage.generate_destination(
                f"scanning/{job.pk}/logs",
                f"{uuid.uuid4().hex[:8]}-{SCAN_LOG_FILENAME}",
            )
        storage.ensure_free_space(staged.stat().st_size)
        storage.store(staged_path, destination)
        original = Path(str(getattr(uploaded_file, "name", "") or SCAN_LOG_FILENAME)).name
        try:
            log = ScanLog.objects.create(
                scan_job=job,
                storage_location=location,
                storage_identifier=destination,
                original_filename=original[:255] or SCAN_LOG_FILENAME,
                size_bytes=staged.stat().st_size,
                sha256=sha256,
                uploader_id=uploader_id,
            )
        except IntegrityError:
            # A concurrent identical upload won; drop our copy.
            storage.delete(destination)
            return ScanLog.objects.get(scan_job=job, sha256=sha256), False
        logger.info(
            "scan_log_stored",
            scan_job_id=str(job.pk),
            scan_log_id=log.pk,
            in_book_folder=job.book_id is not None,
        )
        return log, True
    finally:
        staged.unlink(missing_ok=True)


def place_scan_logs_in_book_folder(job: ScanJob) -> int:
    """Move a job's logs still in scan storage into its book folder; returns the count.

    Copy, verify the SHA-256 of the archive copy, repoint the record, then delete the
    scan-storage copy. A failed verification leaves the original where it was.
    """
    if job.book_id is None:
        return 0
    library_location = intake._default_storage_location()
    library = intake._default_storage()
    moved = 0
    for log in job.logs.exclude(storage_location=library_location).select_related(
        "storage_location",
    ):
        source_location = log.storage_location
        source = (
            _scan_storage()
            if source_location.identifier == "scan"
            else filesystem_backend_for_location(source_location)
        )
        destination = _book_destination(library, job)
        library.store(str(source.resolve_location(log.storage_identifier)), destination)
        if library.hash(destination) != log.sha256:
            library.delete(destination)
            logger.error("scan_log_move_verification_failed", scan_log_id=log.pk)
            continue
        old_identifier = log.storage_identifier
        log.storage_location = library_location
        log.storage_identifier = destination
        log.save(update_fields=["storage_location", "storage_identifier", "updated_at"])
        if not source_location.read_only:
            source.delete(old_identifier)
            source.prune_empty_parents(old_identifier)
        moved += 1
    return moved
