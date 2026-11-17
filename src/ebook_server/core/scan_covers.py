"""Scanner cover photos (#128, libreflip contract §4.3, §6.4, §6.6, §6.9).

* :class:`CoverManifestSource` reports the active cover versions in ``GET jobs/{id}``
  (``covers``) and checks them in ``capture-complete`` (item keys ``cover:front`` and
  ``cover:back``). It is registered with :mod:`.scan_manifest` at app start.
* :func:`apply_scan_covers_to_book` runs at finish (scan finalization): the job's
  active cover versions become its Book's front/back covers, with source ``scanner``.
  The raw photo stays in scan storage; the library gets a normalized copy, exactly like
  covers extracted from book files.
"""

from __future__ import annotations

import hashlib
from importlib.metadata import version as package_version
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any
from uuid import uuid4

import structlog
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ebook_server.storage import LocalFilesystemStorage

from . import archive_layout, covers, scan_manifest
from .models import Book, CoverImage, ScanCover, ScanJob
from .storage_registry import backend_for_location, default_archive_location

logger = structlog.get_logger(__name__)

_POSITIONS = tuple(ScanCover.Position.values)


def item_key(position: str) -> str:
    return f"cover:{position}"


class CoverManifestSource:
    """Covers (§4.3): one item per position with its active version."""

    name = "covers"

    def _active(self, job: ScanJob) -> list[ScanCover]:
        return list(ScanCover.objects.filter(scan_job=job, is_active=True).order_by("position"))

    def received(self, job: ScanJob) -> dict[str, scan_manifest.ReceivedItem]:
        return {
            item_key(cover.position): scan_manifest.ReceivedItem(
                cover.checksum_sha256, cover.version
            )
            for cover in self._active(job)
        }

    def status(self, job: ScanJob) -> list[dict[str, Any]]:
        by_position = {cover.position: cover for cover in self._active(job)}
        return [
            {
                "position": position,
                "sha256": by_position[position].checksum_sha256,
                "version": by_position[position].version,
            }
            for position in _POSITIONS
            if position in by_position
        ]

    def expected(self, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, list):
            raise scan_manifest.ManifestError("covers", "covers must be a list.")
        expected: dict[str, str] = {}
        for entry in value:
            if not isinstance(entry, dict):
                raise scan_manifest.ManifestError("covers", "Each cover must be an object.")
            position = entry.get("position")
            if position not in _POSITIONS:
                raise scan_manifest.ManifestError("covers", "position must be 'front' or 'back'.")
            key = item_key(position)
            if key in expected:
                raise scan_manifest.ManifestError("covers", f"{position} is listed twice.")
            sha256 = entry.get("sha256")
            if not isinstance(sha256, str) or scan_manifest._SHA256_RE.fullmatch(sha256) is None:
                raise scan_manifest.ManifestError(
                    "covers.sha256", "covers.sha256 must be a lowercase hex SHA-256."
                )
            expected[key] = sha256
        return expected


def register() -> None:
    scan_manifest.register_manifest_source(CoverManifestSource())


# --- finish: covers onto the Book ---------------------------------------------------------


def _scan_storage() -> LocalFilesystemStorage:
    from .scanner_uploads import SCANNING_STORAGE_ROOT

    return LocalFilesystemStorage(str(Path(settings.APP_DATA_DIR) / SCANNING_STORAGE_ROOT))


_BOOK_COVER_FIELD = {
    CoverImage.CoverPosition.FRONT: "front_cover",
    CoverImage.CoverPosition.BACK: "back_cover",
}


def _replaced_record(existing: CoverImage) -> dict[str, Any]:
    """What a replaced cover was, kept on its successor (Sprint020 G)."""
    return {
        "cover_id": existing.pk,
        "source": existing.source,
        "sha256": existing.sha256,
        "storage_location_id": existing.storage_location_id,
        "storage_identifier": existing.storage_identifier,
        "extractor_tool": existing.extractor_tool,
        "scan_cover_id": existing.scan_cover_id,
        "replaced_at": timezone.now().isoformat(),
    }


def _apply_one(book: Book, scan_cover: ScanCover) -> CoverImage | None:
    position = scan_cover.position
    current = CoverImage.objects.filter(book=book, position=position).first()
    if current is not None and current.scan_cover_id == scan_cover.pk:
        return None  # already applied (a repeated or resumed finish)

    # Normalize outside any transaction: reading the raw photo and writing the cover
    # may take a moment on network storage.
    raw_path = _scan_storage().resolve_location(scan_cover.storage_identifier)
    payload = covers.normalized_cover_from_file(raw_path)
    location = default_archive_location()
    storage = backend_for_location(location)
    with NamedTemporaryFile(suffix=".jpg", delete_on_close=False) as output_file:
        output_file.write(payload)
        output_file.close()
        destination = archive_layout.publish_derived(
            storage,
            book,
            output_file.name,
            f"cover-{position}.jpg",
            legacy_namespace=f"books/{book.pk}/covers",
            legacy_filename=f"{uuid4()}-{position}.jpg",
        )
    try:
        with transaction.atomic():
            locked = Book.objects.select_for_update().get(pk=book.pk)
            current = (
                CoverImage.objects.select_for_update()
                .filter(book=locked, position=position)
                .first()
            )
            if current is not None and current.scan_cover_id == scan_cover.pk:
                storage.delete(destination)
                return None
            replaced: dict[str, Any] = {}
            if current is not None:
                # One cover per position: the scanner's photo of this very copy wins.
                # The earlier cover's file stays on storage; its provenance moves here.
                replaced = _replaced_record(current)
                current.delete()
            cover = CoverImage.objects.create(
                book=locked,
                position=position,
                storage_location=location,
                storage_identifier=destination,
                mime_type="image/jpeg",
                size_bytes=len(payload),
                sha256=hashlib.sha256(payload).hexdigest(),
                extractor_tool=covers.SCANNER_EXTRACTOR_TOOL,
                extractor_version=f"Pillow {package_version('Pillow')}",
                source=CoverImage.Source.SCANNER,
                scan_cover=scan_cover,
                replaced=replaced,
            )
            field_name = _BOOK_COVER_FIELD[CoverImage.CoverPosition(position)]
            setattr(locked, field_name, cover)
            locked.save(update_fields=[f"{field_name}_id", "updated_at"])
            return cover
    except Exception:
        storage.delete(destination)
        raise


def apply_scan_covers_to_book(job: ScanJob) -> list[CoverImage]:
    """At finish: the job's active scanner covers become its Book's covers.

    Idempotent: a cover already made from the same scan cover version is kept. A Book
    cover from another source (or an older scan cover version) is replaced, and the new
    cover records what it replaced.
    """
    if job.book_id is None:
        return []
    book = Book.objects.get(pk=job.book_id)
    applied: list[CoverImage] = []
    for scan_cover in ScanCover.objects.filter(scan_job=job, is_active=True).order_by("position"):
        cover = _apply_one(book, scan_cover)
        if cover is not None:
            applied.append(cover)
            logger.info(
                "scan_cover_applied",
                scan_job_id=str(job.pk),
                book_id=book.pk,
                position=cover.position,
                cover_id=cover.pk,
                replaced_cover_id=cover.replaced.get("cover_id"),
            )
    return applied
