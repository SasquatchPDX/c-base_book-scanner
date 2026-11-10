"""Storage abstraction for application-managed artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol

MAX_STORAGE_FILENAME_LENGTH = 150


class StorageUnavailableError(OSError):
    """Raised when the configured storage root is missing, unmounted, or not writable."""


def cap_filename(name: str, limit: int = MAX_STORAGE_FILENAME_LENGTH) -> str:
    """Shorten a filename to ``limit`` characters while keeping its extension."""
    if len(name) <= limit:
        return name
    suffix = Path(name).suffix
    if len(suffix) > 16:
        suffix = ""
    stem = name[: len(name) - len(suffix)] if suffix else name
    return stem[: max(limit - len(suffix), 1)] + suffix


def _storage_policy() -> tuple[Path | None, bool, str]:
    """Return (configured root, require_existing, sentinel) from Django settings."""
    try:
        from django.conf import settings as django_settings

        require = bool(getattr(django_settings, "STORAGE_REQUIRE_EXISTING_ROOT", False))
        sentinel = str(getattr(django_settings, "STORAGE_SENTINEL_FILE", "") or "")
        app_root = getattr(django_settings, "APP_DATA_DIR", None)
    except Exception:  # settings not configured (pure-library use)
        return None, False, ""
    return (Path(app_root) if app_root else None), require, sentinel


def check_storage_root(
    *,
    root: Path | None = None,
    require_existing: bool | None = None,
    sentinel: str | None = None,
) -> Path:
    """Fail clearly when the configured storage root is missing (unmounted share).

    Without this check a missing NFS/Samba mount silently turns into a directory tree
    created on the local root disk.
    """
    configured_root, configured_require, configured_sentinel = _storage_policy()
    target = root if root is not None else configured_root
    if target is None:
        raise StorageUnavailableError("storage root is not configured")
    must_exist = configured_require if require_existing is None else require_existing
    sentinel_name = configured_sentinel if sentinel is None else sentinel
    if must_exist and not target.is_dir():
        raise StorageUnavailableError(
            f"storage root {target} does not exist; is the network share mounted?",
        )
    if sentinel_name and not (target / sentinel_name).exists():
        raise StorageUnavailableError(
            f"storage sentinel {sentinel_name!r} not found under {target}; "
            "is the correct share mounted?",
        )
    return target


def probe_storage_writable(root: Path) -> None:
    """Write and delete a tiny temp file under ``root``."""
    import tempfile

    try:
        with tempfile.NamedTemporaryFile(dir=root, prefix=".ebook-server-probe-") as probe:
            probe.write(b"ok")
            probe.flush()
            os.fsync(probe.fileno())
    except OSError as exc:
        raise StorageUnavailableError(f"storage root {root} is not writable: {exc}") from exc


def ensure_path_storage_ready(path: Path) -> None:
    """Guard writes beneath the configured storage root (no-op for other locations)."""
    app_root, require, sentinel = _storage_policy()
    if app_root is None or not (require or sentinel):
        return
    resolved_root = app_root.resolve()
    resolved_path = path.resolve()
    if resolved_path != resolved_root and resolved_root not in resolved_path.parents:
        return
    check_storage_root(root=app_root, require_existing=require, sentinel=sentinel)


class StorageBackend(Protocol):
    """Abstract storage operations for archive/artifact access."""

    def store(self, source: str, destination: str) -> str:
        """Copy a source path into storage and return canonical storage id."""

    def copy(self, source: str, destination: str) -> str:
        """Copy an existing storage artifact."""

    def move(self, source: str, destination: str) -> str:
        """Move an existing storage artifact."""

    def delete(self, location: str) -> None:
        """Delete an artifact by storage id."""

    def exists(self, location: str) -> bool:
        """Check artifact existence."""

    def hash(self, location: str) -> str:
        """Return SHA-256 hex digest for an artifact."""

    def open(self, location: str) -> BinaryIO:
        """Open artifact for binary read."""

    def stream(self, location: str, chunk_size: int = 8192) -> Iterator[bytes]:
        """Yield file bytes in chunks."""

    def generate_destination(self, namespace: str, original_name: str) -> str:
        """Generate a stable storage id for a new artifact."""


@dataclass(frozen=True)
class LocalFilesystemStorage:
    """Simple local storage backend used for tests and initial environment."""

    root: str

    def _resolve_source(self, source: str) -> Path:
        """Resolve source paths that may point at storage or be absolute."""
        candidate = Path(source)
        if candidate.is_absolute():
            return candidate
        return self.resolve_location(source)

    def resolve_location(self, location: str) -> Path:
        """Resolve and validate a logical storage id to an absolute filesystem path."""
        base = Path(self.root).resolve()
        target = (base / location).resolve()
        base_string = str(base)
        target_string = str(target)
        if not (target_string == base_string or target_string.startswith(base_string + os.sep)):
            raise ValueError("path escapes storage root")
        return target

    def _prepare_destination(self, destination: str) -> Path:
        dest = self.resolve_location(destination)
        ensure_path_storage_ready(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        return dest

    @staticmethod
    def _copy_file(src: Path, dest: Path) -> None:
        # copyfile avoids copystat (chmod/xattr), which commonly fails on CIFS/NFS.
        shutil.copyfile(src, dest)
        try:
            shutil.copymode(src, dest)
        except OSError:
            pass

    def store(self, source: str, destination: str) -> str:
        destination_path = self._prepare_destination(destination)
        source_path = self._resolve_source(source)
        self._copy_file(source_path, destination_path)
        return destination

    def copy(self, source: str, destination: str) -> str:
        src = self._resolve_source(source)
        dest = self._prepare_destination(destination)
        self._copy_file(src, dest)
        return destination

    def move(self, source: str, destination: str) -> str:
        src = self._resolve_source(source)
        dest = self._prepare_destination(destination)
        shutil.move(src, dest, copy_function=shutil.copyfile)
        return destination

    def delete(self, location: str) -> None:
        target = self.resolve_location(location)
        target.unlink(missing_ok=True)

    def exists(self, location: str) -> bool:
        return self.resolve_location(location).exists()

    def hash(self, location: str) -> str:
        target = self.resolve_location(location)
        digest = hashlib.sha256()
        with target.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def open(self, location: str) -> BinaryIO:
        return self.resolve_location(location).open("rb")

    def stream(self, location: str, chunk_size: int = 8192) -> Iterator[bytes]:
        target = self.resolve_location(location)
        with target.open("rb") as handle:
            yield from iter(lambda: handle.read(chunk_size), b"")

    def generate_destination(self, namespace: str, original_name: str) -> str:
        safe_name = cap_filename(Path(original_name).name.replace(" ", "_"))
        return f"{namespace}/{safe_name}"
