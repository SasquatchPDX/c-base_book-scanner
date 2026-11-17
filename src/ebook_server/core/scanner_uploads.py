"""Device page and cover uploads (#128, libreflip contract §4.2, §4.3, §6.5, §6.6).

``PUT jobs/{id}/pages/{n}`` and ``PUT jobs/{id}/covers/{front|back}`` carry a small
``metadata`` JSON part and an ``image`` part. The image is never held in memory:

1. :func:`receive_upload` walks the multipart body with Django's streaming multipart
   parser and writes the ``image`` part chunk by chunk to a temporary file in the scan
   staging folder, hashing it on the way. ``request.body`` is never read.
2. The caller checks the declared SHA-256 and that the image decodes.
3. :func:`publish_staged` gives the staged file its final, exclusively allocated name in
   scan storage (``publish_unique``: a hard link plus removal of the staged name, never
   overwriting anything) and re-hashes the published copy.
4. Only then, under the job's row lock, is the version row created and made active.

Any failure before step 4 -- a dropped connection, a size or checksum error, a crash --
leaves no version row; the ``finally`` blocks remove the staged and published files.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any, Final

import structlog
from django.conf import settings
from django.core.exceptions import SuspiciousOperation
from django.db import transaction
from django.http import HttpRequest
from django.http.multipartparser import (
    FIELD,
    FILE,
    ChunkIter,
    LazyStream,
    MultiPartParser,
    MultiPartParserError,
    Parser,
)
from django.utils.dateparse import parse_datetime
from django.utils.http import parse_header_parameters

from ebook_server.storage import (
    InsufficientStorageError,
    LocalFilesystemStorage,
    StorageUnavailableError,
    ensure_free_space,
    ensure_path_storage_ready,
)

from . import scan_events, scan_pipeline, scantailor
from .models import (
    ScanCover,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    ScanUploadSource,
    StorageLocation,
)

logger = structlog.get_logger(__name__)

# The same scan storage and staging folder as the session-based scan API (views.py).
SCANNING_STORAGE_ROOT: Final = "scan-artifacts"
SCANNING_NAMESPACE: Final = "scan-jobs"
SCAN_STORAGE_LOCATION_IDENTIFIER: Final = "scan"
STAGING_DIRNAME: Final = "uploads"

READ_CHUNK_BYTES: Final = 64 * 1024
MAX_METADATA_BYTES: Final = 64 * 1024
# Part headers, boundaries and the metadata part on top of the image itself.
MULTIPART_OVERHEAD_BYTES: Final = 1024 * 1024
MAX_SEQUENCE_NUMBER: Final = 99_999
MAX_RECOGNIZED_PAGE_NUMBER_LENGTH: Final = 64
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
_STATUS_RE: Final = re.compile(r"[A-Za-z0-9_.-]{1,32}")

_IMAGE_FORMATS: Final[dict[str, tuple[str, str]]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "TIFF": ("image/tiff", ".tif"),
    "WEBP": ("image/webp", ".webp"),
}

# Job states that accept new pages (§6.5) and covers (§6.6).
PAGE_UPLOAD_STATES: Final = frozenset({ScanJob.State.OPEN, ScanJob.State.CAPTURING})
COVER_UPLOAD_STATES: Final = PAGE_UPLOAD_STATES | {ScanJob.State.CAPTURED}

CAMERA_ROLES: Final = ("left", "right")


class UploadError(Exception):
    """An upload is refused; rendered as the API's error envelope."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.details = details or {}


class _ClientGone(Exception):
    """Reading the request body failed: the client disconnected mid-upload."""


# --- storage ------------------------------------------------------------------------------


def scan_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(str(Path(settings.APP_DATA_DIR) / SCANNING_STORAGE_ROOT))


def scan_storage_location() -> StorageLocation:
    location, _ = StorageLocation.objects.get_or_create(
        identifier=SCAN_STORAGE_LOCATION_IDENTIFIER,
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": SCANNING_STORAGE_ROOT,
        },
    )
    return location


def staging_root() -> Path:
    """Staging folder on the scan storage volume, so publishing is a hard link."""
    return Path(settings.APP_DATA_DIR) / SCANNING_STORAGE_ROOT / STAGING_DIRNAME


def max_image_bytes() -> int:
    return int(settings.BOOK_SCANNER_SCANNER_MAX_IMAGE_BYTES)


