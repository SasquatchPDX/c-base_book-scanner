"""Scan-job and ScanTailor workspace views."""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from typing import Any, Final
from uuid import uuid4

import structlog
from asgiref.sync import sync_to_async
from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.core.files.base import File
from django.db import transaction
from django.db.models import Count, Q
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
    StreamingHttpResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from PIL import Image, UnidentifiedImageError

from ebook_server.storage import (
    LocalFilesystemStorage,
    StorageBackend,
)

from . import (
    capture_layout,
    intake,
    ocr,
    scan_identification,
    scan_images,
    scan_lifecycle,
    scan_logs,
    scan_pipeline,
    scan_thumbnails,
    scanner_opf,
    scanner_rescans,
    scantailor,
)
from .models import (
    Book,
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanPageImage,
    ScanPageVersion,
    ScanTailorStageState,
    ScanTodo,
    StorageLocation,
)
from .scan_job_ids import normalize_scan_job_id
from .storage_registry import StorageConfigurationError, backend_for_location
from .view_helpers import (
    _active_library_profile,
    _cleanup_staged_upload,
    _coerce_bool,
    _coerce_int,
    _coerce_optional_bool,
    _coerce_uuid,
    _json_body,
    _request_payload,
    _wants_html,
)
from .workspace_events import WorkspaceEventPoller

SCANNING_STORAGE_ROOT = "scan-artifacts"
SCANNING_NAMESPACE: Final[str] = "scan-jobs"

logger = structlog.get_logger(__name__)


def _normalize_stage(value: str) -> str | None:
    return scantailor.normalize_stage(value)


def _serialize_scan_page_version(version: ScanPageVersion | None) -> dict[str, Any]:
    if version is None:
        return {}
    return {
        "id": str(version.id),
        "version": version.version,
        "checksum_sha256": version.checksum_sha256,
        "mime_type": version.mime_type,
        "size_bytes": version.size_bytes,
        "capture_timestamp": (
            version.capture_timestamp.isoformat() if version.capture_timestamp else None
        ),
        "scanner_metadata": version.scanner_metadata,
        "page_number_verified": version.page_number_verified,
        "is_active": version.is_active,
        "storage_identifier": version.storage_identifier,
        "original_filename": version.original_filename,
        "created_at": version.created_at.isoformat(),
    }


def _serialize_scan_todo(todo: ScanTodo) -> dict[str, Any]:
    return {
        "id": todo.id,
        "scan_job_id": str(todo.scan_job_id),
        "scan_page_id": str(todo.scan_page_id),
        "todo_type": todo.todo_type,
        "reason": todo.reason,
        "state": todo.state,
        "creator_id": todo.creator_id,
        "claimed_by_id": todo.claimed_by_id,
        "created_at": todo.created_at.isoformat(),
        "updated_at": todo.updated_at.isoformat(),
    }


def _prefetched_active_version(versions: list[ScanPageVersion]) -> ScanPageVersion | None:
    """``ScanPage.active_version()`` computed from an already-loaded version list."""
    active = [version for version in versions if version.is_active]
    return max(active, key=lambda version: version.version) if active else None


def _serialize_scan_page(
    page: ScanPage,
    *,
    versions: list[ScanPageVersion] | None = None,
    open_todo_count: int | None = None,
) -> dict[str, Any]:
    """Serialize a page; pass ``versions``/``open_todo_count`` to avoid per-page queries."""
    if versions is None:
        active_version = page.active_version()
        ordered_versions = list(page.versions.order_by("-version"))
    else:
        active_version = _prefetched_active_version(versions)
        ordered_versions = sorted(versions, key=lambda version: version.version, reverse=True)
    if open_todo_count is None:
        open_todo_count = page.todos.filter(state=ScanTodo.State.OPEN).count()
    return {
        "id": str(page.id),
        "logical_sequence_number": page.logical_sequence_number,
        "side": page.side,
        "recognized_page_number": page.recognized_page_number,
        # The scanner's page-number check of the active version (rescan, #131).
        "page_number_verified": (
            active_version.page_number_verified if active_version is not None else None
        ),
        "scanner_status": page.status,
        "open_rescan_todos": open_todo_count,
        "active_version": _serialize_scan_page_version(active_version),
        "available_versions": [_serialize_scan_page_version(v) for v in ordered_versions],
        "created_at": page.created_at.isoformat(),
        "updated_at": page.updated_at.isoformat(),
    }


def _serialize_stage_state(state: ScanTailorStageState) -> dict[str, Any]:
    return {
        "id": state.id,
        "scan_job_id": str(state.scan_job_id),
        "scan_page_id": str(state.scan_page_id),
        "stage": state.stage,
        "stage_label": ScanTailorStageState.Stage(state.stage).label,
        "revision": state.revision,
        "status": state.status,
        "algorithm": state.algorithm,
        "algorithm_version": state.algorithm_version,
        "parameters": state.parameters,
        "input_stage_state_id": state.input_stage_state_id,
        "input_scan_page_version_id": state.input_scan_page_version_id,
        "input_storage_identifier": state.input_storage_identifier,
        "output_storage_identifier": state.output_storage_identifier,
        "preview_storage_identifier": state.preview_storage_identifier,
        "worker_job_id": state.worker_job_id,
        "output_size_bytes": state.output_size_bytes,
        "output_sha256": state.output_sha256,
        "last_error": state.last_error,
        "invalidated_reason": state.invalidated_reason,
        # Done with a manual override: a blue status icon in the page strip (#195).
        "manual": _is_manual_stage_state(state),
        "started_at": state.started_at.isoformat() if state.started_at else None,
        "finished_at": state.finished_at.isoformat() if state.finished_at else None,
        "created_at": state.created_at.isoformat(),
        "updated_at": state.updated_at.isoformat(),
    }


def _serialize_scan_job(
    job: ScanJob,
    include_pages: bool = False,
    include_open_todo_count: bool = False,
    include_pipeline: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": str(job.id),
        "state": job.state,
        "scanner_id": job.scanner_id,
        "provisional_title": job.provisional_title,
        "provisional_authors": job.provisional_authors,
        "expected_page_count": job.expected_page_count,
        "notes": job.notes,
        "book_id": str(job.book_id) if job.book_id else None,
        "operator_id": job.operator_id,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }
    if include_open_todo_count:
        payload["open_rescan_todo_count"] = getattr(job, "open_rescan_todo_count", 0)
    if include_pipeline:
        payload["pipeline"] = scan_pipeline.pipeline_progress(job)
    if include_pages:
        payload["pages"] = [
            _serialize_scan_page(page)
            for page in job.pages.all().order_by("logical_sequence_number", "side")
        ]
    return payload


_SCAN_SIDE_ALIASES: Final[dict[str, str]] = {
    "": ScanPage.Side.SINGLE,
    "single": ScanPage.Side.SINGLE,
    "s": ScanPage.Side.SINGLE,
    "left": ScanPage.Side.LEFT,
    "l": ScanPage.Side.LEFT,
    "right": ScanPage.Side.RIGHT,
    "r": ScanPage.Side.RIGHT,
}


def _normalize_scan_side(raw_side: str | None) -> str | None:
    """Return the canonical side, or ``None`` for an unrecognised value."""
    return _SCAN_SIDE_ALIASES.get((raw_side or "").strip().lower())


def _parse_json_field(payload: dict[str, Any], key: str) -> dict[str, Any]:
    value = payload.get(key, {})
    if isinstance(value, dict):
        return value
    return {}


def _is_multipart_request(request: HttpRequest) -> bool:
    content_type = request.META.get("CONTENT_TYPE", "")
    return content_type.startswith("multipart/")


def _default_scan_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(str(Path(django_settings.APP_DATA_DIR) / SCANNING_STORAGE_ROOT))


def _default_scan_storage_location() -> StorageLocation:
    storage, _ = StorageLocation.objects.get_or_create(
        identifier="scan",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": SCANNING_STORAGE_ROOT,
        },
    )
    return storage


def _normalize_scanner_metadata(raw: Any) -> dict[str, Any]:
    parsed = _parse_json_field({"scanner_metadata": raw}, "scanner_metadata")
    if parsed:
        return parsed
    if isinstance(raw, str):
        try:
            loaded = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return {}
        if isinstance(loaded, dict):
            return loaded
    return {}


def _parse_capture_ts(raw: Any) -> datetime | None:
    """Parse a capture timestamp; raise ``ValueError`` if provided but invalid."""
    if raw is None or isinstance(raw, bool):
        if isinstance(raw, bool):
            raise ValueError("invalid capture_timestamp")
        return None
    value: float | None = None
    if isinstance(raw, (int, float)):
        value = float(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        try:
            value = float(text)
        except ValueError:
            value = None
        if value is None:
            try:
                parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError("invalid capture_timestamp") from None
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=UTC)
            return parsed
    else:
        raise ValueError("invalid capture_timestamp")
    if value != value or value in {float("inf"), float("-inf")}:
        raise ValueError("invalid capture_timestamp")
    if abs(value) > 1e11:
        value /= 1000.0
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        raise ValueError("invalid capture_timestamp") from None


def _parse_scan_request_payload(request: HttpRequest) -> dict[str, Any]:
    if _is_multipart_request(request):
        return _scan_page_request_payload_for_multipart(request)
    payload = _request_payload(request)
    if payload:
        return payload
    result: dict[str, Any] = {}
    if request.method == "POST" or request.method == "PUT":
        for key, value in request.POST.items():
            result[key] = value
    return result


def _coerce_str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        stripped = value.strip()
        return stripped if stripped else None
    return str(value).strip() or None


def _coerce_optional_int_list(value: Any) -> list[int] | None:
    if value is None:
        return None
    if isinstance(value, list):
        values: list[int] = []
        for raw in value:
            parsed = _coerce_int(raw)
            if parsed is None:
                return None
            values.append(parsed)
        return values
    if isinstance(value, str):
        parsed_values: list[int] = []
        for entry in value.split(","):
            parsed_entry = _coerce_int(entry, minimum=0)
            if parsed_entry is None:
                return None
            parsed_values.append(parsed_entry)
        return parsed_values
    return None


def _stage_scan_upload(uploaded_file: Any) -> tuple[str, int]:
    staging_root = Path(django_settings.APP_DATA_DIR) / SCANNING_STORAGE_ROOT / "uploads"
    return intake.write_upload_to_staging(uploaded_file, staging_root)


