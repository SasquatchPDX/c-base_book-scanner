"""Resolve persistent storage locations to configured backend instances."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

from ebook_server.storage import LocalFilesystemStorage, ReadOnlyFilesystemStorage, StorageBackend

from .models import StorageLocation

DEFAULT_ARCHIVE_LOCATION_IDENTIFIER = "default"


class StorageConfigurationError(RuntimeError):
    """Raised when a database storage location has no usable backend."""


def archive_root() -> Path:
    """Writable archive root on the attached storage (originals, conversions, covers, OCR)."""
    dirname = getattr(settings, "BOOK_SCANNER_ARCHIVE_DIRNAME", "library-artifacts")
    return Path(settings.APP_DATA_DIR) / dirname


def default_archive_location() -> StorageLocation:
    """The writable ``default`` archive location record, created on first use."""
    location, _ = StorageLocation.objects.get_or_create(
        identifier=DEFAULT_ARCHIVE_LOCATION_IDENTIFIER,
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": "default",
        },
    )
    return location


def writable_location_for(location: StorageLocation) -> StorageLocation:
    """Where artifacts derived from a file on ``location`` are written.

    Derived artifacts (conversions, covers, OCR output) never go onto a read-only,
    indexed-in-place location; they go to the default archive on the attached storage.
    """
    if location.read_only:
        return default_archive_location()
    return location


def backend_for_location(location: StorageLocation) -> StorageBackend:
    """Build the backend for a database location without exposing paths to callers."""
    return filesystem_backend_for_location(location)


def filesystem_backend_for_location(location: StorageLocation) -> LocalFilesystemStorage:
    """Like ``backend_for_location`` but typed as the filesystem backend."""
    if location.backend not in {
        StorageLocation.BackendType.LOCAL,
        StorageLocation.BackendType.NFS,
        StorageLocation.BackendType.SAMBA,
    }:
        raise StorageConfigurationError(f"unsupported storage backend: {location.backend}")

    if location.identifier == DEFAULT_ARCHIVE_LOCATION_IDENTIFIER:
        root = archive_root()
    elif location.identifier == "scan":
        root = Path(settings.APP_DATA_DIR) / "scan-artifacts"
    else:
        configured_path = Path(location.base_path).expanduser()
        root = (
            configured_path
            if configured_path.is_absolute()
            else Path(settings.APP_DATA_DIR) / configured_path
        )
    if location.read_only:
        return ReadOnlyFilesystemStorage(str(root))
    return LocalFilesystemStorage(str(root))
