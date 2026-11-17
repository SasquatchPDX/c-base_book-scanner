"""Index-in-place support: FileVariants that reference a file where it already lives.

A source configured with ``TransferMode.INDEX_IN_PLACE`` is never copied into the
archive. The FileVariant points at the file through a read-only ``StorageLocation``
(``storage_identifier`` relative to its base path) and records size, mtime and SHA-256.
Because the application does not own those bytes, a consistency check compares them
again later; missing or changed files are flagged (``FileVariant.reference_state``) and
are no longer served, instead of silently serving different bytes under the old record.
Nothing on a read-only location is ever written, moved or deleted.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from pathlib import Path, PurePath
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone

from ebook_server.jobs import get_queue, is_stale_running
from ebook_server.storage import LocalFilesystemStorage, StorageUnavailableError, check_storage_root

from .models import FileVariant, ImportJob, ProcessingJob, SourceLocation, StorageLocation
from .storage_registry import StorageConfigurationError, filesystem_backend_for_location

logger = structlog.get_logger(__name__)

STORAGE_IDENTIFIER_MAX_LENGTH = FileVariant._meta.get_field("storage_identifier").max_length or 512


class ReferencedFileError(RuntimeError):
    """A referenced (indexed-in-place) file cannot be used safely."""


def is_index_in_place(source_location: SourceLocation | None) -> bool:
    return (
        source_location is not None
        and source_location.transfer_mode == SourceLocation.TransferMode.INDEX_IN_PLACE
    )


def index_storage_location(source_location: SourceLocation) -> StorageLocation:
    """The read-only location an index-in-place source is referenced through.

    Created from the source root on first use. Once created it is never repointed: the
    stored relative identifiers depend on its base path, so changing the source's
    ``root_path`` later does not break variants that were already indexed.
    """
    location = source_location.index_storage_location
    if location is not None:
        if not location.read_only:
            raise ReferencedFileError(
                f"storage location {location.identifier} must be read-only for index in place",
            )
        return location
    root = Path(source_location.root_path).expanduser().resolve()
    backend = (
        StorageLocation.BackendType.NFS
        if source_location.default_source_type == SourceLocation.SourceType.NETWORK_SOURCE
        else StorageLocation.BackendType.LOCAL
    )
    with transaction.atomic():
        locked = SourceLocation.objects.select_for_update().get(pk=source_location.pk)
        if locked.index_storage_location is not None:
            location = locked.index_storage_location
        else:
            location, _ = StorageLocation.objects.get_or_create(
                identifier=f"source-{source_location.pk}",
                defaults={"backend": backend, "base_path": str(root), "read_only": True},
            )
            if not location.read_only:
                raise ReferencedFileError(
                    f"storage location {location.identifier} exists but is not read-only",
                )
            locked.index_storage_location = location
            locked.save(update_fields=["index_storage_location", "updated_at"])
    source_location.index_storage_location = location
    return location


def index_source_file(job: ImportJob) -> bool:
    """Reference ``job.source_path`` in place instead of archiving a copy.

    Sets ``job.storage_location``/``storage_identifier`` and refreshes the recorded
    size/mtime. Returns True only when the file, read back through the read-only
    backend, still has the SHA-256 computed during hashing and did not change while it
    was being verified.
    """
    if job.source_location is None:
        raise ReferencedFileError("index in place requires a source location")
    location = index_storage_location(job.source_location)
    backend = filesystem_backend_for_location(location)
    try:
        identifier = backend.relative_identifier(job.source_path)
    except ValueError as exc:
        raise ReferencedFileError(
            f"source file is outside the indexed storage location {location.identifier}",
        ) from exc
    if len(identifier) > STORAGE_IDENTIFIER_MAX_LENGTH:
        raise ReferencedFileError("relative path is too long to index in place")
    existing = (
        FileVariant.objects.filter(storage_location=location, storage_identifier=identifier)
        .exclude(sha256=job.sha256)
        .first()
    )
    if existing is not None:
        # Same path, different bytes: the indexed file was replaced. Never repoint the
        # old record silently; the consistency check flags it for an admin.
        raise ReferencedFileError(
            f"path is already indexed as file variant {existing.pk} with different content; "
            "run check_referenced_files and resolve the changed variant first",
        )

    size_before, mtime_before = backend.stat(identifier)
    verified = backend.hash(identifier) == job.sha256
    size_after, mtime_after = backend.stat(identifier)
    if (size_before, mtime_before) != (size_after, mtime_after):
        verified = False

    job.storage_location = location
    job.storage_identifier = identifier
    job.source_size = size_after
    job.source_mtime_ns = mtime_after
    job.save(
        update_fields=[
            "storage_location_id",
            "storage_identifier",
            "source_size",
            "source_mtime_ns",
            "updated_at",
        ],
    )
    return verified


def reference_fields_for_job(job: ImportJob) -> dict[str, object]:
    """Extra FileVariant fields for a variant created from ``job``."""
    location = job.storage_location
    if location is None or not location.read_only:
        return {}
    return {
        "source_mtime_ns": job.source_mtime_ns,
        "reference_state": FileVariant.ReferenceState.OK,
        "reference_checked_at": timezone.now(),
    }


def is_referenced(variant: FileVariant) -> bool:
    return bool(variant.storage_location.read_only)


def ensure_reference_intact(variant: FileVariant) -> None:
    """Refuse to use a referenced file that is flagged or visibly changed.

    A cheap stat comparison (size + mtime) runs before every download/conversion/OCR
    read; a mismatch flags the variant and raises. Archived variants pass unchecked.
    """
    if not is_referenced(variant):
        return
    if variant.reference_state != FileVariant.ReferenceState.OK:
        raise ReferencedFileError(f"referenced file is {variant.reference_state}")
    try:
        backend = filesystem_backend_for_location(variant.storage_location)
        size, mtime_ns = backend.stat(variant.storage_identifier)
    except FileNotFoundError as exc:
        # A missing file on an unmounted share is an outage, not drift.
        if location_unavailable_reason(variant.storage_location, backend) is not None:
            raise ReferencedFileError("referenced storage is unavailable") from exc
        _flag(variant, FileVariant.ReferenceState.MISSING)
        raise ReferencedFileError("referenced file is missing") from exc
    except (OSError, ValueError, StorageConfigurationError) as exc:
        # An unmounted share is an outage, not drift: do not flag, just refuse.
        raise ReferencedFileError("referenced storage is unavailable") from exc
    if size != variant.size_bytes or (
        variant.source_mtime_ns is not None and mtime_ns != variant.source_mtime_ns
    ):
        _flag(variant, FileVariant.ReferenceState.CHANGED)
        raise ReferencedFileError("referenced file changed on storage")


def location_unavailable_reason(
    location: StorageLocation,
    backend: LocalFilesystemStorage,
) -> str | None:
    """Why a read-only location does not count as mounted, or None when it is ready.

    An unmounted mount point is usually still an (empty) directory, so ``is_dir()`` is
    not enough. With ``sentinel_file`` set, that file must exist under the base path
    (the same rule ``check_storage_root`` applies to the attached storage root).
    Without one, an empty base directory counts as unmounted.
    """
    root = Path(backend.root)
    sentinel = (location.sentinel_file or "").strip()
    if sentinel:
        pure = PurePath(sentinel)
        if pure.is_absolute() or ".." in pure.parts or ":" in sentinel:
            return f"storage location {location.identifier} has an invalid sentinel_file"
    try:
        check_storage_root(root=root, require_existing=True, sentinel=sentinel)
        if not sentinel:
            with os.scandir(root) as entries:
                if next(entries, None) is None:
                    return (
                        f"storage location {location.identifier} base directory {root} is "
                        "empty; is the share mounted? (configure a sentinel_file)"
                    )
    except StorageUnavailableError as exc:
        return f"storage location {location.identifier}: {exc}"
    except OSError as exc:
        return f"storage location {location.identifier} is not readable: {exc}"
    return None


def _flag(variant: FileVariant, state: str) -> None:
    variant.reference_state = state
    variant.reference_checked_at = timezone.now()
    variant.save(update_fields=["reference_state", "reference_checked_at", "updated_at"])
    logger.warning(
        "referenced_file_drift",
        file_variant_id=variant.pk,
        book_id=variant.book_id,
        storage_location=variant.storage_location.identifier,
        reference_state=state,
    )


@dataclass
class ReferenceCheckReport:
    checked: int = 0
    ok: int = 0
    missing: list[int] = field(default_factory=list)
    changed: list[int] = field(default_factory=list)
    restored: list[int] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    unavailable_locations: list[str] = field(default_factory=list)

    @property
    def has_findings(self) -> bool:
        return bool(self.missing or self.changed or self.errors)

    def summary(self) -> str:
        return (
            f"checked={self.checked} ok={self.ok} missing={len(self.missing)} "
            f"changed={len(self.changed)} restored={len(self.restored)} "
            f"errors={len(self.errors)} unavailable_locations={len(self.unavailable_locations)}"
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _check_one(
    variant: FileVariant,
    backend: LocalFilesystemStorage,
    *,
    verify_hashes: bool,
) -> str:
    """Current reference state of one variant; may refresh a touched-but-equal mtime."""
    try:
        size, mtime_ns = backend.stat(variant.storage_identifier)
    except FileNotFoundError:
        return FileVariant.ReferenceState.MISSING
    stat_matches = size == variant.size_bytes and (
        variant.source_mtime_ns is None or mtime_ns == variant.source_mtime_ns
    )
    if stat_matches and not verify_hashes:
        return FileVariant.ReferenceState.OK
    if size != variant.size_bytes:
        return FileVariant.ReferenceState.CHANGED
    if backend.hash(variant.storage_identifier) != variant.sha256:
        return FileVariant.ReferenceState.CHANGED
    if variant.source_mtime_ns != mtime_ns:
        # Same bytes, new timestamp (e.g. touched or re-copied): keep serving it.
        variant.source_mtime_ns = mtime_ns
        variant.save(update_fields=["source_mtime_ns", "updated_at"])
    return FileVariant.ReferenceState.OK


def referenced_variants(
    *,
    variant_ids: Iterable[int] | None = None,
    storage_location_ids: Iterable[int] | None = None,
) -> Iterable[FileVariant]:
    queryset = FileVariant.objects.select_related("storage_location").filter(
        storage_location__read_only=True,
    )
    if variant_ids is not None:
        queryset = queryset.filter(pk__in=list(variant_ids))
    if storage_location_ids is not None:
        queryset = queryset.filter(storage_location_id__in=list(storage_location_ids))
    return queryset.order_by("pk").iterator()


def check_referenced_files(
    *,
    verify_hashes: bool = False,
    variant_ids: Iterable[int] | None = None,
    storage_location_ids: Iterable[int] | None = None,
) -> ReferenceCheckReport:
    """Compare indexed-in-place variants with the files on storage.

    Checks every referenced variant, or only ``variant_ids`` / those on
    ``storage_location_ids`` when given. Size/mtime mismatches are re-hashed, so a
    touched but identical file stays OK; ``verify_hashes`` re-hashes every file (I/O
    heavy). This hashes, so call it from the CLI or a worker job
    (``request_reference_check``), never inside a web request. A variant whose file is
    back with the recorded content returns to OK.

    A location that is not ready (see ``location_unavailable_reason``: missing base
    directory, missing sentinel, or an empty bare mount point) is an outage: its
    variants are reported as errors and none is flagged, so an unmounted share cannot
    mass-flag a library.
    """
    report = ReferenceCheckReport()
    backends: dict[int, LocalFilesystemStorage] = {}
    unavailable: dict[int, str] = {}
    for variant in referenced_variants(
        variant_ids=variant_ids,
        storage_location_ids=storage_location_ids,
    ):
        report.checked += 1
        location_id = variant.storage_location_id
        if location_id in unavailable:
            report.errors.append(f"file_variant:{variant.pk}: {unavailable[location_id]}")
            continue
        try:
            backend = backends.get(location_id)
            if backend is None:
                backend = filesystem_backend_for_location(variant.storage_location)
                reason = location_unavailable_reason(variant.storage_location, backend)
                if reason is not None:
                    unavailable[location_id] = reason
                    report.unavailable_locations.append(variant.storage_location.identifier)
                    logger.error(
                        "referenced_storage_unavailable",
                        storage_location=variant.storage_location.identifier,
                        reason=reason,
                    )
                    report.errors.append(f"file_variant:{variant.pk}: {reason}")
                    continue
                backends[location_id] = backend
            state = _check_one(variant, backend, verify_hashes=verify_hashes)
        except (OSError, ValueError, StorageConfigurationError) as exc:
            report.errors.append(f"file_variant:{variant.pk}: {exc}")
            continue
        previous = variant.reference_state
        if state == FileVariant.ReferenceState.OK:
            report.ok += 1
            if previous != FileVariant.ReferenceState.OK:
                report.restored.append(variant.pk)
        elif state == FileVariant.ReferenceState.MISSING:
            report.missing.append(variant.pk)
        else:
            report.changed.append(variant.pk)
        if state != previous:
            if state == FileVariant.ReferenceState.OK:
                variant.reference_state = state
                variant.reference_checked_at = timezone.now()
                variant.save(
                    update_fields=["reference_state", "reference_checked_at", "updated_at"],
                )
                logger.info("referenced_file_restored", file_variant_id=variant.pk)
            else:
                _flag(variant, state)
        else:
            variant.reference_checked_at = timezone.now()
            variant.save(update_fields=["reference_checked_at"])
    return report


# Variant ids stored in a job's details are capped so a huge check stays a small row.
REPORT_ID_LIMIT = 200


class ReferenceCheckDispatchError(RuntimeError):
    """The reference-check job was recorded but could not be handed to the queue."""


def _details(job: ProcessingJob) -> dict[str, Any]:
    return dict(job.details) if isinstance(job.details, dict) else {}


def request_reference_check(
    *,
    variant_ids: Iterable[int] | None = None,
    storage_location_ids: Iterable[int] | None = None,
    verify_hashes: bool = False,
    requested_by_user_id: int | None = None,
) -> tuple[ProcessingJob, bool]:
    """Record a persistent reference-check job and hand it to the worker queue.

    Returns ``(job, dispatched)``. ``dispatched`` is False when no queue is configured:
    the job then stays QUEUED for ``check_referenced_files --run-queued``. The check
    itself never runs in the caller (it hashes; see AGENTS.md "Asynchronous Work").
    """
    scope_variants = sorted({int(pk) for pk in variant_ids}) if variant_ids is not None else None
    scope_locations = (
        sorted({int(pk) for pk in storage_location_ids})
        if storage_location_ids is not None
        else None
    )
    processing_job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.REFERENCE_CHECK,
        details={
            "variant_ids": scope_variants,
            "storage_location_ids": scope_locations,
            "verify_hashes": verify_hashes,
            "requested_by_user_id": requested_by_user_id,
            "attempt_count": 0,
            "error": "",
        },
    )
    queue = get_queue()
    if queue is None:
        logger.warning(
            "reference_check_not_dispatched",
            processing_job_id=str(processing_job.job_id),
            reason="no queue configured",
        )
        return processing_job, False
    try:
        queue.enqueue(process_reference_check_job, str(processing_job.job_id))
    except Exception as exc:
        processing_job.state = ProcessingJob.JobState.FAILED
        processing_job.finished_at = timezone.now()
        processing_job.details = {
            **_details(processing_job),
            "error": f"queue dispatch failed: {exc}",
        }
        processing_job.save(update_fields=["state", "finished_at", "details"])
        raise ReferenceCheckDispatchError(str(exc)) from exc
    processing_job.refresh_from_db()
    return processing_job, True


def process_reference_check_job(job_id: str) -> None:
    """Worker entry point: run one recorded reference check and store its report."""
    with transaction.atomic():
        try:
            processing_job = ProcessingJob.objects.select_for_update().get(job_id=job_id)
        except ProcessingJob.DoesNotExist:
            return
        if processing_job.job_type != ProcessingJob.JobType.REFERENCE_CHECK:
            return
        if processing_job.state in {
            ProcessingJob.JobState.SUCCESS,
            ProcessingJob.JobState.FAILED,
        }:
            return
        if processing_job.state == ProcessingJob.JobState.RUNNING and not is_stale_running(
            processing_job,
        ):
            return
        details = _details(processing_job)
        details["attempt_count"] = int(details.get("attempt_count", 0)) + 1
        details["error"] = ""
        processing_job.state = ProcessingJob.JobState.RUNNING
        processing_job.started_at = timezone.now()
        processing_job.finished_at = None
        processing_job.details = details
        processing_job.save(update_fields=["state", "started_at", "finished_at", "details"])

    try:
        report = check_referenced_files(
            verify_hashes=bool(details.get("verify_hashes")),
            variant_ids=details.get("variant_ids"),
            storage_location_ids=details.get("storage_location_ids"),
        )
    except Exception as exc:
        details["error"] = str(exc)
        processing_job.state = ProcessingJob.JobState.FAILED
        processing_job.finished_at = timezone.now()
        processing_job.details = details
        processing_job.save(update_fields=["state", "finished_at", "details"])
        logger.error("reference_check_failed", processing_job_id=job_id, error=str(exc))
        raise

    result = report.as_dict()
    for key in ("missing", "changed", "restored", "errors"):
        result[f"{key}_count"] = len(result[key])
        result[key] = result[key][:REPORT_ID_LIMIT]
    details["report"] = result
    details["summary"] = report.summary()
    # Drift is a successful check that found something. An outage or an unreadable
    # file means the check could not do its job, so the job fails (and is counted by
    # ebook_server_processing_jobs{job_type="reference_check",state="failed"}).
    if report.errors:
        details["error"] = (
            "storage unavailable: " + ", ".join(report.unavailable_locations)
            if report.unavailable_locations
            else f"{len(report.errors)} variant(s) could not be checked"
        )
        processing_job.state = ProcessingJob.JobState.FAILED
    else:
        processing_job.state = ProcessingJob.JobState.SUCCESS
    processing_job.finished_at = timezone.now()
    processing_job.details = details
    processing_job.save(update_fields=["state", "finished_at", "details"])
    logger.info(
        "reference_check_finished",
        processing_job_id=job_id,
        state=processing_job.state,
        checked=report.checked,
        missing=len(report.missing),
        changed=len(report.changed),
        errors=len(report.errors),
    )


def queued_reference_check_ids() -> list[str]:
    return [
        str(job_id)
        for job_id in ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.REFERENCE_CHECK,
            state=ProcessingJob.JobState.QUEUED,
        )
        .order_by("scheduled_at")
        .values_list("job_id", flat=True)
    ]