def _scan_job_state_query(request: HttpRequest) -> tuple[list[str] | None, str]:
    raw_state = _coerce_str_or_none(request.GET.get("state"))
    if raw_state is None:
        return None, ""
    requested_states = [state.strip() for state in raw_state.split(",") if state.strip()]
    for state in requested_states:
        if state not in dict(ScanJob.State.choices):
            return None, state
    return requested_states, ""


def _scan_todo_state_query(request: HttpRequest) -> tuple[list[str] | None, str]:
    raw_state = _coerce_str_or_none(request.GET.get("state"))
    if raw_state is None:
        return None, ""
    requested_states = [state.strip() for state in raw_state.split(",") if state.strip()]
    for state in requested_states:
        if state not in dict(ScanTodo.State.choices):
            return None, state
    return requested_states, ""


def _scan_todos_for_query(request: HttpRequest) -> tuple[Any, str]:
    requested_states, invalid = _scan_todo_state_query(request)
    if invalid:
        return None, invalid
    states = requested_states or [
        ScanTodo.State.OPEN,
        ScanTodo.State.CLAIMED,
        ScanTodo.State.COMPLETED,
        ScanTodo.State.CANCELLED,
    ]
    queryset = ScanTodo.objects.all().order_by("-created_at")
    if states:
        queryset = queryset.filter(state__in=states)
    if raw_job_id := _coerce_str_or_none(request.GET.get("job_id")):
        job_id = normalize_scan_job_id(raw_job_id)
        if job_id is None:
            return None, "invalid job_id"
        queryset = queryset.filter(scan_job_id=job_id)
    if raw_page_id := _coerce_str_or_none(request.GET.get("scan_page_id")):
        page_id = _coerce_uuid(raw_page_id)
        if page_id is None:
            return None, "invalid scan_page_id"
        queryset = queryset.filter(scan_page_id=page_id)
    return queryset, ""


_SCAN_IMAGE_FORMATS: Final[dict[str, tuple[str, str]]] = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "TIFF": ("image/tiff", ".tif"),
    "BMP": ("image/bmp", ".bmp"),
    "WEBP": ("image/webp", ".webp"),
}
_SCAN_IMAGE_EXTENSIONS: Final[frozenset[str]] = frozenset(
    {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}
)


def _sniff_scan_image(path: str) -> tuple[str, str]:
    """Validate an image with Pillow; return ``(mime_type, extension)``."""
    from PIL import Image

    try:
        with Image.open(path) as image:
            image_format = image.format or ""
            image.verify()
    except Exception as exc:  # Pillow raises many types for corrupt input.
        raise ValueError("invalid image") from exc
    known = _SCAN_IMAGE_FORMATS.get(image_format)
    if known is None:
        raise ValueError("invalid image")
    return known


def _sanitize_client_filename(raw_name: str | None, fallback_extension: str) -> str:
    """Return a safe client filename that always carries an image extension."""
    name = Path((raw_name or "").replace("\\", "/")).name.strip()
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    suffix = Path(name).suffix.lower()
    if not name or suffix not in _SCAN_IMAGE_EXTENSIONS:
        stem = Path(name).stem if name else "page"
        name = f"{stem or 'page'}{fallback_extension}"
    if len(name) > 120:
        suffix = Path(name).suffix
        name = f"{Path(name).stem[: 120 - len(suffix)]}{suffix}"
    return name


def _build_page_storage_destination(
    *,
    scan_job_id: str,
    logical_sequence_number: int,
    side: str,
    checksum_sha256: str,
    safe_name: str,
) -> str:
    """Unique per-upload destination so copies never collide or need the job lock."""
    return _default_scan_storage().generate_destination(
        f"{SCANNING_NAMESPACE}/{scan_job_id}/{logical_sequence_number:04d}-{side}/"
        f"{checksum_sha256[:16]}-{uuid4().hex[:8]}",
        safe_name,
    )


def _append_version(
    *,
    page: ScanPage,
    uploader_id: int | None,
    destination: str,
    original_filename: str,
    checksum_sha256: str,
    mime_type: str,
    size_bytes: int,
    capture_timestamp: datetime | None,
    scanner_metadata: dict[str, Any],
) -> ScanPageVersion:
    """DB-only: record an already-stored artifact as the next active version."""
    latest = page.versions.order_by("version").last()
    next_version = (latest.version + 1) if latest is not None else 1
    page.versions.filter(is_active=True).update(is_active=False)
    return ScanPageVersion.objects.create(
        scan_page=page,
        storage_location=_default_scan_storage_location(),
        storage_identifier=destination,
        checksum_sha256=checksum_sha256,
        mime_type=mime_type,
        size_bytes=size_bytes,
        capture_timestamp=capture_timestamp,
        scanner_metadata=scanner_metadata,
        version=next_version,
        uploader_id=uploader_id,
        original_filename=original_filename,
        is_active=True,
    )


def _complete_open_rescan_todos(page: ScanPage) -> int:
    todos = page.todos.filter(
        todo_type=ScanTodo.TodoType.RESCAN,
        state__in={ScanTodo.State.OPEN, ScanTodo.State.CLAIMED},
    )
    count = todos.count()
    for todo in todos:
        todo.complete()
    return count


def _serialize_scan_page_with_thumbnail(page: ScanPage) -> dict[str, Any]:
    payload = _serialize_scan_page(page)
    if page.active_version():
        payload["thumbnail"] = _scan_page_thumbnail_url(page)
    return payload


SCAN_ARTIFACT_IMMUTABLE: Final[str] = "private, max-age=31536000, immutable"
SCAN_ARTIFACT_REVALIDATE: Final[str] = "private, max-age=0, must-revalidate"


def _stage_artifact_token(state: ScanTailorStageState) -> str:
    """Identity of a stage's current artifacts: changes whenever they are replaced."""
    if state.status != ScanTailorStageState.StageStatus.SUCCESS or not state.output_sha256:
        return ""
    return f"{state.revision}-{state.output_sha256[:16]}"


def _page_thumbnail_token(
    states: dict[str, ScanTailorStageState],
    version: ScanPageVersion | None,
    page_image: ScanPageImage | None,
) -> str:
    """Token of what ``scan_page_thumbnail`` shows, from rows the caller already has."""
    source = _select_page_thumbnail(states, version, page_image)
    return source.token if source is not None else ""


def _guess_image_type(identifier: str) -> str:
    suffix = Path(identifier).suffix.lower()
    return {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}.get(
        suffix, "application/octet-stream"
    )


def _serve_scan_artifact(
    request: HttpRequest,
    *,
    storage: StorageBackend,
    identifier: str,
    token: str,
    content_type: str | None = None,
    accel_identifier: str | None = None,
) -> HttpResponseBase:
    """Serve a scan artifact; immutable when the URL names its current token (#195).

    The workspace links ``?v=<token>``, and a new artifact always gets a new token,
    so such a URL never shows two different images and is cached for good. A request
    without the token, or with an outdated one, gets the current file and must
    revalidate (ETag). With ``BOOK_SCANNER_SCAN_ARTIFACT_ACCEL_PREFIX`` set, Django
    only checks the session and nginx sends the bytes.
    """
    immutable = bool(token) and request.GET.get("v") == token
    headers = {
        "Cache-Control": SCAN_ARTIFACT_IMMUTABLE if immutable else SCAN_ARTIFACT_REVALIDATE,
        "X-Content-Type-Options": "nosniff",
    }
    if token:
        headers["ETag"] = f'"{token}"'
        if headers["ETag"] in request.headers.get("If-None-Match", ""):
            return HttpResponse(status=304, headers=headers)
    accel_prefix = str(getattr(django_settings, "BOOK_SCANNER_SCAN_ARTIFACT_ACCEL_PREFIX", ""))
    response: HttpResponseBase
    if accel_prefix:
        response = HttpResponse(content_type=content_type or _guess_image_type(identifier))
        target = (accel_identifier or identifier).lstrip("/")
        response["X-Accel-Redirect"] = f"{accel_prefix.rstrip('/')}/{target}"
    else:
        try:
            stream = storage.open(identifier)
        except FileNotFoundError:
            return HttpResponseNotFound("preview artifact missing")
        except OSError:
            return HttpResponse("storage unavailable", status=503)
        response = FileResponse(stream, as_attachment=False, filename=Path(identifier).name)
        if content_type:
            response["Content-Type"] = content_type
    for name, value in headers.items():
        response[name] = value
    return response


def _scan_page_thumbnail_url(page: ScanPage, token: str = "") -> str:
    url = f"/scan/pages/{page.id}/image/"
    return f"{url}?v={token}" if token else url


def _serialize_scan_job_with_pages(
    job: ScanJob,
    include_pages: bool = True,
) -> dict[str, Any]:
    payload = _serialize_scan_job(job, include_pages=False, include_pipeline=True)
    if include_pages:
        payload["pages"] = [
            _serialize_scan_page_with_thumbnail(page)
            for page in job.pages.all().order_by("logical_sequence_number", "side")
        ]
    return payload


def _ordered_stages_for_page(page: ScanPage) -> list[ScanTailorStageState]:
    all_states = {
        state.stage: state
        for state in ScanTailorStageState.objects.filter(
            scan_page=page,
            scan_job=page.scan_job,
        )
    }
    ordered: list[ScanTailorStageState] = []
    for stage_name in scantailor.stage_order():
        state = all_states.get(stage_name)
        if state is not None:
            ordered.append(state)
    return ordered


def _scan_page_request_payload(request: HttpRequest) -> dict[str, Any]:
    payload = _parse_scan_request_payload(request)
    if not payload:
        if request.POST:
            payload = dict(request.POST.items())
    return payload


def _find_duplicate_version(
    job_id: Any,
    logical_sequence_number: int,
    side: str,
    checksum_sha256: str,
) -> ScanPageVersion | None:
    return (
        ScanPageVersion.objects.filter(
            scan_page__scan_job_id=job_id,
            scan_page__logical_sequence_number=logical_sequence_number,
            scan_page__side=side,
            checksum_sha256=checksum_sha256,
        )
        .order_by("-version")
        .first()
    )