# --- streaming multipart ------------------------------------------------------------------


class _CountingReader:
    """File-like view of the request stream that counts bytes and spots disconnects."""

    def __init__(self, request: HttpRequest, limit: int) -> None:
        self._request = request
        self._limit = limit
        self.total = 0

    def read(self, size: int = -1) -> bytes:
        try:
            data = self._request.read(size)
        except OSError as exc:  # UnreadablePostError: the connection was dropped
            raise _ClientGone(str(exc)) from exc
        self.total += len(data)
        if self.total > self._limit:
            raise UploadError(413, "payload_too_large", "The request body is too large.")
        return data


@dataclass
class ReceivedUpload:
    """The parsed request: metadata JSON and the staged image file."""

    metadata: dict[str, Any] = field(default_factory=dict)
    image_path: Path | None = None
    image_size: int = 0
    image_sha256: str = ""
    image_filename: str = ""

    def discard(self) -> None:
        """Remove the staged image (a published copy is no longer this file)."""
        if self.image_path is not None:
            self.image_path.unlink(missing_ok=True)
            self.image_path = None


def _disposition_param(meta_data: dict[str, Any], name: str) -> str:
    """A ``Content-Disposition`` parameter of a part (the parser yields bytes)."""
    try:
        value = meta_data["content-disposition"][1].get(name)
    except (KeyError, IndexError, TypeError, AttributeError):
        return ""
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    return str(value or "").strip()


def _drain(stream: LazyStream) -> None:
    for _chunk in stream:
        pass


def _stage_image(stream: LazyStream, upload: ReceivedUpload, limit: int) -> None:
    root = staging_root()
    ensure_path_storage_ready(root)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    size = 0
    with NamedTemporaryFile(delete=False, dir=root, prefix="scanner-", suffix=".part") as handle:
        upload.image_path = Path(handle.name)
        for chunk in stream:
            size += len(chunk)
            if size > limit:
                raise UploadError(
                    413,
                    "payload_too_large",
                    "The image exceeds the size limit.",
                    {"max_image_bytes": limit},
                )
            digest.update(chunk)
            handle.write(chunk)
        handle.flush()
        os.fsync(handle.fileno())
    upload.image_size = size
    upload.image_sha256 = digest.hexdigest()


