"""Storage abstraction for application-managed artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
import uuid
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


# Characters that are path separators or invalid on Windows/SMB shares.
_UNSAFE_COMPONENT_CHARS = re.compile(r'[\\/:*?"<>|]')
_WHITESPACE_RUN = re.compile(r"\s+")
_WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{index}" for index in range(1, 10)}
    | {f"LPT{index}" for index in range(1, 10)},
)
# Most filesystems cap one path component at 255 bytes; stay well below it so a
# collision suffix and an extension still fit, and full paths stay short for SMB clients.
MAX_COMPONENT_BYTES = 120


def _truncate_utf8(value: str, max_bytes: int) -> str:
    """Cut ``value`` to at most ``max_bytes`` UTF-8 bytes without splitting a character."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_path_component(
    value: str,
    *,
    fallback: str = "Unknown",
    max_bytes: int = MAX_COMPONENT_BYTES,
) -> str:
    """Turn untrusted text (a title, an author) into one safe path component.

    Unicode is NFC-normalized (so the same title never yields two folders), control and
    format characters are dropped, separators and Windows-invalid characters become
    ``_``, leading/trailing dots and spaces are stripped (no ``..``, no hidden files, no
    names Windows silently truncates), Windows device names get a ``_`` prefix, and the
    result is capped in UTF-8 bytes. An empty result becomes ``fallback``.
    """
    text = _WHITESPACE_RUN.sub(" ", unicodedata.normalize("NFC", value or ""))
    text = "".join(ch for ch in text if unicodedata.category(ch) not in {"Cc", "Cf", "Cs"})
    text = _UNSAFE_COMPONENT_CHARS.sub("_", text)
    text = _WHITESPACE_RUN.sub(" ", text).strip(" .")
    text = _truncate_utf8(text, max_bytes).strip(" .")
    if not text:
        text = fallback
    if text.split(".", 1)[0].strip().upper() in _WINDOWS_RESERVED_NAMES:
        text = f"_{text}"
    return text


def safe_filename(name: str, *, max_bytes: int = MAX_COMPONENT_BYTES) -> str:
    """Sanitize a filename like ``safe_path_component`` while keeping a short extension."""
    base = Path(unicodedata.normalize("NFC", name or "")).name
    suffix = Path(base).suffix
    if not re.fullmatch(r"\.[A-Za-z0-9]{1,10}", suffix):
        suffix = ""
    stem = base[: len(base) - len(suffix)] if suffix else base
    safe_stem = safe_path_component(stem, fallback="file", max_bytes=max_bytes - len(suffix))
    return f"{safe_stem}{suffix.lower()}"


def _validate_namespace(namespace: str) -> str:
    """Reject namespaces that are absolute or climb out of the storage root."""
    normalized = namespace.replace("\\", "/").strip("/")
    parts = normalized.split("/") if normalized else []
    if namespace.startswith(("/", "\\")) or any(part in {"", ".", ".."} for part in parts):
        raise ValueError("unsafe storage namespace")
    return normalized


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


class ReadOnlyStorageError(PermissionError):
    """Raised when a write, move or delete targets a read-only (indexed-in-place) location."""


class InsufficientStorageError(StorageUnavailableError):
    """Raised when writing would push the storage volume below its free-space reserve."""


def free_bytes(path: Path) -> int | None:
    """Free bytes on the volume holding ``path`` (or its nearest existing parent)."""
    candidate = path
    while not candidate.exists():
        if candidate.parent == candidate:
            return None
        candidate = candidate.parent
    try:
        return shutil.disk_usage(candidate).free
    except OSError:
        return None


def min_free_bytes() -> int:
    """Configured free-space reserve (``BOOK_SCANNER_STORAGE_MIN_FREE_BYTES``); 0 disables."""
    try:
        from django.conf import settings as django_settings

        return max(int(getattr(django_settings, "BOOK_SCANNER_STORAGE_MIN_FREE_BYTES", 0) or 0), 0)
    except Exception:  # settings not configured (pure-library use)
        return 0