def _store_scan_page(
    job: ScanJob,
    logical_sequence_number: int,
    side: str,
    uploaded_file: Any,
    uploader_id: int,
    scanner_metadata: dict[str, Any],
    capture_timestamp: datetime | None,
    requested_checksum: str | None = None,
    force_new_version: bool = False,
    accepting_states: frozenset[str] = scan_lifecycle.CAPTURE_STATES,
) -> tuple[ScanPage, ScanPageVersion, bool]:
    """Store one page image as a new (or the matching existing) version.

    ``accepting_states`` are the job states this upload is allowed in, checked under
    the row lock. A new version for a ``complete`` job (a replacement, §5) puts the
    job back to ``captured`` with "re-finish needed"; its library variants stay until
    the next finish instead of being invalidated.
    """
    staged_path, size_bytes = _stage_scan_upload(uploaded_file)
    storage = _default_scan_storage()
    destination: str | None = None
    try:
        checksum_sha256 = intake.compute_sha256(staged_path)
        if requested_checksum is not None and requested_checksum.lower() != checksum_sha256.lower():
            raise ValueError("checksum mismatch")
        mime_type, image_extension = _sniff_scan_image(staged_path)
        original_filename = _sanitize_client_filename(
            getattr(uploaded_file, "name", None),
            image_extension,
        )
        if size_bytes <= 0:
            size_bytes = Path(staged_path).stat().st_size

        # Copy the (large) bytes to their final location BEFORE taking the job lock.
        # A clearly-duplicate upload needs no copy at all.
        if force_new_version or (
            _find_duplicate_version(job.pk, logical_sequence_number, side, checksum_sha256) is None
        ):
            destination = _build_page_storage_destination(
                scan_job_id=str(job.pk),
                logical_sequence_number=logical_sequence_number,
                side=side,
                checksum_sha256=checksum_sha256,
                safe_name=original_filename,
            )
            storage.store(staged_path, destination)

        with transaction.atomic():
            locked_job = ScanJob.objects.select_for_update().get(pk=job.pk)
            if locked_job.state not in accepting_states:
                raise ValueError("job not accepting pages")

            page, _ = ScanPage.objects.get_or_create(
                scan_job=locked_job,
                logical_sequence_number=logical_sequence_number,
                side=side,
            )

            existing_version = None
            if not force_new_version:
                # Idempotent duplicate for already-uploaded artifact.
                existing_version = (
                    page.versions.filter(checksum_sha256=checksum_sha256)
                    .order_by("-version")
                    .first()
                )
            if locked_job.state == ScanJob.State.OPEN:
                locked_job.state = ScanJob.State.CAPTURING
                locked_job.save(update_fields=["state", "updated_at"])
            if existing_version is not None:
                if not existing_version.is_active:
                    page.versions.filter(is_active=True).update(is_active=False)
                    existing_version.is_active = True
                    existing_version.save(update_fields=["is_active", "updated_at"])
                page.refresh_from_db()
                return page, existing_version, False

            if destination is None:
                # Duplicate vanished between the pre-check and the lock; rare.
                destination = _build_page_storage_destination(
                    scan_job_id=str(job.pk),
                    logical_sequence_number=logical_sequence_number,
                    side=side,
                    checksum_sha256=checksum_sha256,
                    safe_name=original_filename,
                )
                storage.store(staged_path, destination)

            scan_lifecycle.reopen_for_refinish(locked_job)
            version = _append_version(
                page=page,
                uploader_id=uploader_id,
                destination=destination,
                original_filename=original_filename,
                checksum_sha256=checksum_sha256,
                mime_type=mime_type,
                size_bytes=size_bytes,
                capture_timestamp=capture_timestamp,
                scanner_metadata=scanner_metadata,
            )
            scantailor.mark_state_stale(
                scan_job_id=str(page.scan_job_id),
                page_id=str(page.id),
                reason="source changed",
            )
            if not locked_job.refinish_needed:
                scan_pipeline.invalidate_scan_job_library_artifacts(locked_job)
            destination = None  # The DB row now owns the stored file.
            return page, version, True
    finally:
        _cleanup_staged_upload(staged_path)
        if destination is not None:
            # Duplicate upload or DB failure: do not leave an orphaned copy behind.
            try:
                storage.delete(destination)
            except OSError:
                pass


SCAN_THUMBNAIL_MAX_EDGE: Final[int] = 1600


@dataclass(frozen=True)
class _ThumbnailSource:
    """The one artifact a page thumbnail is rendered from, and its identity.

    ``token`` names exactly this artifact *and* which representation of it is used
    (stage preview/output, the page's working image, or the raw scan), so the bytes
    rendered from ``identifier`` and the ETag/``?v=`` validator can never disagree.
    It is also the render-cache key. ``location`` is resolved lazily, so computing
    only the token (the workspace) never loads a storage location.
    """

    location: Callable[[], StorageLocation | None]
    identifier: str
    token: str

    @property
    def storage(self) -> StorageBackend:
        location = self.location()
        if location is None:
            return _default_scan_storage()
        return backend_for_location(location)


_THUMBNAIL_TOKEN_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")


def _select_page_thumbnail(
    states: dict[str, ScanTailorStageState],
    version: ScanPageVersion | None,
    page_image: ScanPageImage | None,
) -> _ThumbnailSource | None:
    """Pick the page thumbnail's artifact from rows the caller already loaded.

    The latest stage of the page's unbroken run of successful stages that has an
    artifact wins; a stage that is not SUCCESS ends the run, so an output whose
    upstream was edited/invalidated or is re-running is never shown (#58). Without
    one, the active version's working image (#195) or, until it exists, the raw scan.
    The workspace and ``scan_page_thumbnail`` both go through this function, so the
    URL token and the served bytes come from one selection.
    """
    latest: ScanTailorStageState | None = None
    for stage_name in scantailor.stage_order():
        state = states.get(stage_name)
        if state is None or state.status != ScanTailorStageState.StageStatus.SUCCESS:
            break
        if state.preview_storage_identifier or state.output_storage_identifier:
            latest = state
    if latest is not None:
        chosen: ScanTailorStageState = latest
        stage_token = f"s{chosen.id}-{_stage_artifact_token(chosen)}"
        if chosen.preview_storage_identifier:
            return _ThumbnailSource(
                location=lambda: chosen.preview_storage_location,
                identifier=chosen.preview_storage_identifier,
                token=f"{stage_token}-p",
            )
        return _ThumbnailSource(
            location=lambda: chosen.output_storage_location,
            identifier=chosen.output_storage_identifier,
            token=f"{stage_token}-o",
        )
    if version is None:
        return None
    version_token = f"v{version.id}-{version.checksum_sha256[:16]}"
    if page_image is not None and page_image.version_id == version.id:
        # Rendering from the working image avoids decoding the full scan (#195).
        generator = _THUMBNAIL_TOKEN_UNSAFE.sub("_", page_image.generator_version)
        return _ThumbnailSource(
            location=lambda: page_image.storage_location,
            identifier=page_image.work_storage_identifier,
            token=f"{version_token}-w{generator}",
        )
    return _ThumbnailSource(
        location=lambda: (
            version.storage_location if version.storage_location_id is not None else None
        ),
        identifier=version.storage_identifier,
        token=f"{version_token}-r",
    )


def _version_page_image(version: ScanPageVersion | None) -> ScanPageImage | None:
    """The version's working image; uses a ``page_image`` prefetch when there is one."""
    if version is None:
        return None
    try:
        return version.page_image
    except ScanPageImage.DoesNotExist:
        return None


def _work_image_token(version: ScanPageVersion, page_image: ScanPageImage) -> str:
    """Token of a version's working image; it changes if the image is regenerated."""
    generator = _THUMBNAIL_TOKEN_UNSAFE.sub("_", page_image.generator_version)
    return f"page-{version.id}-w{generator}"


# Seconds the editor should wait before asking again for a working image being generated.
PAGE_IMAGE_RETRY_AFTER_SECONDS = 2


def _page_work_identifier(page: ScanPage) -> tuple[str, str] | None:
    """Working image of the page's active version and its token.

    Returns ``("", "")`` when the page has no version, and ``None`` while the
    working image is still being generated: pages uploaded before migration 0050, or
    a new upload whose generation job has not run yet. The request never decodes the
    scan itself; it queues the (deduplicated) background job and looks again,
    because an inline queue, or a fast worker, may already have finished it.
    """
    version = page.active_version()
    if version is None:
        return "", ""
    page_image = ScanPageImage.objects.filter(version=version).first()
    if page_image is None:
        scan_images.request_page_image(version.pk)
        page_image = ScanPageImage.objects.filter(version=version).first()
    if page_image is None:
        return None
    return page_image.work_storage_identifier, _work_image_token(version, page_image)


def _scan_thumbnail_cache_path(source: _ThumbnailSource) -> Path:
    return (
        Path(django_settings.APP_DATA_DIR)
        / SCANNING_STORAGE_ROOT
        / "thumbnails"
        / f"{source.token}-{SCAN_THUMBNAIL_MAX_EDGE}.jpg"
    )


def _render_scan_thumbnail(source_artifact: _ThumbnailSource, cache_path: Path) -> None:
    """Generate a downscaled, browser-displayable JPEG for a page artifact."""
    scan_thumbnails.render_jpeg_thumbnail(
        source_artifact.storage, source_artifact.identifier, cache_path, SCAN_THUMBNAIL_MAX_EDGE
    )


def _stage_scan_page_from_existing_version(page: ScanPage) -> tuple[str, int]:
    active_version = page.active_version()
    if active_version is None:
        raise ValueError("no active page version")
    storage = (
        backend_for_location(active_version.storage_location)
        if active_version.storage_location_id is not None
        else _default_scan_storage()
    )
    with storage.open(active_version.storage_identifier) as stream:
        staged_path, size_bytes = _stage_scan_upload(
            File(stream, name=active_version.original_filename or "scan-page.bin"),
        )
    return staged_path, size_bytes


def scan_api_login_required(view: Callable[..., Any]) -> Callable[..., Any]:
    """Like ``login_required`` but API clients get 401 JSON instead of a login redirect.

    Browsers (``Accept: text/html`` or ``?view=html``) keep the redirect.
    """
    redirecting = login_required(view)

    @wraps(view)
    def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        if request.user.is_authenticated or _wants_html(request):
            return redirecting(request, *args, **kwargs)
        return JsonResponse({"error": "authentication required"}, status=401)

    return wrapper