def _parse_metadata(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_METADATA_BYTES:
        raise UploadError(413, "payload_too_large", "The metadata part is too large.")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise UploadError(400, "invalid_json", "The metadata part must be a JSON object.") from exc
    if not isinstance(value, dict):
        raise UploadError(400, "invalid_json", "The metadata part must be a JSON object.")
    return value


def receive_upload(request: HttpRequest) -> ReceivedUpload:
    """Stream a ``metadata`` + ``image`` multipart body; the image goes to a staged file.

    The image part may be sent with or without a filename. Unknown parts are skipped.
    On any error the staged file is removed before the exception propagates.
    """
    content_type, options = parse_header_parameters(request.META.get("CONTENT_TYPE", ""))
    if content_type != "multipart/form-data":
        raise UploadError(415, "unsupported_media_type", "Send multipart/form-data.")
    boundary = options.get("boundary", "")
    if not boundary or not MultiPartParser.boundary_re.fullmatch(boundary):
        raise UploadError(400, "invalid_multipart", "The multipart boundary is missing.")
    image_limit = max_image_bytes()
    body_limit = image_limit + MULTIPART_OVERHEAD_BYTES
    try:
        declared_length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared_length = 0
    if declared_length > body_limit:
        raise UploadError(
            413,
            "payload_too_large",
            "The request body is too large.",
            {"max_image_bytes": image_limit},
        )
    upload = ReceivedUpload()
    reader = _CountingReader(request, body_limit)
    metadata_seen = False
    try:
        ensure_free_space(staging_root(), declared_length or image_limit)
        stream = LazyStream(ChunkIter(reader, READ_CHUNK_BYTES))  # type: ignore[arg-type]
        for item_type, meta_data, part in Parser(stream, boundary.encode("ascii")):
            name = _disposition_param(meta_data, "name") if item_type in (FIELD, FILE) else ""
            if name == "image":
                if upload.image_path is not None:
                    raise UploadError(400, "invalid_multipart", "Send one image part only.")
                upload.image_filename = _disposition_param(meta_data, "filename")
                _stage_image(part, upload, image_limit)
            elif name == "metadata":
                if metadata_seen:
                    raise UploadError(400, "invalid_multipart", "Send one metadata part only.")
                metadata_seen = True
                upload.metadata = _parse_metadata(part.read(MAX_METADATA_BYTES + 1))
                _drain(part)
            else:
                _drain(part)
        # Read to the end so a truncated body is told apart from a complete one.
        _drain(stream)
        if declared_length and reader.total < declared_length:
            raise _ClientGone("request body ended early")
    except _ClientGone as exc:
        upload.discard()
        raise UploadError(
            400, "upload_interrupted", "The upload was interrupted; send it again."
        ) from exc
    except (MultiPartParserError, SuspiciousOperation) as exc:
        upload.discard()
        raise UploadError(400, "invalid_multipart", "The multipart body is malformed.") from exc
    except StorageUnavailableError as exc:
        upload.discard()
        raise storage_error(exc) from exc
    except BaseException:
        upload.discard()
        raise
    if not metadata_seen:
        upload.discard()
        raise UploadError(
            400, "invalid_request", "The metadata part is missing.", {"field": "metadata"}
        )
    if upload.image_path is None:
        raise UploadError(400, "invalid_request", "The image part is missing.", {"field": "image"})
    return upload


# --- validation ---------------------------------------------------------------------------


def parse_sequence_number(raw: str) -> int:
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        raise UploadError(
            400,
            "invalid_sequence_number",
            "sequence_number must be a non-negative integer.",
            {"sequence_number": raw[:16]},
        )
    value = int(raw)
    if value > MAX_SEQUENCE_NUMBER:
        raise UploadError(
            400,
            "invalid_sequence_number",
            f"sequence_number must be at most {MAX_SEQUENCE_NUMBER}.",
            {"sequence_number": value},
        )
    return value


def _invalid(field_name: str, message: str) -> UploadError:
    return UploadError(400, "invalid_request", message, {"field": field_name})


def declared_sha256(metadata: dict[str, Any]) -> str:
    value = metadata.get("sha256")
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise _invalid("sha256", "sha256 must be a lowercase hex SHA-256.")
    return value


def captured_at(metadata: dict[str, Any]) -> datetime | None:
    value = metadata.get("captured_at")
    if value is None:
        return None
    parsed = parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise _invalid("captured_at", "captured_at must be an ISO 8601 timestamp with a zone.")
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class PageMetadata:
    sha256: str
    camera_role: str
    captured_at: datetime | None
    recognized_page_number: str | None
    status: str


def page_metadata(metadata: dict[str, Any], sequence_number: int) -> PageMetadata:
    """Validate a page's metadata part (§6.5); parity errors are ``422``."""
    sha256 = declared_sha256(metadata)
    role = metadata.get("camera_role")
    if role not in CAMERA_ROLES:
        raise _invalid("camera_role", "camera_role must be 'left' or 'right'.")
    expected_role = CAMERA_ROLES[sequence_number % 2]
    if role != expected_role:
        raise UploadError(
            422,
            "camera_role_parity",
            f"sequence_number {sequence_number} is a {expected_role} camera page.",
            {"sequence_number": sequence_number, "expected_camera_role": expected_role},
        )
    number = metadata.get("recognized_page_number")
    if number is not None and (
        not isinstance(number, str) or len(number) > MAX_RECOGNIZED_PAGE_NUMBER_LENGTH
    ):
        raise _invalid(
            "recognized_page_number",
            "recognized_page_number must be a string of at most 64 characters or null.",
        )
    status = metadata.get("status", "")
    if status is None:
        status = ""
    if not isinstance(status, str) or (status and _STATUS_RE.fullmatch(status) is None):
        raise _invalid("status", "status must be a short word such as 'ok'.")
    return PageMetadata(
        sha256=sha256,
        camera_role=role,
        captured_at=captured_at(metadata),
        recognized_page_number=number,
        status=status,
    )


def verify_checksum(upload: ReceivedUpload, expected: str) -> None:
    if upload.image_sha256 != expected:
        raise UploadError(
            422,
            "checksum_mismatch",
            "The image does not match its sha256.",
            {"expected": expected, "received": upload.image_sha256},
        )


def sniff_image(path: Path) -> tuple[str, str]:
    """``(mime_type, extension)`` of a decodable image, else ``422 invalid_image``.

    ``verify()`` checks the structure; a JPEG is then decoded at 1/8 scale, which reads
    every scan line (so a truncated file fails) at a fraction of the memory.
    """
    import warnings

    from PIL import Image

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as probe:
                image_format = probe.format or ""
                probe.verify()
            if image_format == "JPEG":
                with Image.open(path) as image:
                    width, height = image.size
                    image.draft(image.mode, (max(width // 8, 1), max(height // 8, 1)))
                    image.load()
    except Exception as exc:  # Pillow raises many types for corrupt input.
        raise UploadError(422, "invalid_image", "The image cannot be decoded.") from exc
    known = _IMAGE_FORMATS.get(image_format)
    if known is None:
        raise UploadError(
            422, "invalid_image", "The image format is not supported.", {"format": image_format}
        )
    return known


def storage_error(exc: StorageUnavailableError) -> UploadError:
    """A full or unmounted scan volume: a transient 5xx, so the scanner retries (§6.13)."""
    if isinstance(exc, InsufficientStorageError):
        return UploadError(507, "insufficient_storage", "The server is out of space.")
    return UploadError(503, "storage_unavailable", "Scan storage is unavailable.")


def publish_staged(
    upload: ReceivedUpload, storage: LocalFilesystemStorage, namespace: str, filename: str
) -> str:
    """Move the staged image to a fresh, exclusively allocated name and verify it."""
    assert upload.image_path is not None
    try:
        storage.ensure_free_space(upload.image_size)
        destination = storage.publish_unique(
            str(upload.image_path), namespace, filename, mode="move"
        )
    except StorageUnavailableError as exc:
        raise storage_error(exc) from exc
    upload.image_path = None  # the staged name is gone; the published name owns the bytes
    try:
        published_sha256 = storage.hash(destination)
    except OSError:
        storage.delete(destination)
        raise
    if published_sha256 != upload.image_sha256:
        storage.delete(destination)
        raise UploadError(
            503, "storage_verification_failed", "The stored copy did not verify; retry."
        )
    return destination


def _delete_quietly(storage: LocalFilesystemStorage, destination: str | None) -> None:
    if destination is None:
        return
    try:
        storage.delete(destination)
    except OSError:
        logger.warning("scanner_upload_cleanup_failed", storage_identifier=destination)


# --- pages (§6.5) -------------------------------------------------------------------------


@dataclass(frozen=True)
class StoredItem:
    created: bool
    version: int
    sha256: str
    state: str


def _job_state_error(state: str, what: str) -> UploadError:
    return UploadError(
        409, "job_state", f"{what} not accepted for a {state} job.", {"state": state}
    )


def _page_conflict(sequence_number: int, active: ScanPageVersion) -> UploadError:
    return UploadError(
        409,
        "page_conflict",
        "A different image already exists for this sequence_number.",
        {"sequence_number": sequence_number, "sha256": active.checksum_sha256},
    )


def _active_page_version(job_id: str, sequence_number: int) -> ScanPageVersion | None:
    return (
        ScanPageVersion.objects.filter(
            scan_page__scan_job_id=job_id,
            scan_page__logical_sequence_number=sequence_number,
            scan_page__side=ScanPage.Side.SINGLE,
            is_active=True,
        )
        .order_by("-version")
        .first()
    )


def store_page(
    *,
    job: ScanJob,
    sequence_number: int,
    upload: ReceivedUpload,
    metadata: PageMetadata,
    mime_type: str,
    extension: str,
) -> StoredItem:
    """Store a verified, staged page image as version 1 (§6.5), or confirm a repeat."""
    active = _active_page_version(job.pk, sequence_number)
    if active is not None:
        if active.checksum_sha256 == metadata.sha256:
            return StoredItem(False, active.version, active.checksum_sha256, job.state)
        raise _page_conflict(sequence_number, active)

    storage = scan_storage()
    destination: str | None = publish_staged(
        upload,
        storage,
        f"{SCANNING_NAMESPACE}/{job.pk}/{sequence_number:04d}-{ScanPage.Side.SINGLE}",
        f"{sequence_number:04d}-{metadata.sha256[:16]}{extension}",
    )
    try:
        with transaction.atomic():
            locked = ScanJob.objects.select_for_update().get(pk=job.pk)
            if locked.state not in PAGE_UPLOAD_STATES:
                raise _job_state_error(locked.state, "Pages are")
            page, _ = ScanPage.objects.get_or_create(
                scan_job=locked,
                logical_sequence_number=sequence_number,
                side=ScanPage.Side.SINGLE,
            )
            active = page.versions.filter(is_active=True).first()
            if active is not None:
                if active.checksum_sha256 == metadata.sha256:
                    return StoredItem(False, active.version, active.checksum_sha256, locked.state)
                raise _page_conflict(sequence_number, active)
            latest = page.versions.order_by("-version").first()
            version = ScanPageVersion.objects.create(
                scan_page=page,
                storage_location=scan_storage_location(),
                storage_identifier=destination,
                checksum_sha256=metadata.sha256,
                mime_type=mime_type,
                size_bytes=upload.image_size,
                capture_timestamp=metadata.captured_at,
                scanner_metadata={
                    "source": "scanner_api_v1",
                    "camera_role": metadata.camera_role,
                    "recognized_page_number": metadata.recognized_page_number,
                    "status": metadata.status,
                },
                version=(latest.version + 1) if latest is not None else 1,
                uploader=None,
                source=ScanUploadSource.SCANNER,
                original_filename=f"{sequence_number:04d}{extension}",
                is_active=True,
            )
            page.recognized_page_number = metadata.recognized_page_number
            page.status = metadata.status
            page.save(update_fields=["recognized_page_number", "status", "updated_at"])
            # A ``page`` event that arrived before the image wins (S5, #129).
            scan_events.project_page_from_events(page)
            if locked.state == ScanJob.State.OPEN:
                locked.state = ScanJob.State.CAPTURING
                locked.save(update_fields=["state", "updated_at"])
            scantailor.mark_state_stale(
                scan_job_id=str(locked.pk), page_id=str(page.pk), reason="source changed"
            )
            scan_pipeline.invalidate_scan_job_library_artifacts(locked)
            stored = StoredItem(True, version.version, metadata.sha256, locked.state)
            destination = None  # the version row now owns the published file
            return stored
    finally:
        _delete_quietly(storage, destination)


# --- covers (§6.6) ------------------------------------------------------------------------


def _active_cover(job_id: str, position: str) -> ScanCover | None:
    return ScanCover.objects.filter(scan_job_id=job_id, position=position, is_active=True).first()


def store_cover(
    *,
    job: ScanJob,
    position: str,
    upload: ReceivedUpload,
    sha256: str,
    captured: datetime | None,
    mime_type: str,
    extension: str,
) -> StoredItem:
    """Store a cover photo as a new version (§6.6); the active checksum is a repeat."""
    active = _active_cover(job.pk, position)
    if active is not None and active.checksum_sha256 == sha256:
        return StoredItem(False, active.version, sha256, job.state)

    storage = scan_storage()
    destination: str | None = publish_staged(
        upload,
        storage,
        f"{SCANNING_NAMESPACE}/{job.pk}/covers",
        f"{position}-{sha256[:16]}{extension}",
    )
    try:
        with transaction.atomic():
            locked = ScanJob.objects.select_for_update().get(pk=job.pk)
            if locked.state not in COVER_UPLOAD_STATES:
                raise _job_state_error(locked.state, "Covers are")
            active = (
                ScanCover.objects.select_for_update()
                .filter(scan_job=locked, position=position, is_active=True)
                .first()
            )
            if active is not None and active.checksum_sha256 == sha256:
                return StoredItem(False, active.version, sha256, locked.state)
            latest = (
                ScanCover.objects.filter(scan_job=locked, position=position)
                .order_by("-version")
                .first()
            )
            if active is not None:
                # Earlier versions are kept (preserve originals); only one is active.
                ScanCover.objects.filter(pk=active.pk).update(is_active=False)
            cover = ScanCover.objects.create(
                scan_job=locked,
                position=position,
                version=(latest.version + 1) if latest is not None else 1,
                storage_location=scan_storage_location(),
                storage_identifier=destination,
                checksum_sha256=sha256,
                mime_type=mime_type,
                size_bytes=upload.image_size,
                capture_timestamp=captured,
                original_filename=f"cover-{position}{extension}",
                uploader=None,
                source=ScanUploadSource.SCANNER,
                is_active=True,
            )
            stored = StoredItem(True, cover.version, sha256, locked.state)
            destination = None
            return stored
    finally:
        _delete_quietly(storage, destination)
