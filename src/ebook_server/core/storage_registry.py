"""Resolve persistent storage locations to configured backend instances."""

from __future__ import annotations

from pathlib import Path

from django.conf import settings

from ebook_server.storage import LocalFilesystemStorage, StorageBackend

from .models import StorageLocation


class StorageConfigurationError(RuntimeError):
    """Raised when a database storage location has no usable backend."""


def backend_for_location(location: StorageLocation) -> StorageBackend:
    """Build the backend for a database location without exposing paths to callers."""
    if location.backend not in {
        StorageLocation.BackendType.LOCAL,
        StorageLocation.BackendType.NFS,
        StorageLocation.BackendType.SAMBA,
    }:
        raise StorageConfigurationError(f"unsupported storage backend: {location.backend}")

    if location.identifier == "default":
        root = Path(settings.APP_DATA_DIR) / "library-artifacts"
    elif location.identifier == "scan":
        root = Path(settings.APP_DATA_DIR) / "scan-artifacts"
    else:
        configured_path = Path(location.base_path).expanduser()
        root = (
            configured_path
            if configured_path.is_absolute()
            else Path(settings.APP_DATA_DIR) / configured_path
        )
    return LocalFilesystemStorage(str(root))