@require_GET
@login_required
def stage_job_status(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseNotFound | HttpResponseForbidden:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.STAGE:
        return HttpResponseNotFound()
    return JsonResponse(scantailor.stage_job_payload(job))


def _scan_job_queryset(*, with_open_todo_count: bool = False) -> Any:
    queryset = (
        ScanJob.objects.select_related("book", "operator")
        .prefetch_related("pages", "pages__versions")
        .order_by("-created_at")
    )
    if with_open_todo_count:
        queryset = queryset.annotate(
            open_rescan_todo_count=Count(
                "todos",
                filter=Q(
                    todos__state=ScanTodo.State.OPEN,
                    todos__todo_type=ScanTodo.TodoType.RESCAN,
                ),
            )
        )
    return queryset


def _scan_job_transition_allowed(job: ScanJob, target_state: str) -> bool:
    if target_state == ScanJob.State.CANCELLED:
        return job.state in scan_lifecycle.PROCESSING_STATES
    if target_state == ScanJob.State.PAUSED:
        return job.state in {ScanJob.State.OPEN, ScanJob.State.CAPTURING}
    if target_state == ScanJob.State.CAPTURING:
        return job.state in {ScanJob.State.OPEN, ScanJob.State.PAUSED}
    if target_state == ScanJob.State.COMPLETE:
        return job.state in {ScanJob.State.OPEN, ScanJob.State.CAPTURING, ScanJob.State.PAUSED}
    return False


def _apply_scan_job_transition(job: ScanJob, target_state: str) -> tuple[bool, str]:
    """Move ``job`` to ``target_state`` under a row lock; already-there is a no-op success."""
    if target_state not in dict(ScanJob.State.choices):
        return False, "invalid state"
    with transaction.atomic():
        locked = ScanJob.objects.select_for_update().get(pk=job.pk)
        if locked.state == target_state:
            job.state = locked.state
            job.finished_at = locked.finished_at
            return True, ""
        if not _scan_job_transition_allowed(locked, target_state):
            job.state = locked.state
            return False, "transition not allowed"
        locked.state = target_state
        if target_state in {ScanJob.State.COMPLETE, ScanJob.State.CANCELLED}:
            locked.finished_at = timezone.now()
        locked.save(update_fields=["state", "finished_at", "updated_at"])
    job.state = locked.state
    job.finished_at = locked.finished_at
    return True, ""


def _scan_job_payload(job: ScanJob) -> dict[str, Any]:
    return _serialize_scan_job_with_pages(job, include_pages=True)


def _scan_job_payload_with_open_todos(job: ScanJob) -> dict[str, Any]:
    payload = _serialize_scan_job(
        job,
        include_open_todo_count=True,
        include_pipeline=True,
    )
    payload["pages"] = [
        _serialize_scan_page(page)
        for page in job.pages.all().order_by("logical_sequence_number", "side")
    ]
    return payload


def _ensure_workspace_stage_states(job: ScanJob, page: ScanPage) -> list[ScanTailorStageState]:
    states: list[ScanTailorStageState] = []
    for stage in scantailor.stage_order():
        states.append(
            scantailor.get_or_create_stage_state(
                scan_job_id=str(job.id),
                page_id=str(page.id),
                stage=stage,
            ),
        )
    return states


def _serialize_workspace_page(page: ScanPage) -> dict[str, Any]:
    payload = _serialize_scan_page_with_thumbnail(page)
    payload["stages"] = [_serialize_stage_state(state) for state in _ordered_stages_for_page(page)]
    return payload


def _serialize_workspace_pages(job: ScanJob) -> list[dict[str, Any]]:
    """Serialize every page of ``job`` for the workspace with a bounded query count.

    The per-page helpers above issue several queries per page and per stage, which
    made the workspace for a realistic (hundreds of pages) book time out (#51).
    """
    page_list = list(
        job.pages.annotate(
            open_todo_count=Count("todos", filter=Q(todos__state=ScanTodo.State.OPEN)),
        )
        .prefetch_related("versions", "versions__page_image")
        .order_by("logical_sequence_number", "side"),
    )
    versions_by_page = {page.id: list(page.versions.all()) for page in page_list}
    states_by_page = scantailor.ensure_stage_states_for_pages(
        job,
        page_list,
        {
            page_id: _prefetched_active_version(versions)
            for page_id, versions in versions_by_page.items()
        },
    )
    serialized: list[dict[str, Any]] = []
    for page in page_list:
        versions = versions_by_page[page.id]
        payload = _serialize_scan_page(
            page,
            versions=versions,
            open_todo_count=getattr(page, "open_todo_count", 0),
        )
        page_states = states_by_page.get(page.id, {})
        active_version = _prefetched_active_version(versions)
        if active_version is not None:
            page_image = _version_page_image(active_version)
            # Tokenised from rows already loaded: no extra queries per page.
            payload["thumbnail"] = _scan_page_thumbnail_url(
                page, _page_thumbnail_token(page_states, active_version, page_image)
            )
            # The first stage's input preview token; empty while it is being generated.
            payload["work_image_token"] = (
                _work_image_token(active_version, page_image) if page_image is not None else ""
            )
        payload["stages"] = [
            _serialize_stage_state(page_states[stage])
            for stage in scantailor.stage_order()
            if stage in page_states
        ]
        serialized.append(payload)
    return serialized


def _scan_workspace_page_payload(job: ScanJob, page: ScanPage) -> dict[str, Any]:
    _ensure_workspace_stage_states(job, page)
    return _serialize_workspace_page(page)


def _scan_workspace_event_payload(job_id: str) -> dict[str, Any] | None:
    return WorkspaceEventPoller(job_id, _serialize_stage_state).poll().payload


def _bind_stage_inputs(state: ScanTailorStageState, page: ScanPage) -> bool:
    previous = (
        state.input_stage_state_id,
        state.input_scan_page_version_id,
        state.input_storage_identifier,
    )
    predecessor_name = scantailor.previous_stage(state.stage)
    if predecessor_name is None:
        active_version = page.active_version()
        state.input_stage_state = None
        state.input_scan_page_version = active_version
        state.input_storage_identifier = active_version.storage_identifier if active_version else ""
        return previous != (
            state.input_stage_state_id,
            state.input_scan_page_version_id,
            state.input_storage_identifier,
        )

    predecessor = (
        ScanTailorStageState.objects.filter(scan_page=page, stage=predecessor_name)
        .order_by("created_at")
        .last()
    )
    state.input_stage_state = predecessor
    state.input_scan_page_version = None
    if predecessor is None:
        active_version = page.active_version()
        state.input_storage_identifier = active_version.storage_identifier if active_version else ""
    elif predecessor.output_storage_identifier:
        state.input_storage_identifier = predecessor.output_storage_identifier
    else:
        state.input_storage_identifier = ""
    return previous != (
        state.input_stage_state_id,
        state.input_scan_page_version_id,
        state.input_storage_identifier,
    )


def _conflict_response(current: ScanTailorStageState, expected_revision: int) -> JsonResponse:
    return JsonResponse(
        {
            "error": "conflict",
            "message": (
                "the stage was modified by another user; fetch the latest revision and retry"
            ),
            "current_revision": current.revision,
            "expected_revision": expected_revision,
        },
        status=409,
    )


def _contains_stage_manual_override(stage: str, parameters: dict[str, Any]) -> bool:
    if stage == "deskew":
        return "manual_angle" in parameters
    if stage == "select_content":
        mode = str(parameters.get("content_mode", "")).strip().lower()
        if mode:
            # Auto results record the box they found; only the chosen mode counts
            # (#195 step 5). Disabled is a deliberate per-page choice too.
            return mode in {"manual", "disabled"}
        return "content_box" in parameters
    if stage == "margins":
        if str(parameters.get("margins_mode", "")).strip().lower() == "manual":
            return True
        return any(
            key in parameters
            for key in {"top", "right", "bottom", "left", "margin", "canvas_width", "canvas_height"}
        )
    if stage == "dewarping":
        mode = str(parameters.get("dewarp_mode", "")).strip().lower()
        if mode:
            # Auto results record the curves they found; only the chosen mode counts.
            return mode in {"manual", "off", "bypass"}
        return "top_curve" in parameters or "dewarp_curves" in parameters
    if stage == "output":
        if str(parameters.get("zone_mode", "")).strip().lower() == "manual":
            return True
        if str(parameters.get("zone_mode", "")).strip().lower() in {"auto", "off"}:
            return False
        return any(
            key in parameters
            for key in {
                "text_box",
                "picture_box",
                "zones",
                "text_algorithm",
                "picture_algorithm",
            }
        )
    return False


def _is_manual_stage_state(state: ScanTailorStageState) -> bool:
    return _contains_stage_manual_override(
        state.stage,
        dict(state.parameters or {}),
    )


def _coerce_uuid_list(raw_values: Any) -> list[str] | None:
    if raw_values is None:
        return None
    if not isinstance(raw_values, list):
        return None
    page_ids: list[str] = []
    for value in raw_values:
        if not isinstance(value, str):
            return None
        if _coerce_uuid(value) is None:
            return None
        page_ids.append(value)
    return page_ids


def _apply_stage_payload_to_state(
    state: ScanTailorStageState,
    page: ScanPage,
    stage: str,
    payload: dict[str, Any],
) -> None:
    algorithm = payload.get("algorithm")
    if isinstance(algorithm, str) and algorithm.strip():
        state.algorithm = algorithm.strip()
    algorithm_version = payload.get("algorithm_version")
    if isinstance(algorithm_version, str) and algorithm_version.strip():
        state.algorithm_version = algorithm_version.strip()
    parameters = payload.get("parameters")
    if isinstance(parameters, dict):
        merged = dict(parameters)
        if stage == "select_content":
            if _contains_stage_manual_override(stage, merged):
                merged.setdefault("content_mode", "manual")
        elif stage == "margins":
            if _contains_stage_manual_override(stage, merged):
                merged.setdefault("margins_mode", "manual")
        elif stage == "output":
            output_mode = str(merged.get("output_mode", "")).strip().lower()
            if output_mode in {"otsu", "sauvola", "wolf", "fox", "window", "grad", "copy"}:
                state.algorithm = "copy" if output_mode == "copy" else f"binarization_{output_mode}"
        state.parameters = merged

    _bind_stage_inputs(state, page)
    state.revision += 1
    state.last_error = ""
    state.status = ScanTailorStageState.StageStatus.QUEUED


def _lock_stage_state(state: ScanTailorStageState) -> ScanTailorStageState:
    """Re-read ``state`` under a row lock; call inside ``transaction.atomic()``."""
    return (
        ScanTailorStageState.objects.select_for_update()
        .select_related("scan_job", "scan_page")
        .get(pk=state.pk)
    )


def _apply_and_save_stage_edit(
    state: ScanTailorStageState,
    page: ScanPage,
    stage: str,
    payload: dict[str, Any],
) -> None:
    """Apply a user edit to a locked ``state`` and persist it as the next revision."""
    _apply_stage_payload_to_state(state, page, stage, payload)
    state.save(
        update_fields=[
            "algorithm",
            "algorithm_version",
            "parameters",
            "input_stage_state",
            "input_scan_page_version",
            "input_storage_identifier",
            "revision",
            "last_error",
            "status",
            "updated_at",
        ],
    )


@require_GET
@scan_api_login_required
def scan_job_workspace(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")

    try:
        job = ScanJob.objects.select_related("book", "operator").get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    pages = _serialize_workspace_pages(job)
    opf_metadata = scanner_opf.display_metadata(job)
    payload = {
        "job_id": str(job.id),
        "state": job.state,
        "pages": pages,
        "count": len(pages),
        "stage_order": list(scantailor.stage_order()),
        "metadata_opf": opf_metadata,
    }
    if _wants_html(request):
        stage_navigation = [
            {"name": stage, "label": ScanTailorStageState.Stage(stage).label}
            for stage in scantailor.stage_order()
        ]
        return render(
            request,
            "core/scan_workspace.html",
            {
                "job": job,
                "pages": pages,
                "stage_navigation": stage_navigation,
                "opf_metadata": opf_metadata,
            },
        )
    return JsonResponse(payload)


# Rows changed this long before the client's cursor are sent again, so a transaction
# that committed late with an older updated_at is still delivered (idempotent merge).
WORKSPACE_STATE_OVERLAP: Final[timedelta] = timedelta(seconds=30)


@require_GET
@scan_api_login_required
def scan_job_workspace_state(request: HttpRequest, job_id: str) -> HttpResponseBase:
    """Stage rows changed since ``?since=<cursor>`` (#195 §8.2 item 8).

    The compatibility-polling client used to re-download the whole workspace every
    few seconds. This answers with two indexed queries and never writes: the job row,
    then only the stage rows updated since the cursor (all of them without one). The
    response's ``cursor`` is the server time before the read.
    """
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    job = ScanJob.objects.filter(pk=job_id).only("id", "state", "updated_at").first()
    if job is None:
        return HttpResponseNotFound()
    cursor = timezone.now()
    states = ScanTailorStageState.objects.filter(scan_job_id=job.pk)
    raw_since = request.GET.get("since")
    if raw_since:
        since = parse_datetime(raw_since)
        if since is None or since.tzinfo is None:
            return HttpResponseBadRequest("since must be an ISO 8601 time with a time zone")
        states = states.filter(updated_at__gte=since - WORKSPACE_STATE_OVERLAP)
    return JsonResponse(
        {
            "job_id": str(job.pk),
            "state": job.state,
            "updated_at": job.updated_at.isoformat(),
            "cursor": cursor.isoformat(),
            "stages": [_serialize_stage_state(state) for state in states.order_by("pk")],
        }
    )


@require_GET
@login_required
def scan_job_editor(request: HttpRequest, job_id: str) -> HttpResponseBase:
    """The ScanTailor editor: stage list, preview canvas and page strip (#195 step 3).

    Upstream Scan Tailor's main window: the whole viewport, three columns. The job
    overview (``workspace/``) keeps uploads, identification, finishing and the
    no-JavaScript Run forms.
    """
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    job = ScanJob.objects.filter(pk=job_id).first()
    if job is None:
        return HttpResponseNotFound()
    return render(
        request,
        "core/scan_editor.html",
        {
            "job": job,
            "pages": _serialize_workspace_pages(job),
            "editable": job.state in scan_lifecycle.PROCESSING_STATES,
            "stage_navigation": [
                {
                    "name": stage,
                    "label": ScanTailorStageState.Stage(stage).label,
                    "hint": EDITOR_STAGE_HINTS.get(stage, ""),
                }
                for stage in scantailor.stage_order()
            ],
        },
    )


# Status-bar hints; Deskew, Select Content and Margins are upstream Scan Tailor's
# ImageView strings (filters/*/ImageView.cpp).
EDITOR_STAGE_HINTS: Final[dict[str, str]] = {
    "deskew": "Use Ctrl+Wheel to rotate or Ctrl+Shift+Wheel for finer rotation.",
    "select_content": "Drag lines or corners to resize the content box.",
    "dewarping": (
        "Drag the red points to bend the grid; click the top or bottom curve to add a "
        "point, press Del or D to remove one."
    ),
    "margins": "Resize margins by dragging any of the solid lines.",
    "output": "Draw text and picture zones; choose how each kind is binarised.",
}


@require_GET
@login_required
async def scan_job_workspace_events(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    del request
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")

    poller = WorkspaceEventPoller(job_id, _serialize_stage_state)
    initial_update = await sync_to_async(
        poller.poll,
        thread_sensitive=True,
    )()
    if not initial_update.exists:
        return HttpResponseNotFound()

    interval = max(
        0.1,
        float(getattr(django_settings, "SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS", 1.0)),
    )
    max_events = max(
        1,
        int(getattr(django_settings, "SCANTAILOR_EVENT_STREAM_MAX_EVENTS", 45)),
    )

    async def event_stream() -> AsyncIterator[str]:
        previous: str | None = None
        for sequence in range(max_events):
            update = (
                initial_update
                if sequence == 0
                else await sync_to_async(
                    poller.poll,
                    thread_sensitive=True,
                )()
            )
            if not update.exists:
                yield "event: workspace-closed\ndata: {}\n\n"
                return
            encoded = (
                json.dumps(update.payload, separators=(",", ":"), sort_keys=True)
                if update.payload is not None
                else previous
            )
            if encoded != previous:
                yield f"event: workspace\ndata: {encoded}\n\n"
                previous = encoded
            elif sequence % 15 == 0:
                yield ": keep-alive\n\n"
            if sequence + 1 < max_events:
                await asyncio.sleep(interval)

    response = StreamingHttpResponse(event_stream(), content_type="text/event-stream")
    response["Cache-Control"] = "no-cache, no-transform"
    response["X-Accel-Buffering"] = "no"
    return response


@require_GET
@scan_api_login_required
def scan_page_stages(
    request: HttpRequest,
    job_id: str,
    page_id: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    del request
    if normalize_scan_job_id(job_id) is None or _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid uuid")

    try:
        job = ScanJob.objects.get(pk=job_id)
        page = ScanPage.objects.get(pk=page_id, scan_job=job)
    except (ScanJob.DoesNotExist, ScanPage.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    _ensure_workspace_stage_states(job, page)
    return JsonResponse(
        {
            "job_id": str(job.id),
            "page_id": str(page.id),
            "stages": [_serialize_stage_state(state) for state in _ordered_stages_for_page(page)],
            "count": page.stage_states.count(),
        },
    )


@require_http_methods(["GET", "POST"])
@scan_api_login_required
def scan_page_stage(
    request: HttpRequest,
    job_id: str,
    page_id: str,
    stage: str,
) -> HttpResponseBase:
    if normalize_scan_job_id(job_id) is None or _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid uuid")

    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    normalized_stage = _normalize_stage(stage)
    if normalized_stage is None:
        return HttpResponseBadRequest("invalid stage")

    try:
        page = ScanPage.objects.select_related("scan_job").get(pk=page_id, scan_job_id=job_id)
    except (ScanPage.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    state = scantailor.get_or_create_stage_state(
        scan_job_id=str(page.scan_job_id),
        page_id=str(page.id),
        stage=normalized_stage,
    )
    if request.method == "GET":
        _ensure_workspace_stage_states(page.scan_job, page)
        if _bind_stage_inputs(state, page):
            state.save(
                update_fields=[
                    "input_stage_state",
                    "input_scan_page_version",
                    "input_storage_identifier",
                    "updated_at",
                ],
            )
        return JsonResponse(_serialize_workspace_page(page))

    if page.scan_job.state not in scan_lifecycle.PROCESSING_STATES:
        return HttpResponseBadRequest("scan job is not accepting processing changes")

    payload = _request_payload(request)
    expected_revision = _coerce_int(payload.get("expected_revision"), minimum=1)
    # Check and bump the revision on the locked row, not on the copy read above: a
    # concurrent writer (another user, or the automatic single-page passthrough)
    # either finished first, so the expected_revision check sees it, or waits for
    # this edit and then finds a newer revision and backs off (#55).
    with transaction.atomic():
        state = _lock_stage_state(state)
        if expected_revision is not None and expected_revision != state.revision:
            return _conflict_response(state, expected_revision)
        _apply_and_save_stage_edit(state, page, normalized_stage, payload)
        scantailor.invalidate_downstream_states(
            page,
            from_stage=state.stage,
            reason=f"{state.stage} updated",
        )

    processing_job = scantailor.request_stage_job(
        state=state,
        requested_by_user_id=profile.user_id,
    )
    response_payload = {
        "stage": _serialize_stage_state(state),
        "job": scantailor.stage_job_payload(processing_job),
    }
    if _wants_html(request):
        return redirect("scan-job-workspace", job_id=page.scan_job_id)
    return JsonResponse(response_payload, status=202)


@require_POST
@scan_api_login_required
def scan_job_stage_batch(
    request: HttpRequest,
    job_id: str,
    stage: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid uuid")

    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    normalized_stage = _normalize_stage(stage)
    if normalized_stage is None:
        return HttpResponseBadRequest("invalid stage")

    try:
        job = ScanJob.objects.prefetch_related("pages").get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.state not in scan_lifecycle.PROCESSING_STATES:
        return HttpResponseBadRequest("scan job is not accepting processing changes")

    payload = _json_body(request)
    preserve_exceptions = True
    if "preserve_exceptions" in payload:
        preserve_exceptions = _coerce_bool(payload.get("preserve_exceptions"))
    page_ids = _coerce_uuid_list(payload.get("page_ids"))

    if page_ids is None:
        pages = list(job.pages.all().order_by("logical_sequence_number", "side"))
    else:
        all_pages = {str(page.id): page for page in job.pages.all()}
        pages = []
        seen: set[str] = set()
        for raw_page_id in page_ids:
            page = all_pages.get(raw_page_id)
            if page is None:
                return HttpResponseBadRequest(f"invalid page id: {raw_page_id}")
            if raw_page_id in seen:
                continue
            seen.add(raw_page_id)
            pages.append(page)

    if not pages:
        return HttpResponseBadRequest("no pages")

    applied: list[dict[str, Any]] = []
    skipped: list[str] = []
    jobs: list[dict[str, Any]] = []
    batch_job_ids: list[str] = []

    for page in pages:
        state = scantailor.get_or_create_stage_state(
            scan_job_id=str(job.id),
            page_id=str(page.id),
            stage=normalized_stage,
        )
        with transaction.atomic():
            state = _lock_stage_state(state)
            if preserve_exceptions and _is_manual_stage_state(state):
                skipped.append(str(page.id))
                continue
            _apply_and_save_stage_edit(state, page, normalized_stage, payload)
            scantailor.invalidate_downstream_states(
                page,
                from_stage=state.stage,
                reason=f"{state.stage} updated via batch",
            )
        processing_job = scantailor.request_stage_job(
            state=state,
            requested_by_user_id=profile.user_id,
            dispatch=False,
        )
        if not (processing_job.details or {}).get("superseded"):
            batch_job_ids.append(str(processing_job.job_id))
        applied.append(
            {
                "page_id": str(page.id),
                "stage": _serialize_stage_state(state),
            },
        )
        jobs.append(scantailor.stage_job_payload(processing_job))

    # One queued job per chunk of pages instead of one per page (#195 §8.2 item 5);
    # an interactive single-page edit still runs first.
    scantailor.dispatch_stage_batch(batch_job_ids)

    return JsonResponse(
        {
            "scan_job_id": str(job.id),
            "stage": normalized_stage,
            "applied": applied,
            "skipped_page_ids": skipped,
            "jobs": jobs,
            "applied_count": len(applied),
            "skipped_count": len(skipped),
        },
        status=202,
    )


@require_GET
@scan_api_login_required
def scan_page_stage_preview(
    request: HttpRequest,
    job_id: str,
    page_id: str,
    stage: str,
) -> HttpResponseBase:
    if normalize_scan_job_id(job_id) is None or _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid uuid")
    normalized_stage = _normalize_stage(stage)
    if normalized_stage is None:
        return HttpResponseBadRequest("invalid stage")

    try:
        state = ScanTailorStageState.objects.select_related("scan_page").get(
            scan_job_id=job_id,
            scan_page_id=page_id,
            stage=normalized_stage,
        )
    except (ScanTailorStageState.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    wants_input = _coerce_bool(request.GET.get("input"))
    token = ""
    if wants_input and state.input_stage_state is not None:
        identifier = (
            state.input_stage_state.preview_storage_identifier
            or state.input_stage_state.output_storage_identifier
        )
        token = _stage_artifact_token(state.input_stage_state)
    elif wants_input:
        # The first stage edits the page's working image (#195), never the raw scan.
        work = _page_work_identifier(state.scan_page)
        if work is None:
            # Being generated: a distinct, retryable answer the editor polls on.
            pending = HttpResponse("input preview not ready", status=503, content_type="text/plain")
            pending["Retry-After"] = str(PAGE_IMAGE_RETRY_AFTER_SECONDS)
            pending["Cache-Control"] = "no-store"
            return pending
        identifier, token = work
    else:
        identifier = state.preview_storage_identifier or state.output_storage_identifier
        token = _stage_artifact_token(state)
    if not identifier:
        message = "input preview not ready" if wants_input else "preview not ready"
        return HttpResponseNotFound(message)
    return _serve_scan_artifact(
        request, storage=_default_scan_storage(), identifier=identifier, token=token
    )


@require_http_methods(["GET", "POST"])
@scan_api_login_required
def scan_jobs(
    request: HttpRequest,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    if request.method == "GET":
        requested, invalid = _scan_job_state_query(request)
        if invalid:
            return HttpResponseBadRequest(f"invalid state: {invalid}")
        include_open_todos = _coerce_bool(request.GET.get("with_open_todos"))
        states = requested or [
            ScanJob.State.OPEN,
            ScanJob.State.CAPTURING,
            ScanJob.State.PAUSED,
            ScanJob.State.CAPTURED,
            ScanJob.State.FINALIZING,
        ]
        queryset = _scan_job_queryset(with_open_todo_count=include_open_todos)
        queryset = queryset.filter(state__in=states)
        if include_open_todos:
            queryset = queryset.filter(
                todos__state=ScanTodo.State.OPEN,
                todos__todo_type=ScanTodo.TodoType.RESCAN,
            ).distinct()
        scanner_id = _coerce_str_or_none(request.GET.get("scanner_id"))
        if scanner_id:
            queryset = queryset.filter(scanner_id=scanner_id)
        serializer = _scan_job_payload_with_open_todos if include_open_todos else _scan_job_payload
        response_payload = {
            "items": [serializer(job) for job in queryset],
            "count": queryset.count(),
        }
        if _wants_html(request):
            return render(
                request,
                "core/scan_jobs.html",
                {
                    "jobs": list(queryset),
                    "states": ScanJob.State.choices,
                    "books": Book.objects.order_by("title")[:500],
                },
            )
        return JsonResponse(response_payload)

    payload = _parse_scan_request_payload(request)
    job_title = _coerce_str_or_none(payload.get("provisional_title")) or ""
    raw_authors = payload.get("provisional_authors", [])
    if isinstance(raw_authors, str):
        provisional_authors = [
            author.strip() for author in raw_authors.split(",") if author.strip()
        ]
    elif isinstance(raw_authors, list):
        provisional_authors = [str(author).strip() for author in raw_authors if str(author).strip()]
    else:
        provisional_authors = []
    expected_page_count = _coerce_int(payload.get("expected_page_count"), minimum=1)
    scanner_id = _coerce_str_or_none(payload.get("scanner_id")) or ""
    book_id = _coerce_int(payload.get("book_id"), minimum=1)
    if book_id is not None:
        try:
            book = Book.objects.get(pk=book_id)
        except Book.DoesNotExist:
            return HttpResponseBadRequest("book not found")
    else:
        book = None

    if not capture_layout.is_accepted(payload.get("capture_layout")):
        return HttpResponseBadRequest(capture_layout.UNSUPPORTED_MESSAGE)

    notes = _normalize_scanner_metadata(payload.get("notes"))
    initial_state = _coerce_str_or_none(payload.get("state")) or ScanJob.State.OPEN
    if initial_state not in {
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
    }:
        return HttpResponseBadRequest("invalid state")

    job = ScanJob.objects.create(
        state=initial_state,
        scanner_id=scanner_id,
        operator=profile.user,
        book=book,
        provisional_title=job_title,
        provisional_authors=provisional_authors,
        expected_page_count=expected_page_count,
        notes=notes,
    )
    if _wants_html(request):
        return redirect("scan-job-workspace", job_id=job.id)
    return JsonResponse(_scan_job_payload(job), status=201)


@require_GET
@scan_api_login_required
def scan_job_detail(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    del request
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    try:
        job = (
            ScanJob.objects.select_related("book", "operator")
            .prefetch_related(
                "pages",
                "pages__versions",
            )
            .get(pk=job_id)
        )
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    payload = _scan_job_payload(job)
    payload["logs"] = scan_logs.serialize_job_logs(job)
    return JsonResponse(payload)


@require_GET
@scan_api_login_required
def scan_job_todos(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")

    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    requested_states, invalid = _scan_todo_state_query(request)
    if invalid:
        return HttpResponseBadRequest(f"invalid state: {invalid}")
    states = requested_states or [
        ScanTodo.State.OPEN,
        ScanTodo.State.CLAIMED,
        ScanTodo.State.COMPLETED,
        ScanTodo.State.CANCELLED,
    ]
    todos = ScanTodo.objects.filter(scan_job=job, state__in=states).order_by("-created_at")
    return JsonResponse(
        {
            "scan_job_id": str(job.id),
            "items": [_serialize_scan_todo(todo) for todo in todos],
            "count": todos.count(),
        }
    )


@require_POST
@scan_api_login_required
def scan_job_ocr_request(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        scan_job = ScanJob.objects.select_related("book").get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if scan_job.book_id is None:
        return HttpResponseBadRequest("scan job must be linked to a book")

    payload = _json_body(request)
    try:
        job = ocr.request_ocr_from_scan_job(
            scan_job=scan_job,
            requested_by_user_id=profile.user_id,
            target_formats=payload.get("target_formats"),
            request_language=payload.get("language")
            if isinstance(payload.get("language"), str)
            else None,
            engine=payload.get("engine") if isinstance(payload.get("engine"), str) else None,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            request_options=payload.get("request_options")
            if isinstance(payload.get("request_options"), dict)
            else None,
        )
    except ocr.OCRJobError as exc:
        return HttpResponseBadRequest(str(exc))
    return JsonResponse(ocr.ocr_job_payload(job), status=202)


@require_POST
@scan_api_login_required
def scan_job_identification_request(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        scan_job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    payload = _request_payload(request)
    title = payload.get("provisional_title")
    raw_authors = payload.get("provisional_authors")
    update_fields: list[str] = []
    if isinstance(title, str) and title.strip():
        scan_job.provisional_title = title.strip()
        update_fields.append("provisional_title")
    if isinstance(raw_authors, str):
        scan_job.provisional_authors = [
            author.strip() for author in raw_authors.split(",") if author.strip()
        ]
        update_fields.append("provisional_authors")
    elif isinstance(raw_authors, list):
        scan_job.provisional_authors = [
            str(author).strip() for author in raw_authors if str(author).strip()
        ]
        update_fields.append("provisional_authors")
    if update_fields:
        scan_job.save(update_fields=[*update_fields, "updated_at"])

    try:
        job = scan_identification.request_scan_identification(
            scan_job=scan_job,
            requested_by_user_id=profile.user_id,
        )
    except scan_identification.ScanIdentificationError as exc:
        return HttpResponseBadRequest(str(exc))
    response_payload = scan_identification.job_payload(job)
    if _wants_html(request):
        review_task_id = response_payload.get("review_task_id")
        if review_task_id is not None:
            return redirect("review-detail", task_id=review_task_id)
        return redirect("scan-identification-job-status", job_id=job.job_id)
    return JsonResponse(response_payload, status=202)


@require_GET
@scan_api_login_required
def scan_identification_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.SCAN_IDENTIFICATION:
        return HttpResponseNotFound()
    payload = scan_identification.job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Scan identification"},
        )
    return JsonResponse(payload)


@require_GET
@scan_api_login_required
def scan_todos(
    request: HttpRequest,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseForbidden:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    queryset, invalid = _scan_todos_for_query(request)
    if queryset is None:
        return HttpResponseBadRequest(f"invalid filter: {invalid}")

    todos = queryset
    return JsonResponse(
        {
            "items": [_serialize_scan_todo(todo) for todo in todos],
            "count": todos.count(),
        },
    )


@require_POST
@scan_api_login_required
def scan_page_request_rescan(
    request: HttpRequest,
    page_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid page id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        page = ScanPage.objects.select_related("scan_job").get(pk=page_id)
    except (ScanPage.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if page.scan_job.state not in scan_lifecycle.RESCAN_STATES:
        return HttpResponseBadRequest("scan job is not accepting rescan requests")

    payload = _scan_page_request_payload(request)
    reason = _coerce_str_or_none(payload.get("reason")) or ""

    # Creates the todo and, for a scanner job, appends ``rescan_flag`` to scan.jsonl in
    # the same transaction (#131, contract §6.11); the state is re-checked under lock.
    try:
        flagged = scanner_rescans.flag_page(page, reason=reason, user=profile.user)
    except scan_lifecycle.LifecycleError:
        return HttpResponseBadRequest("scan job is not accepting rescan requests")
    if _wants_html(request):
        return redirect("scan-job-workspace", job_id=page.scan_job_id)
    return JsonResponse(
        {
            "todo": _serialize_scan_todo(flagged.todo),
            "created": flagged.created,
            "scan_job_id": str(page.scan_job_id),
        },
        status=201 if flagged.created else 200,
    )


@require_http_methods(["GET", "POST"])
@scan_api_login_required
def upload_scan_log(request: HttpRequest, job_id: str) -> HttpResponseBase:
    """List a scan job's scanner logs (GET) or upload one (POST, processed by a worker).

    POST only stages the bounded upload and queues a ``scan_log`` processing job
    (202); hashing and storing happen on a worker. The log's ``state`` (``queued``,
    ``stored``, ``duplicate``, ``failed``) is reported here and on the scan job.
    """
    if _coerce_uuid(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if request.method == "GET":
        return JsonResponse({"job_id": str(job.id), "logs": scan_logs.serialize_job_logs(job)})
    if job.state == ScanJob.State.CANCELLED:
        return HttpResponseBadRequest("job cancelled")
    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return HttpResponseBadRequest("missing_file")
    try:
        log = scan_logs.stage_scan_log(job, uploaded_file, profile.user_id)
    except scan_logs.ScanLogError as exc:
        return HttpResponseBadRequest(str(exc))
    except scan_logs.ScanLogDispatchError:
        logger.exception("scan log dispatch failed", scan_job_id=str(job.id))
        return JsonResponse({"error": "queue unavailable", "retryable": True}, status=503)
    except OSError:
        logger.exception("scan log staging failed", scan_job_id=str(job.id))
        return JsonResponse({"error": "storage unavailable", "retryable": True}, status=503)
    return JsonResponse(
        {
            "job_id": str(job.id),
            "log": scan_logs.serialize_scan_log(log),
            "status_url": reverse("scan-job-upload-log", args=[job.id]),
        },
        status=202,
    )


@require_POST
@scan_api_login_required
def upload_scan_page(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    if job.state not in {
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
    }:
        return HttpResponseBadRequest("job not accepting pages")

    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return HttpResponseBadRequest("missing_file")

    payload = _scan_page_request_payload(request)
    logical_sequence_number = _coerce_int(payload.get("logical_sequence_number"), minimum=1)
    if logical_sequence_number is None:
        return HttpResponseBadRequest("logical_sequence_number required")
    side = _normalize_scan_side(_coerce_str_or_none(payload.get("side")))
    if side is None:
        return HttpResponseBadRequest("invalid side (expected left, right or single)")
    try:
        capture_timestamp = _parse_capture_ts(payload.get("capture_timestamp"))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    scanner_metadata = _normalize_scanner_metadata(payload.get("scanner_metadata"))
    requested_checksum = _coerce_str_or_none(payload.get("checksum_sha256"))

    try:
        page, version, created = _store_scan_page(
            job=job,
            logical_sequence_number=logical_sequence_number,
            side=side,
            uploaded_file=uploaded_file,
            uploader_id=profile.user_id,
            scanner_metadata=scanner_metadata,
            capture_timestamp=capture_timestamp,
            requested_checksum=requested_checksum,
        )
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    except OSError:
        logger.exception("scan page storage failed", scan_job_id=str(job.id))
        return JsonResponse({"error": "storage unavailable", "retryable": True}, status=503)

    if created:
        _complete_open_rescan_todos(page)

    status_code = 201 if created else 200
    if _wants_html(request):
        return redirect("scan-job-workspace", job_id=job.id)
    return JsonResponse(
        {
            "job_id": str(job.id),
            "page": _serialize_scan_page(page),
            "created": created,
            "version": {
                "id": version.id,
                "version": version.version,
                "checksum_sha256": version.checksum_sha256,
                "storage_identifier": version.storage_identifier,
            },
        },
        status=status_code,
    )


@require_http_methods(["GET", "PUT"])
@scan_api_login_required
def scan_page(
    request: HttpRequest,
    page_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid page id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        page = ScanPage.objects.select_related("scan_job").get(pk=page_id)
    except (ScanPage.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    if request.method == "GET":
        return JsonResponse(_serialize_scan_page_with_thumbnail(page))

    payload = _scan_page_request_payload(request)
    requested_checksum = _coerce_str_or_none(payload.get("checksum_sha256"))
    _, uploaded_files = _multipart_data(request)
    uploaded_file: Any = uploaded_files.get("file")
    if uploaded_file is None:
        return HttpResponseBadRequest("missing_file")

    try:
        capture_timestamp = _parse_capture_ts(payload.get("capture_timestamp"))
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    scanner_metadata = _normalize_scanner_metadata(payload.get("scanner_metadata"))
    job = page.scan_job

    if job.state not in scan_lifecycle.RESCAN_STATES:
        return HttpResponseBadRequest("job not accepting pages")

    try:
        page, version, created = _store_scan_page(
            job=job,
            logical_sequence_number=page.logical_sequence_number,
            side=page.side,
            uploaded_file=uploaded_file,
            uploader_id=profile.user_id,
            scanner_metadata=scanner_metadata,
            capture_timestamp=capture_timestamp,
            requested_checksum=requested_checksum,
            accepting_states=scan_lifecycle.RESCAN_STATES,
        )
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    except OSError:
        logger.exception("scan page storage failed", scan_job_id=str(job.id))
        return JsonResponse({"error": "storage unavailable", "retryable": True}, status=503)

    if created:
        _complete_open_rescan_todos(page)

    return JsonResponse(
        {
            "page_id": str(page.id),
            "page": _serialize_scan_page(page),
            "created": created,
            "version": {
                "id": version.id,
                "version": version.version,
                "checksum_sha256": version.checksum_sha256,
            },
        },
        status=201 if created else 200,
    )


@require_POST
@scan_api_login_required
def scan_todo_claim(
    request: HttpRequest,
    todo_id: int,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseForbidden | HttpResponseNotFound:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        todo = ScanTodo.objects.get(pk=todo_id)
    except (ScanTodo.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    if todo.state == ScanTodo.State.CANCELLED:
        return HttpResponseBadRequest("todo cancelled")
    if todo.state == ScanTodo.State.COMPLETED:
        return HttpResponseBadRequest("todo already completed")
    if todo.state == ScanTodo.State.CLAIMED:
        if todo.claimed_by_id != profile.user_id:
            return HttpResponseBadRequest("todo already claimed")
        return JsonResponse({"todo_id": todo.id, "state": todo.state})

    todo.claim(profile.user)
    return JsonResponse(
        {
            "todo_id": todo.id,
            "state": todo.state,
            "claimed_by_id": todo.claimed_by_id,
        },
    )


@require_POST
@scan_api_login_required
def scan_todo_complete(
    request: HttpRequest,
    todo_id: int,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseForbidden | HttpResponseNotFound:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        todo = ScanTodo.objects.get(pk=todo_id)
    except (ScanTodo.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    if todo.state == ScanTodo.State.COMPLETED:
        return JsonResponse({"todo_id": todo.id, "state": todo.state})
    if todo.state == ScanTodo.State.CANCELLED:
        return HttpResponseBadRequest("todo cancelled")

    todo.complete()
    return JsonResponse({"todo_id": todo.id, "state": todo.state})


@require_POST
@scan_api_login_required
def scan_todo_cancel(
    request: HttpRequest,
    todo_id: int,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseForbidden | HttpResponseNotFound:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        todo = ScanTodo.objects.get(pk=todo_id)
    except (ScanTodo.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    if todo.state == ScanTodo.State.CANCELLED:
        return JsonResponse({"todo_id": todo.id, "state": todo.state})
    todo.cancel()
    return JsonResponse({"todo_id": todo.id, "state": todo.state})


@require_GET
@scan_api_login_required
def scan_page_stage_thumbnail(
    request: HttpRequest, job_id: str, page_id: str, stage: str
) -> HttpResponseBase:
    """Strip thumbnail of a completed stage (#195 step 3), cacheable with ``?v=``.

    Stages published before thumbnails existed get theirs made from the preview on
    first request (small: the preview is working-resolution).
    """
    if normalize_scan_job_id(job_id) is None or _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid uuid")
    normalized_stage = _normalize_stage(stage)
    if normalized_stage is None:
        return HttpResponseBadRequest("invalid stage")
    state = ScanTailorStageState.objects.filter(
        scan_job_id=job_id, scan_page_id=page_id, stage=normalized_stage
    ).first()
    if state is None:
        return HttpResponseNotFound()
    token = _stage_artifact_token(state)
    identifier = scantailor.stage_thumbnail_identifier(state)
    if not token or not identifier:
        return HttpResponseNotFound("thumbnail not ready")
    storage = _default_scan_storage()
    if not storage.exists(identifier):
        source = state.preview_storage_identifier or state.output_storage_identifier
        try:
            with Image.open(storage.resolve_location(source)) as preview:
                preview.load()
                small = scantailor._thumbnail(
                    scan_images.ensure_rgb_or_l(preview), scan_images.THUMBNAIL_EDGE
                )
            with tempfile.TemporaryDirectory(prefix="stage-thumb-") as tempdir:
                staged = Path(tempdir) / "thumbnail.jpg"
                scan_images.save_preview_jpeg(small, staged)
                storage.store_atomic(str(staged), identifier)
        except FileNotFoundError:
            return HttpResponseNotFound("preview artifact missing")
        except (OSError, UnidentifiedImageError):
            return HttpResponse("storage unavailable", status=503)
    return _serve_scan_artifact(
        request, storage=storage, identifier=identifier, token=token, content_type="image/jpeg"
    )


@require_GET
@scan_api_login_required
def scan_page_small_thumbnail(request: HttpRequest, page_id: str) -> HttpResponseBase:
    """The 400 px thumbnail of the page's active version (#195), for the page strip."""
    if _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid page id")
    page = ScanPage.objects.filter(pk=page_id).first()
    if page is None:
        return HttpResponseNotFound()
    version = page.active_version()
    if version is None:
        return HttpResponseNotFound("no page version")
    page_image = (
        ScanPageImage.objects.select_related("storage_location").filter(version=version).first()
    )
    if page_image is None:
        scan_images.request_page_image(version.pk)  # deduplicated (#197)
        return HttpResponseNotFound("thumbnail not ready")
    return _serve_scan_artifact(
        request,
        storage=backend_for_location(page_image.storage_location),
        identifier=page_image.thumbnail_storage_identifier,
        token=_work_image_token(version, page_image),
        content_type="image/jpeg",
    )


@require_GET
@scan_api_login_required
def scan_page_thumbnail(
    request: HttpRequest,
    page_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid page id")
    try:
        page = ScanPage.objects.get(pk=page_id)
    except (ScanPage.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    # One read of the stage rows and of the active version selects ONE artifact; the
    # bytes and the token/ETag both derive from that selection, so an invalidation
    # racing this request can never pair new bytes with an old immutable token.
    states = {
        state.stage: state
        for state in ScanTailorStageState.objects.filter(
            scan_job_id=page.scan_job_id, scan_page=page
        ).select_related("preview_storage_location", "output_storage_location")
    }
    version = page.active_version()
    page_image = (
        ScanPageImage.objects.select_related("storage_location").filter(version=version).first()
        if version is not None
        else None
    )
    source = _select_page_thumbnail(states, version, page_image)
    if source is None:
        return HttpResponseNotFound("no page version")
    token = source.token
    etag = f'"{token}"'
    if etag in request.headers.get("If-None-Match", ""):
        return HttpResponse(
            status=304, headers={"ETag": etag, "Cache-Control": SCAN_ARTIFACT_REVALIDATE}
        )

    cache_path = _scan_thumbnail_cache_path(source)
    if not cache_path.exists():
        try:
            _render_scan_thumbnail(source, cache_path)
        except (FileNotFoundError, StorageConfigurationError):
            return HttpResponseNotFound("artifact missing")
        except Exception:  # undecodable image data
            return HttpResponse("unsupported image", status=415)

    scan_root = Path(django_settings.APP_DATA_DIR) / SCANNING_STORAGE_ROOT
    return _serve_scan_artifact(
        request,
        storage=LocalFilesystemStorage(str(cache_path.parent)),
        identifier=cache_path.name,
        token=token,
        content_type="image/jpeg",
        accel_identifier=cache_path.relative_to(scan_root).as_posix(),
    )


@require_GET
@login_required
def scan_job_portal_link(request: HttpRequest, job_id: str) -> HttpResponseBase:
    """``/j/<job_id>`` (contract §9): the scanner's QR code. Login, then the workspace.

    It answers as soon as the job is registered (§8.4); an unknown ID is a 404.
    """
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseNotFound()
    if _active_library_profile(request) is None:
        return HttpResponseForbidden()
    if not ScanJob.objects.filter(pk=job_id).exists():
        return HttpResponseNotFound()
    return redirect(reverse("scan-job-workspace", kwargs={"job_id": job_id}) + "?view=html")


@require_GET
@scan_api_login_required
def raw_scans(request: HttpRequest) -> HttpResponseBase:
    """Portal list of raw scans (``captured``, contract §9): not in the library yet."""
    if _active_library_profile(request) is None:
        return HttpResponseForbidden()
    summaries = scan_lifecycle.raw_scan_summaries()
    if _wants_html(request):
        return render(request, "core/raw_scans.html", {"summaries": summaries})
    return JsonResponse(
        {
            "items": [
                {
                    "job_id": str(summary.job.pk),
                    "title": summary.title,
                    "page_count": summary.page_count,
                    "open_rescans": summary.open_rescans,
                    "refinish_needed": summary.refinish_needed,
                    "completeness": summary.completeness,
                    "metadata": summary.metadata,
                    "scanner_id": summary.job.scanner_id,
                    "captured_at": (
                        summary.job.captured_at.isoformat() if summary.job.captured_at else None
                    ),
                }
                for summary in summaries
            ],
            "count": len(summaries),
        }
    )


@require_POST
@scan_api_login_required
def scan_job_pause(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseNotFound | HttpResponseBadRequest | HttpResponseForbidden:
    del request
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    ok, reason = _apply_scan_job_transition(job, ScanJob.State.PAUSED)
    if not ok:
        return HttpResponseBadRequest(reason)
    return JsonResponse({"job_id": str(job.id), "state": job.state})


@require_POST
@scan_api_login_required
def scan_job_resume(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseNotFound | HttpResponseBadRequest | HttpResponseForbidden:
    del request
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    ok, reason = _apply_scan_job_transition(job, ScanJob.State.CAPTURING)
    if not ok:
        return HttpResponseBadRequest(reason)
    return JsonResponse({"job_id": str(job.id), "state": job.state})


@require_POST
@scan_api_login_required
def scan_job_finish(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()

    if job.book_id is None and job.state in scan_lifecycle.PROCESSING_STATES:
        # S6 (#130) links from the job's OPF here; without a linker (or when it
        # finds nothing to link) scan identification and the review queue run.
        scan_lifecycle.link_book_for_finish(job)
    if job.book_id is None:
        try:
            identification_job = scan_identification.request_scan_identification(
                scan_job=job,
                requested_by_user_id=profile.user_id,
            )
        except scan_identification.ScanIdentificationError as exc:
            return HttpResponseBadRequest(str(exc))
        identification_payload = scan_identification.job_payload(identification_job)
        if _wants_html(request):
            review_task_id = identification_payload.get("review_task_id")
            if review_task_id is not None:
                return redirect("review-detail", task_id=review_task_id)
            return redirect(
                "scan-identification-job-status",
                job_id=identification_job.job_id,
            )
        return JsonResponse(
            {
                "job_id": str(job.id),
                "state": job.state,
                "requires_bibliographic_review": True,
                "identification_job": identification_payload,
            },
            status=202,
        )

    payload = _json_body(request)
    if not payload:
        try:
            payload = dict(request.POST.items())
        except Exception:  # pragma: no cover - defensive for odd requests
            payload = {}

    create_cleaned_variant = _coerce_optional_bool(
        payload.get("create_cleaned_variant"),
        default=True,
    )
    allow_raw_pages = _coerce_optional_bool(payload.get("allow_raw_pages"), default=False)
    auto_ocr = _coerce_optional_bool(payload.get("auto_ocr"), default=False)
    target_formats = payload.get("target_formats")
    language = payload.get("language") if isinstance(payload.get("language"), str) else None
    engine = payload.get("engine") if isinstance(payload.get("engine"), str) else None
    model = payload.get("model") if isinstance(payload.get("model"), str) else None
    request_options = (
        payload.get("request_options") if isinstance(payload.get("request_options"), dict) else None
    )

    try:
        finalization_job = scan_pipeline.request_scan_finalization(
            scan_job=job,
            requested_by_user_id=profile.user_id,
            create_cleaned_variant=create_cleaned_variant,
            allow_raw_pages=allow_raw_pages,
            auto_ocr=auto_ocr,
            target_formats=target_formats,
            request_language=language,
            engine=engine,
            model=model,
            request_options=request_options,
        )
    except scan_pipeline.ScanPipelineError as error:
        message = str(error)
        body: dict[str, Any] = {"error": message}
        if message.startswith("missing processed output"):
            _, _, id_part = message.partition(": ")
            body["missing_pages"] = [item.strip() for item in id_part.split(",") if item.strip()]
            body["hint"] = "run ScanTailor stages or resend with allow_raw_pages=true"
        return JsonResponse(body, status=400)

    job.refresh_from_db()
    response: dict[str, Any] = {
        "job_id": str(job.id),
        "state": job.state,
        "pipeline": scan_pipeline.pipeline_progress(job),
        "finalization_job": scan_pipeline.finalization_job_payload(finalization_job),
    }
    if _wants_html(request):
        return redirect("scan-finalization-job-status", job_id=finalization_job.job_id)
    return JsonResponse(response, status=202)


@require_GET
@scan_api_login_required
def scan_finalization_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.SCAN_FINALIZATION:
        return HttpResponseNotFound()
    payload = scan_pipeline.finalization_job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Scan finalization"},
        )
    return JsonResponse(payload)


@require_POST
@scan_api_login_required
def scan_job_cancel(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseNotFound | HttpResponseBadRequest | HttpResponseForbidden:
    del request
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    ok, reason = _apply_scan_job_transition(job, ScanJob.State.CANCELLED)
    if not ok:
        return HttpResponseBadRequest(reason)
    return JsonResponse({"job_id": str(job.id), "state": job.state})


def _multipart_data(request: HttpRequest) -> tuple[Any, Any]:
    """Return ``(fields, files)`` for a multipart request of any method.

    Django only parses multipart bodies for POST; PUT bodies must be parsed by hand.
    """
    if request.method == "POST":
        return request.POST, request.FILES
    cached = getattr(request, "_scan_multipart", None)
    if cached is None:
        from django.http.multipartparser import MultiPartParser, MultiPartParserError

        try:
            parser = MultiPartParser(
                request.META,
                request,  # type: ignore[arg-type]
                request.upload_handlers,
                request.encoding,
            )
            cached = parser.parse()
        except (MultiPartParserError, ValueError):
            cached = ({}, {})
        request._scan_multipart = cached  # type: ignore[attr-defined]
    return cached


def _scan_page_request_payload_for_multipart(request: HttpRequest) -> dict[str, Any]:
    fields, _ = _multipart_data(request)
    return {key: value for key, value in fields.items()}
