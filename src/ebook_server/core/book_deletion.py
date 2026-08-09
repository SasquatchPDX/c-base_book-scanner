"""Integrity-aware deletion workflow for canonical books and managed artifacts."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path

import structlog
from django.db import transaction

from ebook_server.storage import StorageBackend

from .models import Book, StorageLocation
from .storage_registry import StorageConfigurationError, backend_for_location

logger = structlog.get_logger(__name__)


class BookDeletionError(RuntimeError):
    """Raised when deletion cannot be completed without risking inconsistency."""


@dataclass(frozen=True)
class ArtifactToDelete:
    storage_location: StorageLocation
    storage_identifier: str
    expected_sha256: str
    kind: str


@dataclass(frozen=True)
class QuarantinedArtifact:
    backend: StorageBackend
    original_identifier: str
    quarantine_identifier: str


@dataclass(frozen=True)
class BookDeletionReport:
    book_id: int
    artifact_count: int
    missing_artifact_count: int
    cleanup_warnings: tuple[str, ...]


def _book_artifacts(book: Book) -> list[ArtifactToDelete]:
    artifacts = [
        ArtifactToDelete(
            storage_location=variant.storage_location,
            storage_identifier=variant.storage_identifier,
            expected_sha256=variant.sha256,
            kind=f"file variant {variant.id}",
        )
        for variant in book.file_variants.select_related("storage_location").all()
    ]
    artifacts.extend(
        ArtifactToDelete(
            storage_location=cover.storage_location,
            storage_identifier=cover.storage_identifier,
            expected_sha256=cover.sha256,
            kind=f"{cover.position} cover {cover.id}",
        )
        for cover in book.cover_images.select_related("storage_location").all()
    )
    return artifacts


def _quarantine_destination(book_id: int, identifier: str) -> str:
    safe_name = Path(identifier).name or "artifact"
    return f".trash/books/{book_id}/{uuid.uuid4()}-{safe_name}"


def _restore_quarantined(artifacts: list[QuarantinedArtifact]) -> None:
    restoration_errors: list[str] = []
    for artifact in reversed(artifacts):
        try:
            artifact.backend.move(
                artifact.quarantine_identifier,
                artifact.original_identifier,
            )
        except Exception as exc:  # pragma: no cover - catastrophic backend failure
            restoration_errors.append(f"{artifact.original_identifier}: {exc}")
    if restoration_errors:
        raise BookDeletionError(
            "deletion rollback could not restore: " + "; ".join(restoration_errors),
        )


def delete_book_safely(
    book: Book,
    *,
    verify_hashes: bool = True,
) -> BookDeletionReport:
    """Quarantine artifacts, delete the DB graph atomically, then purge quarantine."""
    book_id = book.id
    missing_count = 0
    quarantined: list[QuarantinedArtifact] = []
    try:
        with transaction.atomic():
            locked_book = Book.objects.select_for_update().get(pk=book_id)
            artifacts = _book_artifacts(locked_book)
            resolved: list[tuple[ArtifactToDelete, StorageBackend]] = []
            for artifact in artifacts:
                try:
                    backend = backend_for_location(artifact.storage_location)
                except StorageConfigurationError as exc:
                    raise BookDeletionError(f"{artifact.kind} storage is unavailable") from exc
                if not backend.exists(artifact.storage_identifier):
                    missing_count += 1
                    raise BookDeletionError(
                        f"{artifact.kind} is missing from storage; "
                        "run verify_storage before deleting",
                    )
                if verify_hashes and artifact.expected_sha256:
                    actual_sha256 = backend.hash(artifact.storage_identifier)
                    if actual_sha256.lower() != artifact.expected_sha256.lower():
                        raise BookDeletionError(
                            f"{artifact.kind} hash mismatch; run verify_storage before deleting",
                        )
                resolved.append((artifact, backend))

            for artifact, backend in resolved:
                quarantine_identifier = _quarantine_destination(
                    book_id,
                    artifact.storage_identifier,
                )
                backend.move(artifact.storage_identifier, quarantine_identifier)
                quarantined.append(
                    QuarantinedArtifact(
                        backend=backend,
                        original_identifier=artifact.storage_identifier,
                        quarantine_identifier=quarantine_identifier,
                    ),
                )
            locked_book.delete()
    except Exception as exc:
        _restore_quarantined(quarantined)
        if isinstance(exc, BookDeletionError):
            raise
        raise BookDeletionError(f"book deletion failed: {exc}") from exc

    cleanup_warnings: list[str] = []
    for quarantined_artifact in quarantined:
        try:
            quarantined_artifact.backend.delete(quarantined_artifact.quarantine_identifier)
        except Exception as exc:  # pragma: no cover - backend-specific cleanup failure
            cleanup_warnings.append(f"{quarantined_artifact.quarantine_identifier}: {exc}")
    logger.info(
        "book_deleted",
        book_id=book_id,
        artifact_count=len(quarantined),
        cleanup_warning_count=len(cleanup_warnings),
    )
    return BookDeletionReport(
        book_id=book_id,
        artifact_count=len(quarantined),
        missing_artifact_count=missing_count,
        cleanup_warnings=tuple(cleanup_warnings),
    )
