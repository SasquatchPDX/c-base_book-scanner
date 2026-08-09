"""Storage abstraction for application-managed artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Protocol


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

    def store(self, source: str, destination: str) -> str:
        destination_path = self.resolve_location(destination)
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        source_path = self._resolve_source(source)
        shutil.copy2(source_path, destination_path)
        return destination

    def copy(self, source: str, destination: str) -> str:
        src = self._resolve_source(source)
        dest = self.resolve_location(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        return destination

    def move(self, source: str, destination: str) -> str:
        src = self._resolve_source(source)
        dest = self.resolve_location(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(src, dest)
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
        safe_name = Path(original_name).name.replace(" ", "_")
        return f"{namespace}/{safe_name}"
