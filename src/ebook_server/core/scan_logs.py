"""Scanner logs (libreflip ``scan.log``) kept next to the book they belong to (#7).

Uploading a log only stages it: the request writes the (size-bounded) bytes into scan
staging, records a ``ScanLog`` in state ``queued`` together with a ``ProcessingJob``
(``job_type=scan_log``) and hands the job to the worker queue once the transaction
commits. Validation of the content, hashing and the copy into storage all happen in
``process_scan_log_job`` on a worker (AGENTS.md: slow work stays out of requests). The
log's ``state`` (``queued`` -> ``stored`` / ``duplicate`` / ``failed``) is reported on
the scan job's API.

When the scan job is already linked to a Book the worker writes the log straight into
that book's archive folder on the attached storage (``<Author>/<Title>/scan.log`` under
the default layout). Otherwise it waits in scan storage and
``place_scan_logs_in_book_folder`` moves it there during finalization, deleting the
scan-storage copy only after the archive copy's SHA-256 is verified.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import structlog
from django.conf import settings as django_settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from ebook_server.jobs import get_queue, is_stale_running
from ebook_server.storage import LocalFilesystemStorage, ensure_path_storage_ready

from . import archive_layout, intake
from .models import ProcessingJob, ScanJob, ScanLog, StorageLocation
from .storage_registry import filesystem_backend_for_location

logger = structlog.get_logger(__name__)

# Logs are small text files; anything bigger is not a scanner log.
MAX_SCAN_LOG_BYTES = 8 * 1024 * 1024
SCAN_STORAGE_DIRNAME = "scan-artifacts"
SCAN_LOG_FILENAME = "scan.log"
# Staged uploads live in scan storage under this namespace until the worker runs.
STAGING_NAMESPACE = "uploads/scan-logs"


class ScanLogError(ValueError):
    """Raised for an upload that is not an acceptable scanner log."""


class ScanLogDispatchError(RuntimeError):
    """The log was staged and recorded but could not be handed to the queue."""


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


def _publish_to_book_folder(storage: LocalFilesystemStorage, job: ScanJob, source: str) -> str:
    """Write ``source`` into the job's book folder; never overwrites an existing file."""
    assert job.book is not None
    return archive_layout.publish_derived(
        storage,
        job.book,
        source,
        SCAN_LOG_FILENAME,
        legacy_namespace=f"books/{job.book_id}/scan-logs",
        legacy_filename=f"{uuid.uuid4().hex[:8]}-{SCAN_LOG_FILENAME}",
    )


