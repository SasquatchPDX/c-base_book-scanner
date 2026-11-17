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

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import structlog
from django.db import transaction
from django.utils import timezone

from ebook_server.storage import LocalFilesystemStorage

from .models import FileVariant, ImportJob, SourceLocation, StorageLocation
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


def referenced_variants() -> Iterable[FileVariant]:
    return (
        FileVariant.objects.select_related("storage_location")
        .filter(storage_location__read_only=True)
        .order_by("pk")
        .iterator()
    )


def check_referenced_files(*, verify_hashes: bool = False) -> ReferenceCheckReport:
    """Compare every indexed-in-place variant with the file on storage.

    Size/mtime mismatches are re-hashed, so a touched but identical file stays OK.
    ``verify_hashes`` re-hashes every file (I/O heavy). A variant whose file is back
    with the recorded content returns to OK. Unavailable storage is reported as an
    error and does not flag variants, so an unmounted share cannot mass-flag a library.
    """
    report = ReferenceCheckReport()
    backends: dict[int, LocalFilesystemStorage] = {}
    for variant in referenced_variants():
        report.checked += 1
        try:
            backend = backends.get(variant.storage_location_id)
            if backend is None:
                backend = filesystem_backend_for_location(variant.storage_location)
                if not Path(backend.root).is_dir():
                    raise StorageConfigurationError(
                        f"storage location {variant.storage_location.identifier} is not mounted",
                    )
                backends[variant.storage_location_id] = backend
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