def ensure_free_space(path: Path, required_bytes: int) -> None:
    """Refuse a write of ``required_bytes`` that would leave less than the reserve free.

    The reserve keeps headroom for PostgreSQL/Redis when they share the volume.
    Unknown free space (unsupported filesystem) is not treated as an error.
    """
    reserve = min_free_bytes()
    if reserve <= 0:
        return
    available = free_bytes(path)
    if available is None:
        return
    if available - max(required_bytes, 0) < reserve:
        raise InsufficientStorageError(
            f"insufficient free space on the storage volume for {path}: "
            f"{available} bytes free, {required_bytes} needed, "
            f"{reserve} reserved (BOOK_SCANNER_STORAGE_MIN_FREE_BYTES)",
        )


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

    def generate_destination(
        self,
        namespace: str,
        original_name: str,
        *,
        unique: bool = False,
    ) -> str:
        """Generate a storage id for a new artifact.

        With ``unique=True`` the readable (sanitized) name is kept and a short random
        token is appended when an artifact already exists there, so a new file never
        overwrites an existing one.
        """

    def stat(self, location: str) -> tuple[int, int]:
        """Return ``(size_bytes, mtime_ns)`` for an artifact."""

    def free_bytes(self) -> int | None:
        """Free bytes available to this backend, or None when unknown."""

    def ensure_free_space(self, required_bytes: int) -> None:
        """Raise InsufficientStorageError if storing ``required_bytes`` breaches the reserve."""


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

    def link(self, source: str, destination: str) -> str:
        """Give an existing artifact a second name (hard link) inside this storage.

        Instant and byte-identical; raises ``OSError`` where hard links are unsupported
        or the destination exists. Never overwrites.
        """
        src = self.resolve_location(source)
        dest = self._prepare_destination(destination)
        os.link(src, dest)
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

    def generate_destination(
        self,
        namespace: str,
        original_name: str,
        *,
        unique: bool = False,
    ) -> str:
        prefix = f"{_validate_namespace(namespace)}/".lstrip("/")
        if not unique:
            safe_name = cap_filename(Path(original_name).name.replace(" ", "_"))
            return f"{prefix}{safe_name}"
        safe_name = safe_filename(original_name)
        candidate = f"{prefix}{safe_name}"
        suffix = Path(safe_name).suffix
        stem = safe_name[: len(safe_name) - len(suffix)] if suffix else safe_name
        while self.resolve_location(candidate).exists():
            candidate = f"{prefix}{stem}-{uuid.uuid4().hex[:8]}{suffix}"
        return candidate

    def stat(self, location: str) -> tuple[int, int]:
        result = self.resolve_location(location).stat()
        return result.st_size, result.st_mtime_ns

    def prune_empty_parents(self, location: str) -> None:
        """Remove now-empty folders above ``location``, up to (not including) the root."""
        base = Path(self.root).resolve()
        current = self.resolve_location(location).parent
        while current != base and base in current.parents:
            try:
                current.rmdir()  # only succeeds when the folder is empty
            except OSError:
                return
            current = current.parent

    def free_bytes(self) -> int | None:
        return free_bytes(Path(self.root))

    def ensure_free_space(self, required_bytes: int) -> None:
        ensure_free_space(Path(self.root), required_bytes)

    def relative_identifier(self, path: str | Path) -> str:
        """Storage id (POSIX-style, relative to the root) for a file already under the root.

        Raises ``ValueError`` when the resolved path lies outside the root, which also
        rejects symlinks that point out of the share.
        """
        base = Path(self.root).resolve()
        target = Path(path).resolve()
        try:
            relative = target.relative_to(base)
        except ValueError as exc:
            raise ValueError("path escapes storage root") from exc
        if not relative.parts:
            raise ValueError("path is the storage root itself")
        return relative.as_posix()


@dataclass(frozen=True)
class ReadOnlyFilesystemStorage(LocalFilesystemStorage):
    """Filesystem backend for files indexed in place: reads only, never writes or deletes.

    Used for collections the application references where they already live (often a
    read-only network share). Every mutating operation fails closed.
    """

    def _refuse(self, operation: str) -> ReadOnlyStorageError:
        return ReadOnlyStorageError(f"storage location is read-only; refusing {operation}")

    def store(self, source: str, destination: str) -> str:
        raise self._refuse("store")

    def copy(self, source: str, destination: str) -> str:
        raise self._refuse("copy")

    def move(self, source: str, destination: str) -> str:
        raise self._refuse("move")

    def link(self, source: str, destination: str) -> str:
        raise self._refuse("link")

    def delete(self, location: str) -> None:
        raise self._refuse("delete")

    def generate_destination(
        self,
        namespace: str,
        original_name: str,
        *,
        unique: bool = False,
    ) -> str:
        raise self._refuse("new artifact")

    def ensure_free_space(self, required_bytes: int) -> None:
        raise self._refuse("write")

    def prune_empty_parents(self, location: str) -> None:
        raise self._refuse("folder removal")