def _stage_bounded(uploaded_file: Any) -> tuple[str, int]:
    """Write the upload into scan staging, refusing it as soon as it exceeds the cap.

    Returns ``(staging identifier in scan storage, size)``. Only the bytes are written
    here; nothing is hashed or parsed.
    """
    storage = _scan_storage()
    identifier = storage.generate_destination(STAGING_NAMESPACE, f"{uuid.uuid4().hex}.log")
    target = storage.resolve_location(identifier)
    ensure_path_storage_ready(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        with os.fdopen(os.open(target, flags, 0o600), "wb") as handle:
            for chunk in uploaded_file.chunks():
                payload = chunk if isinstance(chunk, bytes) else str(chunk).encode()
                total += len(payload)
                if total > MAX_SCAN_LOG_BYTES:
                    raise ScanLogError("log too large")
                handle.write(payload)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    if total == 0:
        target.unlink(missing_ok=True)
        raise ScanLogError("empty log")
    return identifier, total


def stage_scan_log(job: ScanJob, uploaded_file: Any, uploader_id: int | None) -> ScanLog:
    """Stage one uploaded scanner log and queue its processing; returns the queued log.

    The request does only bounded I/O: the upload is written to scan staging (at most
    ``MAX_SCAN_LOG_BYTES``), then a ``ScanLog`` and its ``ProcessingJob`` are recorded
    and the job is enqueued after the transaction commits. Re-uploading content that
    is already stored for the job is detected by the worker (state ``duplicate``).
    """
    declared_size = getattr(uploaded_file, "size", None)
    if isinstance(declared_size, int) and declared_size > MAX_SCAN_LOG_BYTES:
        raise ScanLogError("log too large")
    staging_identifier, size = _stage_bounded(uploaded_file)
    original = Path(str(getattr(uploaded_file, "name", "") or SCAN_LOG_FILENAME)).name
    try:
        with transaction.atomic():
            processing_job = ProcessingJob.objects.create(
                job_type=ProcessingJob.JobType.SCAN_LOG,
                details={"scan_job_id": str(job.pk), "attempt_count": 0, "error": ""},
            )
            log = ScanLog.objects.create(
                scan_job=job,
                state=ScanLog.State.QUEUED,
                staging_identifier=staging_identifier,
                original_filename=original[:255] or SCAN_LOG_FILENAME,
                size_bytes=size,
                uploader_id=uploader_id,
                processing_job=processing_job,
            )
            processing_job.details = {**processing_job.details, "scan_log_id": log.pk}
            processing_job.save(update_fields=["details"])
            transaction.on_commit(lambda: _dispatch(processing_job, log))
    except BaseException:
        _scan_storage().delete(staging_identifier)
        raise
    logger.info("scan_log_staged", scan_job_id=str(job.pk), scan_log_id=log.pk, size_bytes=size)
    log.refresh_from_db()
    return log


def _dispatch(processing_job: ProcessingJob, log: ScanLog) -> None:
    queue = get_queue()
    if queue is None:
        # No queue configured (tests, single-process deployments): the job stays
        # queued for ``process_queued_scan_logs``.
        logger.warning("scan_log_not_dispatched", processing_job_id=str(processing_job.job_id))
        return
    try:
        queue.enqueue(process_scan_log_job, str(processing_job.job_id))
    except Exception as exc:
        error = f"queue dispatch failed: {exc}"
        ProcessingJob.objects.filter(pk=processing_job.pk).update(
            state=ProcessingJob.JobState.FAILED,
            finished_at=timezone.now(),
            details={**processing_job.details, "error": error},
        )
        ScanLog.objects.filter(pk=log.pk, state=ScanLog.State.QUEUED).update(
            state=ScanLog.State.FAILED, error=error[:500], updated_at=timezone.now()
        )
        raise ScanLogDispatchError(str(exc)) from exc


def _claim(job_id: str) -> ProcessingJob | None:
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update().filter(job_id=job_id).first()
        if job is None or job.job_type != ProcessingJob.JobType.SCAN_LOG:
            return None
        if job.state in {ProcessingJob.JobState.SUCCESS, ProcessingJob.JobState.FAILED}:
            return None
        if job.state == ProcessingJob.JobState.RUNNING and not is_stale_running(job):
            return None
        details = dict(job.details) if isinstance(job.details, dict) else {}
        details["attempt_count"] = int(details.get("attempt_count", 0)) + 1
        details["error"] = ""
        job.state = ProcessingJob.JobState.RUNNING
        job.started_at = timezone.now()
        job.finished_at = None
        job.details = details
        job.save(update_fields=["state", "started_at", "finished_at", "details"])
        return job


def _finish(job: ProcessingJob, *, failed: bool, error: str = "", **extra: Any) -> None:
    details = dict(job.details) if isinstance(job.details, dict) else {}
    job.state = ProcessingJob.JobState.FAILED if failed else ProcessingJob.JobState.SUCCESS
    job.finished_at = timezone.now()
    job.details = {**details, **extra, "error": error}
    job.save(update_fields=["state", "finished_at", "details"])


def _validate_staged(path: Path) -> None:
    size = path.stat().st_size
    if size == 0:
        raise ScanLogError("empty log")
    if size > MAX_SCAN_LOG_BYTES:
        raise ScanLogError("log too large")
    with path.open("rb") as handle:
        if b"\x00" in handle.read(64 * 1024):
            raise ScanLogError("log is not a text file")


def process_scan_log_job(job_id: str) -> None:
    """Worker: validate, hash and store one staged scanner log. Safe to re-run."""
    processing_job = _claim(job_id)
    if processing_job is None:
        return
    log = ScanLog.objects.filter(processing_job=processing_job).first()
    if log is None or log.state != ScanLog.State.QUEUED:
        _finish(processing_job, failed=False, skipped="log no longer queued")
        return
    scan_storage = _scan_storage()
    staged_identifier = log.staging_identifier
    staged = scan_storage.resolve_location(staged_identifier)
    try:
        _validate_staged(staged)
        sha256 = intake.compute_sha256(str(staged))
        existing = (
            ScanLog.objects.filter(
                scan_job_id=log.scan_job_id, sha256=sha256, state=ScanLog.State.STORED
            )
            .exclude(pk=log.pk)
            .first()
        )
        if existing is not None:
            _mark_duplicate(log, existing, sha256)
            _finish(processing_job, failed=False, duplicate_of=existing.pk)
            scan_storage.delete(staged_identifier)
            return
        job = ScanJob.objects.select_related("book").get(pk=log.scan_job_id)
        if job.book_id is not None:
            storage = intake._default_storage()
            location = intake._default_storage_location()
            storage.ensure_free_space(staged.stat().st_size)
            destination = _publish_to_book_folder(storage, job, str(staged))
        else:
            storage = scan_storage
            location = _scan_location()
            destination = storage.publish_unique(
                staged_identifier,
                f"scanning/{job.pk}/logs",
                f"{uuid.uuid4().hex[:8]}-{SCAN_LOG_FILENAME}",
            )
        try:
            with transaction.atomic():
                locked = ScanLog.objects.select_for_update().get(pk=log.pk)
                if locked.state != ScanLog.State.QUEUED:
                    raise _NoLongerQueuedError
                locked.state = ScanLog.State.STORED
                locked.sha256 = sha256
                locked.storage_location = location
                locked.storage_identifier = destination
                locked.staging_identifier = ""
                locked.error = ""
                locked.save(
                    update_fields=[
                        "state",
                        "sha256",
                        "storage_location",
                        "storage_identifier",
                        "staging_identifier",
                        "error",
                        "updated_at",
                    ]
                )
        except IntegrityError:
            # A concurrent identical upload was stored first; drop our copy.
            storage.delete(destination)
            winner = ScanLog.objects.get(
                scan_job_id=log.scan_job_id, sha256=sha256, state=ScanLog.State.STORED
            )
            _mark_duplicate(log, winner, sha256)
            _finish(processing_job, failed=False, duplicate_of=winner.pk)
            scan_storage.delete(staged_identifier)
            return
        except _NoLongerQueuedError:
            storage.delete(destination)
            _finish(processing_job, failed=False, skipped="log no longer queued")
            return
        scan_storage.delete(staged_identifier)
        _finish(processing_job, failed=False, scan_log_id=log.pk)
        logger.info(
            "scan_log_stored",
            scan_job_id=str(job.pk),
            scan_log_id=log.pk,
            in_book_folder=job.book_id is not None,
        )
        # The job may have been linked to a book (and finalized) while this ran.
        if job.book_id is None:
            fresh = ScanJob.objects.select_related("book").get(pk=job.pk)
            if fresh.book_id is not None:
                place_scan_logs_in_book_folder(fresh)
    except ScanLogError as exc:
        _fail(log, processing_job, str(exc))
        scan_storage.delete(staged_identifier)
    except Exception as exc:
        # Storage trouble: keep the staged bytes so an operator can re-run the job.
        _fail(log, processing_job, f"{type(exc).__name__}: {exc}")
        logger.exception("scan_log_processing_failed", scan_log_id=log.pk)
        raise


class _NoLongerQueuedError(Exception):
    pass


def _mark_duplicate(log: ScanLog, existing: ScanLog, sha256: str) -> None:
    ScanLog.objects.filter(pk=log.pk).update(
        state=ScanLog.State.DUPLICATE,
        sha256=sha256,
        duplicate_of=existing,
        staging_identifier="",
        updated_at=timezone.now(),
    )


def _fail(log: ScanLog, processing_job: ProcessingJob, error: str) -> None:
    ScanLog.objects.filter(pk=log.pk).update(
        state=ScanLog.State.FAILED, error=error[:500], updated_at=timezone.now()
    )
    _finish(processing_job, failed=True, error=error)


def queued_scan_log_job_ids() -> list[str]:
    return [
        str(job_id)
        for job_id in ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.SCAN_LOG,
            state=ProcessingJob.JobState.QUEUED,
        )
        .order_by("scheduled_at")
        .values_list("job_id", flat=True)
    ]


def serialize_scan_log(log: ScanLog) -> dict[str, Any]:
    location = log.storage_location
    return {
        "id": log.pk,
        "state": log.state,
        "original_filename": log.original_filename,
        "size_bytes": log.size_bytes,
        "sha256": log.sha256 or None,
        "in_book_folder": location is not None and location.identifier != "scan",
        "duplicate_of": log.duplicate_of_id,
        "error": log.error,
        "processing_job_id": (
            str(log.processing_job.job_id) if log.processing_job is not None else None
        ),
        "created_at": log.created_at.isoformat(),
        "updated_at": log.updated_at.isoformat(),
    }


def serialize_job_logs(job: ScanJob) -> list[dict[str, Any]]:
    return [
        serialize_scan_log(log)
        for log in job.logs.select_related("storage_location", "processing_job").order_by("pk")
    ]


def place_scan_logs_in_book_folder(job: ScanJob) -> int:
    """Move a job's stored logs still in scan storage into its book folder.

    Runs on a worker (scan finalization, or the log worker when the job was linked
    meanwhile). Each log is handled under its row lock so two runners never copy the
    same log twice: copy into the book folder (``publish_unique``, never overwriting),
    verify the SHA-256 of the archive copy, repoint the record, and delete the
    scan-storage copy only after the transaction commits. A failed verification
    leaves the original where it was. Returns the number of logs moved.
    """
    if job.book_id is None:
        return 0
    library_location = intake._default_storage_location()
    library = intake._default_storage()
    moved = 0
    candidate_ids = list(
        job.logs.filter(state=ScanLog.State.STORED)
        .exclude(storage_location=library_location)
        .values_list("pk", flat=True)
    )
    for log_id in candidate_ids:
        with transaction.atomic():
            log = (
                ScanLog.objects.select_for_update()
                .select_related("storage_location")
                .filter(pk=log_id, state=ScanLog.State.STORED)
                .exclude(storage_location=library_location)
                .first()
            )
            if log is None or log.storage_location is None:
                continue
            source_location = log.storage_location
            source = (
                _scan_storage()
                if source_location.identifier == "scan"
                else filesystem_backend_for_location(source_location)
            )
            destination = _publish_to_book_folder(
                library, job, str(source.resolve_location(log.storage_identifier))
            )
            if library.hash(destination) != log.sha256:
                library.delete(destination)
                logger.error("scan_log_move_verification_failed", scan_log_id=log.pk)
                continue
            old_identifier = log.storage_identifier
            log.storage_location = library_location
            log.storage_identifier = destination
            log.save(update_fields=["storage_location", "storage_identifier", "updated_at"])
            if not source_location.read_only:

                def _drop_old(
                    source: LocalFilesystemStorage = source, old: str = old_identifier
                ) -> None:
                    source.delete(old)
                    source.prune_empty_parents(old)

                transaction.on_commit(_drop_old)
        moved += 1
    return moved
