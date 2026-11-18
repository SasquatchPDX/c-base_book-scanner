"""Core application views."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from functools import wraps
from pathlib import Path
from typing import Any, Final, cast
from urllib.parse import urlencode
from uuid import uuid4

import redis
import structlog
from asgiref.sync import sync_to_async
from django.conf import settings as django_settings
from django.contrib import messages
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.core.files.base import File
from django.core.paginator import Paginator
from django.db import connections, transaction
from django.db.models import Count, F, OuterRef, Prefetch, Q, Subquery
from django.db.models.functions import Coalesce
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
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from ebook_server.storage import (
    LocalFilesystemStorage,
    StorageBackend,
    check_storage_root,
    free_bytes,
    min_free_bytes,
    probe_storage_writable,
)

from . import (
    book_deletion,
    combine,
    contributions,
    conversion,
    genres,
    intake,
    library_visibility,
    llm,
    llm_identification,
    local_identity,
    metadata_search,
    multipart,
    ocr,
    request_limits,
    review_accept,
    review_batch,
    review_identification,
    scan_identification,
    scan_lifecycle,
    scan_logs,
    scan_pipeline,
    scan_thumbnails,
    scanner_opf,
    scanner_rescans,
    scantailor,
    search,
)
from . import thumbnails as thumbnails_module
from .metrics import PersistentStateCollector
from .models import (
    AppUserProfile,
    Author,
    Book,
    Category,
    CoverImage,
    CoverThumbnail,
    FileVariant,
    Genre,
    LLMSuggestion,
    MetadataCandidate,
    MetadataSearch,
    ProcessingJob,
    ReviewDecisionCorrection,
    ReviewTask,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    ScanTailorStageState,
    ScanTodo,
    SourceLocation,
    StorageLocation,
)
from .referenced_files import ReferencedFileError, ensure_reference_intact
from .scan_job_ids import normalize_scan_job_id
from .storage_registry import StorageConfigurationError, backend_for_location
from .view_helpers import (
    _active_library_profile,
    _as_positive_int,
    _cleanup_staged_upload,
    _coerce_bool,
    _coerce_int,
    _coerce_optional_bool,
    _coerce_uuid,
    _json_body,
    _limited_response,
    _profile_for_request,
    _request_payload,
    _wants_html,
)

SCANNING_STORAGE_ROOT = "scan-artifacts"
SCANNING_NAMESPACE: Final[str] = "scan-jobs"

logger = structlog.get_logger(__name__)


@require_http_methods(["GET", "POST"])
def library_login(request: HttpRequest) -> HttpResponseBase:
    if request.user.is_authenticated:
        return redirect("library-home")
    next_url = (
        request.GET.get("next", "") if request.method == "GET" else request.POST.get("next", "")
    )
    error = ""
    if request.method == "POST":
        username = request.POST.get("username", "").strip()
        password = request.POST.get("password", "")
        try:
            ticket = request_limits.reserve_login(username, request.META.get("REMOTE_ADDR", ""))
        except request_limits.RequestLimited as exc:
            return _limited_response("login", exc)
        user = authenticate(request, username=username, password=password)
        if user is not None:
            request_limits.release_successful_login(ticket)
            login(request, user)
            if next_url and url_has_allowed_host_and_scheme(
                next_url,
                allowed_hosts={request.get_host()},
                require_https=request.is_secure(),
            ):
                return redirect(next_url)
            return redirect("library-home")
        error = "Invalid credentials or disabled account."
    return render(
        request,
        "core/login.html",
        {"next": next_url, "error": error},
        status=401 if error else 200,
    )


@require_POST
def library_logout(request: HttpRequest) -> HttpResponseBase:
    logout(request)
    return redirect("library-login")


@require_GET
def prometheus_metrics(request: HttpRequest) -> HttpResponseBase:
    if not django_settings.BOOK_SCANNER_METRICS_ENABLED:
        return HttpResponseNotFound()
    expected_token = django_settings.BOOK_SCANNER_METRICS_BEARER_TOKEN
    if expected_token:
        authorization = request.headers.get("Authorization", "")
        supplied_token = authorization.removeprefix("Bearer ") if authorization else ""
        if not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
            response = HttpResponseForbidden("invalid metrics credentials")
            response["WWW-Authenticate"] = "Bearer"
            return response

    registry = CollectorRegistry(auto_describe=True)
    registry.register(PersistentStateCollector())
    return HttpResponse(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)


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
        "auto_skipped": scantailor.is_auto_skipped_split(state),
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
        "capture_layout": job.capture_layout,
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


_CAPTURE_LAYOUT_ALIASES: Final[dict[str, str]] = {
    "single_page": ScanJob.CaptureLayout.SINGLE_PAGE,
    "single-page": ScanJob.CaptureLayout.SINGLE_PAGE,
    "single": ScanJob.CaptureLayout.SINGLE_PAGE,
    "spread": ScanJob.CaptureLayout.SPREAD,
    "spreads": ScanJob.CaptureLayout.SPREAD,
}


def _resolve_capture_layout(raw: Any, scanner_id: str) -> str | None:
    """Capture layout for a new scan job; ``None`` for an unrecognised value (#55).

    An explicit ``capture_layout`` wins. Otherwise a scanner listed in
    ``BOOK_SCANNER_SCAN_SPREAD_SCANNER_IDS`` gets ``spread`` and everything else the
    configured default (``single_page``: one page per image). Page ``side`` values do
    not imply spreads: a two-camera rig sends left and right as separate images.
    """
    if raw is not None and str(raw).strip():
        return _CAPTURE_LAYOUT_ALIASES.get(str(raw).strip().lower())
    if scanner_id and scanner_id in django_settings.BOOK_SCANNER_SCAN_SPREAD_SCANNER_IDS:
        return str(ScanJob.CaptureLayout.SPREAD)
    return str(django_settings.BOOK_SCANNER_SCAN_DEFAULT_CAPTURE_LAYOUT)


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


def _scan_page_thumbnail_url(page: ScanPage) -> str:
    return f"/scan/pages/{page.id}/image/"


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
    """The artifact a page thumbnail is rendered from, plus its cache identity."""

    storage: StorageBackend
    identifier: str
    cache_key: str


def _latest_completed_stage_state(page: ScanPage) -> ScanTailorStageState | None:
    """Last stage of the page's unbroken run of successful stages that has an artifact.

    Walks ``stage_order()`` from the first stage and stops at the first stage that
    is not SUCCESS, so an output whose upstream was edited/invalidated or is
    re-running is never shown as current (#58).
    """
    states = {
        state.stage: state
        for state in ScanTailorStageState.objects.filter(
            scan_job_id=page.scan_job_id,
            scan_page=page,
        ).select_related("preview_storage_location", "output_storage_location")
    }
    latest: ScanTailorStageState | None = None
    for stage_name in scantailor.stage_order():
        state = states.get(stage_name)
        if state is None or state.status != ScanTailorStageState.StageStatus.SUCCESS:
            break
        if scantailor.is_auto_skipped_split(state):
            # A "no split" passthrough shows the raw page; its full-size version
            # makes the better thumbnail than the reduced stage preview (#55).
            continue
        if state.preview_storage_identifier or state.output_storage_identifier:
            latest = state
    return latest


def _scan_thumbnail_source(page: ScanPage) -> _ThumbnailSource | None:
    """Thumbnail source: latest completed stage output, else the raw active version."""
    state = _latest_completed_stage_state(page)
    if state is not None:
        if state.preview_storage_identifier:
            identifier = state.preview_storage_identifier
            location = state.preview_storage_location
        else:
            identifier = state.output_storage_identifier
            location = state.output_storage_location
        return _ThumbnailSource(
            storage=(
                backend_for_location(location) if location is not None else _default_scan_storage()
            ),
            identifier=identifier,
            cache_key=(
                f"stage-{state.id}-{state.stage}-r{state.revision}-{state.output_sha256[:16]}"
            ),
        )
    version = page.active_version()
    if version is None:
        return None
    return _ThumbnailSource(
        storage=(
            backend_for_location(version.storage_location)
            if version.storage_location_id is not None
            else _default_scan_storage()
        ),
        identifier=version.storage_identifier,
        cache_key=f"{version.id}-{version.checksum_sha256[:32]}",
    )


def _scan_thumbnail_cache_path(source: _ThumbnailSource) -> Path:
    return (
        Path(django_settings.APP_DATA_DIR)
        / SCANNING_STORAGE_ROOT
        / "thumbnails"
        / f"{source.cache_key}-{SCAN_THUMBNAIL_MAX_EDGE}.jpg"
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


@dataclass(frozen=True)
class DependencyResult:
    name: str
    status: str
    details: dict[str, Any] | None = None


def _status_payload() -> list[dict[str, Any]]:
    results = [
        _check_database(),
        _check_storage(),
        _check_queue(),
    ]
    return [dep.__dict__ for dep in results]


def _check_database() -> DependencyResult:
    try:
        connection = connections["default"]
        connection.connect()
        connection.close()
        return DependencyResult(name="database", status="ok")
    except Exception as exc:  # pragma: no cover - transport-specific failures
        return DependencyResult(name="database", status="down", details={"error": str(exc)})


def _check_storage() -> DependencyResult:
    try:
        storage_root = Path(django_settings.APP_DATA_DIR)
        if not getattr(django_settings, "STORAGE_REQUIRE_EXISTING_ROOT", False):
            storage_root.mkdir(parents=True, exist_ok=True)
        check_storage_root(root=storage_root)
        probe_storage_writable(storage_root)
        details: dict[str, Any] = {"root": str(storage_root)}
        available = free_bytes(storage_root)
        reserve = min_free_bytes()
        if available is not None:
            details["free_bytes"] = available
            details["min_free_bytes"] = reserve
        if available is not None and reserve > 0 and available < reserve:
            # Imports refuse to archive below the reserve; the app itself still serves.
            details["warning"] = "free space below BOOK_SCANNER_STORAGE_MIN_FREE_BYTES"
            return DependencyResult(name="storage", status="degraded", details=details)
        return DependencyResult(name="storage", status="ok", details=details)
    except Exception as exc:  # pragma: no cover
        return DependencyResult(name="storage", status="down", details={"error": str(exc)})


def _check_queue() -> DependencyResult:
    queue_url = getattr(django_settings, "BOOK_SCANNER_QUEUE_URL", None)
    if not queue_url:
        return DependencyResult(
            name="queue",
            status="disabled",
            details={"reason": "not configured"},
        )
    if queue_url.startswith("inline://"):
        return DependencyResult(
            name="queue",
            status="ok",
            details={"mode": "inline development queue"},
        )
    client = redis.Redis.from_url(queue_url)
    try:
        client.ping()
        return DependencyResult(name="queue", status="ok")
    except Exception as exc:  # pragma: no cover - infrastructure-specific failures
        return DependencyResult(name="queue", status="down", details={"error": str(exc)})


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


def _can_manage_review_task(profile: AppUserProfile, task: ReviewTask) -> bool:
    if profile.role == "admin":
        return True
    import_job = task.import_job
    if import_job is not None:
        return import_job.uploader_id == profile.user_id
    scan_job = task.scan_job
    if scan_job is not None:
        return scan_job.operator_id == profile.user_id
    return False


def _review_owner_id(task: ReviewTask) -> int | None:
    import_job = task.import_job
    if import_job is not None:
        return import_job.uploader_id
    scan_job = task.scan_job
    if scan_job is not None:
        return scan_job.operator_id
    return None


def _note_review_redecided(task: ReviewTask, profile: AppUserProfile) -> None:
    """A resolved review decided again: the earlier decision no longer stands (#27)."""
    contributions.record_review_reversal(
        task,
        source=ReviewDecisionCorrection.Source.RE_RESOLVED,
        found_by=profile.user,
    )


def _review_candidates(task: ReviewTask) -> Any:
    """Current candidates only; superseded runs are history, not acceptable choices."""
    return metadata_search.current_candidates(task)


def _review_search_context(task: ReviewTask) -> dict[str, Any]:
    """Recorded search runs plus the live/failed state of the latest search job."""
    searches = metadata_search.searches_for_task(task)
    active_job = metadata_search.active_search_job(task)
    latest_job = metadata_search.latest_search_job(task)
    # A discarded run (superseded, or finished after the review was resolved) is
    # history only; the latest search shown is the latest one that was applied.
    applied = [search for search in searches if search.state != MetadataSearch.State.DISCARDED]
    latest_search = applied[0] if applied else None
    current_ids = set(_review_candidates(task).values_list("id", flat=True))
    # A failed job that recorded no run (dispatch failure, stale worker) must still
    # be visible; one that did record a run is shown through that run.
    failed_job = None
    if (
        active_job is None
        and latest_job is not None
        and latest_job.state != ProcessingJob.JobState.SUCCESS
        and not any(search.processing_job_id == latest_job.job_id for search in searches)
    ):
        failed_job = latest_job
    stale_job = (
        latest_job is not None
        and active_job is None
        and latest_job.state in (metadata_search.ACTIVE_JOB_STATES)
    )
    return {
        "searches": searches,
        "latest_search": latest_search,
        "earlier_searches": [search for search in searches if search is not latest_search],
        "active_search_job": active_job,
        "search_running": (
            active_job is not None and active_job.state == ProcessingJob.JobState.RUNNING
        ),
        "failed_search_job": failed_job,
        "failed_search_error": (
            "the search did not finish (worker stopped or queue entry lost)"
            if stale_job
            else str((failed_job.details or {}).get("error") or "")
            if failed_job is not None
            else ""
        ),
        "latest_search_has_current_candidates": (
            latest_search is not None
            and any(candidate.id in current_ids for candidate in latest_search.candidates.all())
        ),
        "isbn_lookup": metadata_search.isbn_lookup_result(latest_search, current_ids),
    }


def _serialize_task(task: ReviewTask) -> dict[str, Any]:
    source_type = "import" if task.import_job_id is not None else "scan"
    source_id = task.import_job_id or task.scan_job_id
    return {
        "id": str(task.id),
        "state": task.state,
        "decision": task.decision,
        "task_type": task.task_type,
        "source_filename": task.source_filename,
        "source_path_hint": task.source_path_hint,
        "inferred_title": task.inferred_title,
        "inferred_authors": task.inferred_authors,
        "embedded_metadata": task.embedded_metadata,
        "provider_snapshot": task.provider_snapshot,
        "source_type": source_type,
        "source_id": str(source_id) if source_id is not None else None,
        "uploader_id": _review_owner_id(task),
        "reviewer_id": task.reviewer_id,
        "resolved_book_id": str(task.resolved_book_id) if task.resolved_book_id else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


# Moved to review_accept so workers resolve reviews through the same path (#59).
_resolve_review_source = review_accept.resolve_review_source


def _serialize_candidate(candidate: MetadataCandidate) -> dict[str, Any]:
    return {
        "id": str(candidate.id),
        "provider_id": candidate.provider_id,
        "title": candidate.title,
        "authors": candidate.authors,
        "isbn10": candidate.isbn10,
        "isbn13": candidate.isbn13,
        "score": float(candidate.score),
        "language": candidate.language,
        "publisher": candidate.publisher,
        "publication_year": candidate.publication_year,
        "description": candidate.description,
        "subjects": candidate.subjects,
        "state": candidate.state,
    }


def _review_action_response(
    request: HttpRequest,
    payload: dict[str, Any],
    *,
    task_id: int | None = None,
) -> HttpResponseBase:
    if _wants_html(request):
        if task_id is not None:
            return redirect("review-detail", task_id=task_id)
        return redirect("review-queue")
    return JsonResponse(payload)


def _parse_author_list(raw_authors: Any) -> list[str]:
    """Authors from a comma-separated form value or a JSON list."""
    if isinstance(raw_authors, str):
        return [entry.strip() for entry in raw_authors.split(",") if entry.strip()]
    if isinstance(raw_authors, list):
        return [str(name).strip() for name in raw_authors if str(name).strip()]
    return []


_AUTO_ACCEPT_BLOCK_MESSAGES = {
    "no_provider_authors": (
        "The best match scored high enough to be accepted automatically, but the "
        "metadata provider lists no authors for it. Confirm or edit the suggested "
        "authors from the file before accepting it."
    ),
    "author_conflict": (
        "The best match scored high enough to be accepted automatically, but its "
        "authors differ from the authors in the file's own metadata."
    ),
    "stronger_title_evidence_disagrees": (
        "The best match scored high enough to be accepted automatically, but its "
        "title matches only a weaker hint (such as the filename) while the file's "
        "stronger title evidence names another book."
    ),
    "llm_derived": (
        "The best match was found through search terms suggested by the local AI "
        "server. AI suggestions are unverified, so a person must confirm the match."
    ),
    "ambiguous_tie": (
        "Several different books tied for the best score, so none was accepted "
        "automatically. Pick the right one below."
    ),
    "edition_conflict": (
        "The best match scored high enough to be accepted automatically, but it "
        "describes another edition than the file: its ISBN, language or publisher "
        "does not match the file's own metadata."
    ),
}


def _auto_accept_block(task: ReviewTask) -> dict[str, Any] | None:
    """Why an otherwise auto-accepted import was routed to review, for the review page."""
    evidence = task.embedded_metadata if isinstance(task.embedded_metadata, dict) else {}
    reason = evidence.get(intake.AUTO_ACCEPT_BLOCKED_KEY)
    if not isinstance(reason, str) or not reason:
        return None
    details = evidence.get(intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY)
    return {
        "reason": reason,
        "message": _AUTO_ACCEPT_BLOCK_MESSAGES.get(
            reason, "The best match was not accepted automatically."
        ),
        "details": details if isinstance(details, dict) else {},
        "suggested_authors": _parse_author_list(evidence.get(intake.SUGGESTED_AUTHORS_KEY)),
    }


def _local_suggestion(
    task: ReviewTask, candidates: list[MetadataCandidate]
) -> dict[str, Any] | None:
    """The file's local-consensus suggestion (``local_identity``), for the review page.

    ``confirmed_candidate`` is the current provider candidate the suggestion confirms;
    without one the suggestion pre-fills manual metadata. Never accepted by itself.
    """
    evidence = task.embedded_metadata if isinstance(task.embedded_metadata, dict) else {}
    local = evidence.get(local_identity.LOCAL_IDENTITY_KEY)
    if not isinstance(local, dict) or not isinstance(local.get("title"), str):
        return None
    confirms = local.get("confirms") if isinstance(local.get("confirms"), dict) else None
    confirmed = None
    if confirms is not None:
        confirmed = next(
            (
                candidate
                for candidate in candidates
                if candidate.provider_id == confirms.get("provider_id")
                and candidate.provider_ref == confirms.get("provider_ref")
            ),
            None,
        )
    outage = local.get("provider_outage")
    origins = local.get("title_origins")
    return {
        "title": local["title"],
        "authors": _parse_author_list(local.get("authors")),
        "language": local.get("language") if isinstance(local.get("language"), str) else "",
        "title_origins": [o for o in origins if isinstance(o, str)]
        if isinstance(origins, list)
        else [],
        "author_source": local.get("author_source"),
        "provider_outage": [p for p in outage if isinstance(p, str)]
        if isinstance(outage, list)
        else [],
        "confirmed_candidate": confirmed,
        "confirmed_candidate_id": confirmed.pk if confirmed is not None else None,
    }


def _reviewer_genre_choice(request: HttpRequest, payload: dict[str, Any]) -> list[Genre] | None:
    """Genres a reviewer picked, or ``None`` when they made no choice.

    JSON clients send ``genre_ids`` (a list, possibly empty). The review form sends
    ``genres_reviewed=1`` plus one ``genre_ids`` checkbox value per chosen genre, so
    unticking every suggestion is an explicit "no genres". Raises ``ValueError`` for
    ids that are not existing genres.
    """
    if isinstance(payload.get("genre_ids"), list):
        raw_ids: list[Any] = payload["genre_ids"]
    elif payload.get("genres_reviewed"):
        raw_ids = request.POST.getlist("genre_ids")
    else:
        return None
    ids: list[int] = []
    for raw in raw_ids:
        value = _coerce_int(raw, minimum=1)
        if value is None:
            raise ValueError("invalid genre id")
        if value not in ids:
            ids.append(value)
    found = {genre.pk: genre for genre in Genre.objects.filter(pk__in=ids)}
    if len(found) != len(ids):
        raise ValueError("unknown genre")
    return [found[genre_id] for genre_id in ids]


_apply_review_genres = review_accept.apply_review_genres


def _attach_genre_suggestions(task: ReviewTask, candidates: list[MetadataCandidate]) -> None:
    """Set ``candidate.genre_suggestion`` for the review page's genre picker."""
    index = genres.build_index()
    embedded = genres.embedded_subjects(task.embedded_metadata)
    for candidate in candidates:
        candidate.genre_suggestion = genres.suggest(  # type: ignore[attr-defined]
            candidate.subjects, embedded, index
        )


def _serialize_genre_suggestion(suggestion: genres.GenreSuggestion) -> dict[str, Any]:
    return {
        "source": suggestion.source,
        "genre_ids": suggestion.genre_ids,
        "tags": [
            {
                "tag": match.tag,
                "genre_id": match.genre.pk if match.genre else None,
                "genre": match.genre.name if match.genre else None,
            }
            for match in suggestion.matches
        ],
    }


def _build_manual_book_from_payload(payload: dict[str, Any]) -> Book:
    from . import identification

    raw_title = payload.get("title")
    if not isinstance(raw_title, str):
        raise ValueError("title required")
    title = raw_title.strip()
    if not title:
        raise ValueError("title required")

    authors = _parse_author_list(payload.get("authors", []))

    publication_year: int | None = None
    raw_year = payload.get("publication_year")
    if isinstance(raw_year, int) and raw_year > 0:
        publication_year = raw_year
    elif isinstance(raw_year, str) and raw_year.strip().isdigit():
        publication_year = int(raw_year.strip())

    isbn10: str | None = None
    raw_isbn10 = payload.get("isbn10")
    if isinstance(raw_isbn10, str) and raw_isbn10.strip():
        normalized_isbns = identification.normalize_isbns(raw_isbn10)
        isbn10 = next((value for value in normalized_isbns if len(value) == 10), None)

    isbn13: str | None = None
    raw_isbn13 = payload.get("isbn13")
    if isinstance(raw_isbn13, str) and raw_isbn13.strip():
        normalized_isbns = identification.normalize_isbns(raw_isbn13)
        isbn13 = next((value for value in normalized_isbns if len(value) == 13), None)

    return intake._provision_book_from_manual_metadata(
        title=title,
        authors=authors,
        publisher=str(payload.get("publisher", "") or ""),
        language=str(payload.get("language", "") or ""),
        description=str(payload.get("description", "") or ""),
        edition=str(payload.get("edition", "") or ""),
        publication_year=publication_year,
        isbn10=isbn10,
        isbn13=isbn13,
    )


def _require_role(
    required_role: str,
) -> Callable[
    [Callable[[HttpRequest], HttpResponse]],
    Callable[[HttpRequest], HttpResponse],
]:
    def decorator(
        view_fn: Callable[[HttpRequest], HttpResponse],
    ) -> Callable[[HttpRequest], HttpResponse]:
        @wraps(view_fn)
        def wrapped(request: HttpRequest) -> HttpResponse:
            profile = _profile_for_request(request)
            if profile is None or profile.is_disabled:
                if request.user.is_authenticated:
                    logout(request)
                return HttpResponse("forbidden", status=403)
            if profile.role != required_role:
                return HttpResponseForbidden("forbidden")
            return view_fn(request)

        return wrapped

    return decorator


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    dependencies = _status_payload()
    any_down = any(item["status"] == "down" for item in dependencies)
    status_code = 200 if not any_down else 503
    return JsonResponse(
        {
            "status": "ok" if not any_down else "degraded",
            "dependencies": dependencies,
        },
        status=status_code,
    )


@require_GET
def readiness(request: HttpRequest) -> JsonResponse:
    dependencies = _status_payload()
    any_down = any(item["status"] == "down" for item in dependencies)
    if any_down:
        return JsonResponse({"status": "not_ready", "dependencies": dependencies}, status=503)
    return JsonResponse({"status": "ready", "dependencies": dependencies})


@require_GET
@login_required
def library_home(request: HttpRequest) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None:
        return JsonResponse({"detail": "profile_missing"}, status=404)
    if profile.is_disabled:
        return JsonResponse({"detail": "forbidden"}, status=403)
    if _wants_html(request):
        started = time.perf_counter()
        context = _library_landing_context()
        logger.info(
            "library_landing",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return render(request, "core/library_home.html", {"profile": profile, **context})
    return JsonResponse(
        {"status": "ok", "user": request.user.username, "role": profile.role},
    )


SHELF_SIZE: Final = 12
# Facets of the landing page sidebar, in display order, with their headings.
LANDING_FACETS: Final[tuple[tuple[str, str], ...]] = (
    ("format", "Format"),
    ("language", "Language"),
    ("genre", "Genre"),
    ("category", "Category"),
    ("author", "Author"),
    ("decade", "Decade"),
    ("has_cover", "Cover"),
)
LANDING_FACET_ITEMS: Final = 8
# Query parameter each facet's links set on `/library/books/`.
FACET_FILTER_PARAMS: Final[dict[str, str]] = {
    "format": "format",
    "language": "language",
    "genre": "genre_id",
    "category": "category_id",
    "author": "author_id",
    "decade": "decade",
    "has_cover": "has_cover",
}


def _facet_label(facet: str, item: search.FacetCount) -> str:
    if facet == "format":
        return item.value.upper()
    if facet == "language":
        return item.value.upper() if len(item.value) <= 3 else item.value
    if facet == "decade":
        return f"{item.value}s"
    if facet == "has_cover":
        return "With cover" if item.value == "true" else "Without cover"
    return item.value


def _facet_links(facets: dict[str, list[search.FacetCount]]) -> list[dict[str, Any]]:
    """Sidebar groups for the template; facets without values are hidden (#50)."""
    groups = []
    for facet, heading in LANDING_FACETS:
        items = facets.get(facet, [])[:LANDING_FACET_ITEMS]
        if not items:
            continue
        param = FACET_FILTER_PARAMS[facet]
        groups.append(
            {
                "name": facet,
                "heading": heading,
                "items": [
                    {
                        "label": _facet_label(facet, item),
                        "count": item.count,
                        "query": urlencode({"view": "html", param: item.filter_key}),
                    }
                    for item in items
                ],
            }
        )
    return groups


def _language_shelf_ids(
    languages: list[search.FacetCount],
) -> list[tuple[str, str, list[int]]]:
    """ "New in <language>" shelves, cached with the global facets.

    The most common language is skipped: its shelf would mostly repeat "Recently
    added". The next two get a shelf when the library has more than one language.
    """
    cached = cache.get(search.LANGUAGE_SHELVES_CACHE_KEY)
    if cached is not None:
        return list(cached)
    shelves = [
        (item.value, item.filter_key, search.newest_book_ids(limit=SHELF_SIZE, language=item.value))
        for item in languages[1:3]
    ]
    cache.set(search.LANGUAGE_SHELVES_CACHE_KEY, shelves, search.GLOBAL_CACHE_SECONDS)
    return shelves


def _shelf_books(book_ids: list[int]) -> dict[int, Book]:
    """Shelf cards need title, authors and the cover thumbnail: two queries in all."""
    if not book_ids:
        return {}
    # Raw-scan books (#127) never appear on a shelf, whatever the id source.
    return thumbnails_module.with_thumbnail_refs(
        library_visibility.visible_books().prefetch_related("authors")
    ).in_bulk(book_ids)


def _library_landing_context() -> dict[str, Any]:
    """Search-first landing page (#28 Q1): search box, global facets, shelves, indexes.

    Global facet counts, the total and the language shelves come from a
    five-minute cache; "Recently added" and "Recently identified" are live.
    """
    facets = search.global_facets([facet for facet, _heading in LANDING_FACETS])
    recent_ids = search.newest_book_ids(limit=SHELF_SIZE)
    identified_ids = search.recently_identified_book_ids(limit=SHELF_SIZE)
    language_shelves = _language_shelf_ids(facets.get("language", []))
    books = _shelf_books(
        list(
            dict.fromkeys(
                [
                    *recent_ids,
                    *identified_ids,
                    *(i for _l, _k, ids in language_shelves for i in ids),
                ]
            )
        )
    )

    def cards(ids: list[int]) -> list[dict[str, Any]]:
        return [_shelf_card(books[book_id]) for book_id in ids if book_id in books]

    shelves: list[dict[str, Any]] = [
        {
            "id": "recently-added",
            "title": "Recently added",
            "books": cards(recent_ids),
            "see_all": urlencode({"view": "html", "sort": "-created_at"}),
        },
        {
            "id": "recently-identified",
            "title": "Recently identified",
            "books": cards(identified_ids),
            "see_all": None,
        },
    ]
    for value, key, ids in language_shelves:
        shelves.append(
            {
                "id": f"new-in-{key}",
                "title": f"New in {value.upper() if len(value) <= 3 else value}",
                "books": cards(ids),
                "see_all": urlencode({"view": "html", "language": key, "sort": "-created_at"}),
            }
        )
    return {
        "book_count": (total := search.total_books()),
        "book_count_display": f"{total:,}",
        "facet_groups": _facet_links(facets),
        "shelves": [shelf for shelf in shelves if shelf["books"]],
        "letters": AUTHOR_INDEX_LETTERS,
    }


def _shelf_card(book: Book) -> dict[str, Any]:
    return {
        "id": book.id,
        "title": book.title,
        "authors": [author.full_name for author in book.authors.all()],
        "year": book.published_year,
        "cover_image": _cover_image_payload(book),
    }


def _cover_image_payload(book: Book) -> dict[str, Any] | None:
    """What an `<img>` for the front cover needs: thumbnail srcset or the full cover.

    Thumbnails come from the `with_thumbnail_refs` annotation of the list query,
    so rendering never queries per book. Without a current thumbnail (not yet
    generated, or the cover could not be decoded) the full cover is used.
    """
    if not book.front_cover_id:
        return None
    refs = thumbnails_module.annotated_thumbnails(book) or {}
    medium = refs.get(CoverThumbnail.Size.MEDIUM)
    small = refs.get(CoverThumbnail.Size.SMALL)
    primary = medium or small
    if primary is None:
        return {
            "src": f"/library/books/{book.id}/covers/front/",
            "srcset": "",
            "width": 240,
            "height": 320,
            "thumbnail": False,
        }
    return {
        "src": primary.url,
        "small_src": (small or primary).url,
        "srcset": ", ".join(f"{ref.url} {ref.width}w" for ref in (small, medium) if ref),
        "width": primary.width,
        "height": primary.height,
        "thumbnail": True,
    }


AUTHOR_INDEX_LETTERS: Final = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
AUTHOR_INDEX_PAGE_SIZE: Final = 60


@require_GET
@login_required
def library_authors(request: HttpRequest) -> HttpResponseBase:
    """Author A-Z index (#28 Q1): authors with books, keyset-paginated by name."""
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    letter = request.GET.get("letter", "").strip()[:1].upper()
    if letter and letter not in AUTHOR_INDEX_LETTERS:
        letter = ""
    page_size = min(_as_positive_int(request.GET.get("page_size"), AUTHOR_INDEX_PAGE_SIZE), 200)
    try:
        page = search.author_index_page(
            letter=letter,
            cursor=request.GET.get("cursor", "").strip(),
            page_size=page_size,
        )
    except search.InvalidCursorError:
        return JsonResponse({"detail": "invalid_cursor"}, status=400)

    def page_url(cursor: str | None) -> str | None:
        if not cursor:
            return None
        params = {"cursor": cursor}
        if _wants_html(request):
            params["view"] = "html"
        if "page_size" in request.GET:
            params["page_size"] = str(page_size)
        return f"?{urlencode(params)}"

    if _wants_html(request):
        return render(
            request,
            "core/library_authors.html",
            {
                "authors": page.authors,
                "letter": letter,
                "letters": AUTHOR_INDEX_LETTERS,
                "next_url": page_url(page.next_cursor),
                "previous_url": page_url(page.previous_cursor),
            },
        )
    return JsonResponse(
        {
            "items": [
                {"id": entry.id, "name": entry.name, "book_count": entry.book_count}
                for entry in page.authors
            ],
            "next_cursor": page.next_cursor,
            "previous_cursor": page.previous_cursor,
        }
    )


_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")


@require_GET
@login_required
def library_cover_thumbnail(request: HttpRequest, sha256: str, size: str) -> HttpResponseBase:
    """A content-addressed cover thumbnail, cacheable forever by the member's browser.

    The permission check always runs here. With
    `BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX` set, nginx sends the bytes
    (`X-Accel-Redirect` to an `internal` location) instead of a Python worker.
    """
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    if size not in thumbnails_module.THUMBNAIL_WIDTHS or not _SHA256_RE.fullmatch(sha256):
        return HttpResponseNotFound()
    thumbnail = (
        CoverThumbnail.objects.select_related("storage_location")
        .filter(sha256=sha256, size=size)
        .first()
    )
    if thumbnail is None:
        return HttpResponseNotFound("thumbnail not available")

    etag = f'"{thumbnail.sha256}"'
    headers = {
        "Cache-Control": thumbnails_module.IMMUTABLE_CACHE_CONTROL,
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if etag in request.headers.get("If-None-Match", ""):
        not_modified = HttpResponse(status=304)
        for name, value in headers.items():
            not_modified[name] = value
        return not_modified

    accel_prefix = str(
        getattr(django_settings, "BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX", "") or ""
    )
    response: HttpResponseBase
    if accel_prefix:
        response = HttpResponse(content_type=thumbnail.mime_type)
        response["X-Accel-Redirect"] = f"{accel_prefix.rstrip('/')}/{thumbnail.storage_identifier}"
    else:
        try:
            storage = backend_for_location(thumbnail.storage_location)
            stream = storage.open(thumbnail.storage_identifier)
        except StorageConfigurationError:
            return HttpResponse("thumbnail storage unavailable", status=503)
        except FileNotFoundError:
            return HttpResponseNotFound("thumbnail missing")
        except OSError:
            return HttpResponse("storage unavailable", status=503)
        response = FileResponse(stream, as_attachment=False, content_type=thumbnail.mime_type)
        response["Content-Length"] = str(thumbnail.size_bytes)
    for name, value in headers.items():
        response[name] = value
    return response


# Attribute that `_book_list_prefetches` fills with the book's COMPLETE variants.
_COMPLETE_VARIANTS_ATTR = "complete_file_variants"


def _book_list_prefetches() -> list[str | Prefetch[Any]]:
    """Prefetches that let `_serialize_book` render a list row without extra queries.

    The COMPLETE variants are prefetched once for the whole page into
    `_COMPLETE_VARIANTS_ATTR`, so `formats_available` is not a per-book query (#28).
    """
    return [
        "authors",
        "categories",
        "genres",
        Prefetch(
            "file_variants",
            queryset=library_visibility.without_pending_scan_variants(
                FileVariant.objects.filter(
                    processing_state=FileVariant.ProcessingState.COMPLETE,
                )
            ).only("id", "book_id", "format", "processing_state"),
            to_attr=_COMPLETE_VARIANTS_ATTR,
        ),
    ]


def _complete_variants(book: Book) -> list[FileVariant]:
    prefetched: list[FileVariant] | None = getattr(book, _COMPLETE_VARIANTS_ATTR, None)
    if prefetched is not None:
        return prefetched
    # `.all()` reuses a plain `file_variants` prefetch when there is one; a
    # `.filter()` here would bypass it and cost one query per book (#28).
    variants = list(book.file_variants.all())
    hidden = _pending_scan_variant_ids(book, variants)
    return [
        variant
        for variant in variants
        if variant.processing_state == FileVariant.ProcessingState.COMPLETE
        and variant.id not in hidden
    ]


def _pending_scan_variant_ids(book: Book, variants: list[FileVariant]) -> set[int]:
    """Cleaned variants of ``book`` built by a scan job that is not finished (#127)."""
    cleaned = {
        variant.id for variant in variants if variant.source_type == FileVariant.SourceType.CLEANED
    }
    if not cleaned:
        return set()
    shown = library_visibility.without_pending_scan_variants(
        FileVariant.objects.filter(pk__in=cleaned)
    ).values_list("id", flat=True)
    return cleaned - set(shown)


def _serialize_book(
    book: Book,
    *,
    include_variants: bool = False,
    include_conversion_options: bool = False,
) -> dict[str, Any]:
    book_payload: dict[str, Any] = {
        "id": book.id,
        "title": book.title,
        "subtitle": book.subtitle,
        "publisher": book.publisher,
        "edition": book.edition,
        "language": book.language,
        "description": book.description,
        # Year-only data (the usual case) leaves publication_date null (#38).
        "publication_year": book.published_year,
        "publication_date": book.publication_date.isoformat() if book.publication_date else None,
        "authors": [author.full_name for author in book.authors.all()],
        "categories": [category.name for category in book.categories.all()],
        "genres": [genre.name for genre in book.genres.all()],
        "created_at": book.created_at.isoformat(),
        "updated_at": book.updated_at.isoformat(),
        "cover_front": None,
        "cover_back": None,
    }

    if book.front_cover_id and book.front_cover is not None:
        book_payload["cover_front"] = _serialize_cover(book.front_cover)
        refs = thumbnails_module.annotated_thumbnails(book)
        if refs is not None:  # list queries annotate the current thumbnails (#28)
            book_payload["cover_front"]["thumbnails"] = {
                size: ref.url for size, ref in refs.items()
            }
    if book.back_cover_id and book.back_cover is not None:
        book_payload["cover_back"] = _serialize_cover(book.back_cover)
    if include_variants:
        file_variants = sorted(
            _complete_variants(book),
            # Complete representations first, then the parts of a split work in order.
            key=lambda variant: (
                variant.part_index is not None,
                variant.part_index or 0,
                variant.id,
            ),
        )
        variants = [
            {
                "id": file_variant.id,
                "format": file_variant.format,
                "mime_type": file_variant.mime_type,
                "size_bytes": file_variant.size_bytes,
                "original_filename": file_variant.original_filename,
                "source_type": file_variant.source_type,
                "imported_at": (
                    file_variant.imported_at.isoformat() if file_variant.imported_at else None
                ),
                "part_index": file_variant.part_index,
                "part_label": file_variant.part_label,
            }
            for file_variant in file_variants
        ]
        book_payload["variants"] = variants
        book_payload["formats_available"] = sorted(
            {file_variant.format for file_variant in file_variants}
        )
    else:
        book_payload["formats_available"] = sorted(
            {variant.format for variant in _complete_variants(book)}
        )
    if include_conversion_options:
        book_payload["conversion_options"] = conversion.conversion_options_for_book(book)
    return book_payload


def _serialize_cover(cover: Any) -> dict[str, Any]:
    if cover is None:
        return {}
    return {
        "id": cover.id,
        "position": cover.position,
        "mime_type": cover.mime_type,
        "size_bytes": cover.size_bytes,
        "source_variant_id": cover.source_variant_id,
        "extractor_tool": cover.extractor_tool,
        "extractor_version": cover.extractor_version,
        "url": f"/library/books/{cover.book_id}/covers/{cover.position}/",
    }


_LIBRARY_FILTER_PARAMS: Final = (
    ("q", "query"),
    ("title", "title"),
    ("author", "author"),
    ("category", "category"),
    ("genre", "genre"),
    ("language", "language"),
    ("format", "book_format"),
)


def _optional_id(raw: str | None) -> int | None:
    value = (raw or "").strip()
    return int(value) if value.isdigit() and 0 < len(value) <= 18 else None


def _optional_decade(raw: str | None) -> int | None:
    value = (raw or "").strip().removesuffix("s")
    if not value.isdigit() or len(value) > 4:
        return None
    return (int(value) // 10) * 10


def _optional_flag(raw: str | None) -> bool | None:
    value = (raw or "").strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return None


def _active_filter_chips(
    request: HttpRequest, search_request: search.SearchRequest
) -> list[dict[str, str]]:
    """Removable labels for the filters in effect (one name query per entity filter)."""
    chips: list[tuple[str, str, str]] = []
    for param, label, value in (
        ("q", "Search", search_request.query),
        ("title", "Title", search_request.title),
        ("author", "Author", search_request.author),
        ("category", "Category", search_request.category),
        ("genre", "Genre", search_request.genre),
        ("language", "Language", search_request.language),
        ("format", "Format", search_request.book_format.upper()),
    ):
        if value:
            chips.append((param, label, f"{label}: {value}"))
    for param, label, model, field_name, entity_id in (
        ("author_id", "Author", Author, "full_name", search_request.author_id),
        ("genre_id", "Genre", Genre, "name", search_request.genre_id),
        ("category_id", "Category", Category, "name", search_request.category_id),
    ):
        if entity_id is not None:
            name = model.objects.filter(pk=entity_id).values_list(field_name, flat=True).first()
            chips.append((param, label, f"{label}: {name or 'unknown'}"))
    if search_request.decade is not None:
        chips.append(("decade", "Decade", f"Decade: {search_request.decade}s"))
    if search_request.has_cover is not None:
        chips.append(
            ("has_cover", "Cover", "With cover" if search_request.has_cover else "Without cover")
        )
    result = []
    for param, name, text in chips:
        params = request.GET.copy()
        for dropped in (param, "cursor", "page"):
            params.pop(dropped, None)
        result.append({"label": text, "name": name.lower(), "remove_url": f"?{params.urlencode()}"})
    return result


def _library_search_request(request: HttpRequest) -> search.SearchRequest:
    raw_page = request.GET.get("page")
    page_size = min(_as_positive_int(request.GET.get("page_size"), 25), 100)
    filters = {
        attribute: request.GET.get(param, "").strip() for param, attribute in _LIBRARY_FILTER_PARAMS
    }
    return search.SearchRequest(
        query=filters["query"],
        title=filters["title"],
        author=filters["author"],
        category=filters["category"],
        genre=filters["genre"],
        language=filters["language"],
        book_format=filters["book_format"],
        author_id=_optional_id(request.GET.get("author_id")),
        genre_id=_optional_id(request.GET.get("genre_id")),
        category_id=_optional_id(request.GET.get("category_id")),
        decade=_optional_decade(request.GET.get("decade")),
        has_cover=_optional_flag(request.GET.get("has_cover")),
        sort=request.GET.get("sort", "").strip(),
        cursor=request.GET.get("cursor", "").strip(),
        page=_as_positive_int(raw_page, 1) if raw_page is not None else None,
        page_size=page_size,
    )


def _library_books_url(request: HttpRequest, **changes: str) -> str:
    params = request.GET.copy()
    for name in ("cursor", "page"):
        params.pop(name, None)
    for name, value in changes.items():
        params[name] = value
    return f"?{params.urlencode()}"


def _next_page_url(request: HttpRequest, result: search.SearchPage, page_size: int) -> str | None:
    if result.next_cursor:
        return _library_books_url(request, cursor=result.next_cursor)
    if result.page is not None and not result.previous_cursor:
        cap = min(result.count, int(result.extra.get("max_results", result.count)))
        if result.page * page_size < cap:
            return _library_books_url(request, page=str(result.page + 1))
    return None


def _previous_page_url(request: HttpRequest, result: search.SearchPage) -> str | None:
    if result.previous_cursor:
        return _library_books_url(request, cursor=result.previous_cursor)
    if result.page is not None and result.page > 1:
        return _library_books_url(request, page=str(result.page - 1))
    return None


@require_GET
@login_required
def library_books(request: HttpRequest) -> HttpResponseBase:
    """Browse/search the library (#28): keyset cursors for browsing, ranked search by offset.

    `?cursor=` (from `next_cursor`/`previous_cursor`) pages at constant cost at
    any depth. `?page=` still works for one release and is marked deprecated
    for browse sorts. `?facets=language,format,...` adds facet counts.
    """
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    search_request = _library_search_request(request)
    backend = search.get_search_backend()
    started = time.perf_counter()
    try:
        result = backend.search(search_request)
    except search.InvalidCursorError:
        return JsonResponse({"detail": "invalid_cursor"}, status=400)
    books_by_id = thumbnails_module.with_thumbnail_refs(
        library_visibility.visible_books()
        .select_related("front_cover", "back_cover")
        .prefetch_related(*_book_list_prefetches())
    ).in_bulk(result.book_ids)
    serialized = [
        _serialize_book(books_by_id[book_id], include_variants=False)
        for book_id in result.book_ids
        if book_id in books_by_id
    ]
    requested_facets = [
        name.strip()
        for name in request.GET.get("facets", "").split(",")
        if name.strip() in search.FACETS
    ]
    facet_payload = {
        name: [
            {"value": item.value, "count": item.count, "key": item.filter_key} for item in counts
        ]
        for name, counts in (
            backend.facet_counts(search_request, requested_facets).items()
            if requested_facets
            else ()
        )
    }
    # Never log the query text itself (#28 privacy).
    logger.info(
        "library_search",
        backend=result.backend,
        kind="search" if search_request.query else "browse",
        sort=result.sort,
        result_count=result.count,
        fuzzy=result.fuzzy,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    if _wants_html(request):
        for book_payload in serialized:
            book_payload["cover_image"] = _cover_image_payload(books_by_id[book_payload["id"]])
        return render(
            request,
            "core/library_books.html",
            {
                "books": serialized,
                "active_filters": _active_filter_chips(request, search_request),
                "hidden_filters": [
                    (param, request.GET[param])
                    for param in ("author_id", "genre_id", "category_id", "decade", "has_cover")
                    if request.GET.get(param, "").strip()
                ],
                "count": result.count,
                "fuzzy": result.fuzzy,
                "page_number": result.page,
                "next_url": _next_page_url(request, result, search_request.page_size),
                "previous_url": _previous_page_url(request, result),
                "filters": {
                    "q": search_request.query,
                    "title": search_request.title,
                    "author": search_request.author,
                    "category": search_request.category,
                    "genre": search_request.genre,
                    "language": search_request.language,
                    "format": search_request.book_format,
                    "sort": result.sort,
                },
                "all_genres": list(Genre.objects.order_by("name")),
            },
        )
    payload: dict[str, Any] = {
        "count": result.count,
        "page": result.page,
        "page_size": len(serialized),
        "sort": result.sort,
        "next_cursor": result.next_cursor,
        "previous_cursor": result.previous_cursor,
        "fuzzy": result.fuzzy,
        "items": serialized,
    }
    if requested_facets:
        payload["facets"] = facet_payload
    response = JsonResponse(payload)
    if search_request.page is not None and result.sort != search.RELEVANCE:
        # Offset pages over the whole collection give way to cursors (#28).
        response["Deprecation"] = "true"
    return response


@require_GET
@login_required
def library_book_detail(request: HttpRequest, book_id: int) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        book = (
            library_visibility.visible_books()
            .prefetch_related(
                "authors",
                "categories",
                "genres",
                "identifiers",
                "file_variants",
                "front_cover",
                "back_cover",
            )
            .get(pk=book_id)
        )
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    payload = _serialize_book(
        book,
        include_variants=True,
        include_conversion_options=True,
    )
    if book.front_cover is not None:
        payload["cover_front"] = _serialize_cover(book.front_cover)
    if book.back_cover is not None:
        payload["cover_back"] = _serialize_cover(book.back_cover)
    payload["identifiers"] = [
        {"kind": identifier.kind, "value": identifier.value}
        for identifier in book.identifiers.all()
    ]
    payload["combine_options"] = combine.combine_options_for_book(book)
    if _wants_html(request):
        return render(
            request,
            "core/library_book_detail.html",
            {
                "book": book,
                "book_payload": payload,
                "variants": payload.get("variants", []),
                "identifiers": payload["identifiers"],
                "conversion_options": payload.get("conversion_options", []),
                "combine_options": payload["combine_options"],
            },
        )
    return JsonResponse(payload)


@require_GET
@login_required
def library_book_download(
    request: HttpRequest,
    book_id: int,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        book = (
            library_visibility.visible_books()
            .prefetch_related("file_variants__storage_location")
            .get(pk=book_id)
        )
    except Book.DoesNotExist:
        return HttpResponseNotFound()
    servable = library_visibility.library_variants(
        book.file_variants.filter(processing_state=FileVariant.ProcessingState.COMPLETE)
    )

    variant = None
    variant_id = request.GET.get("variant_id")
    requested_format = request.GET.get("format", "").strip()
    if variant_id:
        try:
            variant = servable.get(pk=int(variant_id))
        except (ValueError, FileVariant.DoesNotExist):
            return HttpResponseBadRequest("variant_id invalid")
    elif requested_format:
        variant = servable.filter(format__iexact=requested_format).order_by("id").first()
    if variant is None and requested_format:
        return HttpResponseNotFound("requested format not available")
    if variant is None:
        variant = servable.order_by("id").first()

    if variant is None:
        return HttpResponseNotFound("variant not available")

    try:
        ensure_reference_intact(variant)
    except ReferencedFileError:
        # Indexed-in-place file is missing or changed on its share: never serve
        # different bytes under this record. The variant is flagged for an admin.
        return HttpResponse("file changed or missing on storage", status=409)
    try:
        storage = backend_for_location(variant.storage_location)
    except StorageConfigurationError:
        return HttpResponse("variant storage unavailable", status=503)
    try:
        stream = storage.open(variant.storage_identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("variant missing")
    except OSError:
        return HttpResponse("storage unavailable", status=503)

    response = FileResponse(stream, as_attachment=True, filename=variant.original_filename)
    response["Content-Type"] = variant.mime_type
    response["Content-Length"] = str(variant.size_bytes)
    return response


@require_GET
@login_required
def library_book_cover(
    request: HttpRequest,
    book_id: int,
    position: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    if position not in {"front", "back"}:
        return HttpResponseBadRequest("invalid cover position")
    try:
        cover = CoverImage.objects.select_related("storage_location").get(
            book_id=book_id,
            book__in=library_visibility.visible_books(),
            position=position,
        )
    except CoverImage.DoesNotExist:
        return HttpResponseNotFound("cover not available")

    try:
        storage = backend_for_location(cover.storage_location)
    except StorageConfigurationError:
        return HttpResponse("cover storage unavailable", status=503)
    try:
        stream = storage.open(cover.storage_identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("cover missing")
    except OSError:
        return HttpResponse("storage unavailable", status=503)

    response = FileResponse(stream, as_attachment=False, filename=f"{position}-cover")
    response["Content-Type"] = cover.mime_type
    response["Content-Length"] = str(cover.size_bytes)
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_http_methods(["GET", "POST"])
@login_required
def library_book_delete(request: HttpRequest, book_id: int) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None or not profile.is_admin():
        return HttpResponseForbidden()
    try:
        book = Book.objects.prefetch_related(
            "file_variants__storage_location",
            "cover_images__storage_location",
        ).get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    if request.method == "GET":
        if _wants_html(request):
            return render(request, "core/book_confirm_delete.html", {"book": book})
        return JsonResponse(
            {
                "book_id": book.id,
                "title": book.title,
                "confirmation_required": book.title,
                "artifact_count": book.file_variants.count() + book.cover_images.count(),
            },
        )

    payload = _request_payload(request)
    if payload.get("confirm_title") != book.title:
        return HttpResponseBadRequest("confirm_title must exactly match the book title")
    verify_hashes = _coerce_optional_bool(payload.get("verify_hashes"), default=True)
    try:
        report = book_deletion.delete_book_safely(book, verify_hashes=verify_hashes)
    except book_deletion.BookDeletionError as exc:
        return HttpResponseBadRequest(str(exc))

    if _wants_html(request):
        return redirect("library-books")
    return JsonResponse(
        {
            "book_id": report.book_id,
            "deleted": True,
            "artifact_count": report.artifact_count,
            "cleanup_warnings": list(report.cleanup_warnings),
        },
    )


@require_POST
@login_required
def library_book_conversion_request(
    request: HttpRequest,
    book_id: int,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        book = Book.objects.prefetch_related("file_variants").get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    payload = _request_payload(request)
    raw_source_variant_id = payload.get("source_variant_id")
    source_variant_id: int
    if isinstance(raw_source_variant_id, int):
        source_variant_id = raw_source_variant_id
    elif isinstance(raw_source_variant_id, str) and raw_source_variant_id.isdigit():
        source_variant_id = int(raw_source_variant_id)
    else:
        return HttpResponseBadRequest("source_variant_id required")

    raw_target_format = payload.get("target_format")
    if not isinstance(raw_target_format, str) or not raw_target_format.strip():
        return HttpResponseBadRequest("target_format required")

    try:
        source_variant = book.file_variants.get(
            pk=source_variant_id,
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )
    except (ValueError, FileVariant.DoesNotExist):
        return HttpResponseBadRequest("source variant does not belong to book")

    request_options = payload.get("request_options")
    if not isinstance(request_options, dict):
        request_options = None
    try:
        job = conversion.request_conversion(
            source_variant=source_variant,
            target_format=raw_target_format,
            requested_by_user_id=profile.user_id,
            request_options=request_options,
        )
    except conversion.ConversionError as exc:
        return HttpResponseBadRequest(str(exc))

    if _wants_html(request):
        return redirect("conversion-job-status", job_id=job.job_id)
    return JsonResponse(conversion.conversion_job_payload(job), status=202)


@require_POST
@login_required
def library_book_ocr_request(
    request: HttpRequest,
    book_id: int,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        book = Book.objects.prefetch_related("file_variants").get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    payload = _request_payload(request)
    raw_source_variant_id = payload.get("source_variant_id")
    if isinstance(raw_source_variant_id, int):
        source_variant_id = raw_source_variant_id
    elif isinstance(raw_source_variant_id, str) and raw_source_variant_id.isdigit():
        source_variant_id = int(raw_source_variant_id)
    else:
        return HttpResponseBadRequest("source_variant_id required")

    try:
        source_variant = book.file_variants.get(
            pk=source_variant_id,
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )
    except (ValueError, FileVariant.DoesNotExist):
        return HttpResponseBadRequest("source variant does not belong to book")

    try:
        job = ocr.request_ocr_from_variant(
            source_variant=source_variant,
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

    if _wants_html(request):
        return redirect("ocr-job-status", job_id=job.job_id)
    return JsonResponse(ocr.ocr_job_payload(job), status=202)


@require_GET
@login_required
def conversion_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.CONVERSION:
        return HttpResponseNotFound()
    payload = conversion.conversion_job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Conversion"},
        )
    return JsonResponse(payload)


@require_POST
@login_required
def library_book_combine_parts(request: HttpRequest, book_id: int) -> HttpResponseBase:
    """Queue a combined whole-book file built from a multi-part work's parts (#60)."""
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        book = Book.objects.get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()
    payload = _request_payload(request)
    raw_format = payload.get("target_format")
    if not isinstance(raw_format, str) or not raw_format.strip():
        return HttpResponseBadRequest("target_format required")
    try:
        job = combine.request_combined_variant(
            book=book,
            target_format=raw_format,
            requested_by_user_id=profile.user_id,
        )
    except combine.CombineError as exc:
        return HttpResponseBadRequest(str(exc))
    if _wants_html(request):
        return redirect("combine-job-status", job_id=job.job_id)
    return JsonResponse(combine.combine_job_payload(job), status=202)


@require_GET
@login_required
def combine_job_status(request: HttpRequest, job_id: str) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.COMBINE_PARTS:
        return HttpResponseNotFound()
    payload = combine.combine_job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Combine parts"},
        )
    return JsonResponse(payload)


@require_GET
@login_required
def ocr_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.OCR:
        return HttpResponseNotFound()
    payload = ocr.ocr_job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "OCR"},
        )
    return JsonResponse(payload)


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
        .prefetch_related("versions")
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
        if _prefetched_active_version(versions) is not None:
            payload["thumbnail"] = _scan_page_thumbnail_url(page)
        page_states = states_by_page.get(page.id, {})
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
    job = ScanJob.objects.filter(pk=job_id).only("id", "state", "updated_at").first()
    if job is None:
        return None
    states = ScanTailorStageState.objects.filter(scan_job_id=job_id).order_by(
        "scan_page__logical_sequence_number",
        "scan_page__side",
        "scan_page_id",
        "stage",
    )
    return {
        "job_id": str(job.id),
        "state": job.state,
        "updated_at": job.updated_at.isoformat(),
        "stages": [_serialize_stage_state(state) for state in states],
    }


def _bind_stage_inputs(state: ScanTailorStageState, page: ScanPage) -> None:
    predecessor_name = scantailor.previous_stage(state.stage)
    if predecessor_name is None:
        active_version = page.active_version()
        state.input_stage_state = None
        state.input_scan_page_version = active_version
        state.input_storage_identifier = active_version.storage_identifier if active_version else ""
        return

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
    if stage == "page_split":
        return (
            str(parameters.get("split_mode", "")).strip().lower() == "manual"
            or "split_x" in parameters
        )
    if stage == "deskew":
        return "manual_angle" in parameters
    if stage == "select_content":
        if str(parameters.get("content_mode", "")).strip().lower() == "manual":
            return True
        return "content_box" in parameters
    if stage == "margins":
        if str(parameters.get("margins_mode", "")).strip().lower() == "manual":
            return True
        return any(
            key in parameters
            for key in {"top", "right", "bottom", "left", "margin", "canvas_width", "canvas_height"}
        )
    if stage == "dewarping":
        if str(parameters.get("dewarp_mode", "")).strip().lower() == "manual":
            return True
        return "dewarp_points" in parameters
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
        if stage == "page_split":
            # The algorithm follows the chosen mode, so switching a single-page
            # passthrough to a manual split (or back) never keeps a stale label (#55).
            state.algorithm = scantailor.split_algorithm_for_mode(
                scantailor.effective_split_mode(state),
            )

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


@require_GET
@login_required
async def scan_job_workspace_events(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    del request
    if normalize_scan_job_id(job_id) is None:
        return HttpResponseBadRequest("invalid job id")

    initial_payload = await sync_to_async(
        _scan_workspace_event_payload,
        thread_sensitive=True,
    )(job_id)
    if initial_payload is None:
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
            payload = (
                initial_payload
                if sequence == 0
                else await sync_to_async(
                    _scan_workspace_event_payload,
                    thread_sensitive=True,
                )(job_id)
            )
            if payload is None:
                yield "event: workspace-closed\ndata: {}\n\n"
                return
            encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
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
        _bind_stage_inputs(state, page)
        state.save(
            update_fields=[
                "input_stage_state",
                "input_scan_page_version",
                "input_storage_identifier",
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
        )
        applied.append(
            {
                "page_id": str(page.id),
                "stage": _serialize_stage_state(state),
            },
        )
        jobs.append(scantailor.stage_job_payload(processing_job))

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
    if wants_input and state.input_stage_state is not None:
        identifier = (
            state.input_stage_state.preview_storage_identifier
            or state.input_stage_state.output_storage_identifier
        )
    elif wants_input:
        identifier = state.input_storage_identifier
    else:
        identifier = state.preview_storage_identifier or state.output_storage_identifier
    if not identifier:
        message = "input preview not ready" if wants_input else "preview not ready"
        return HttpResponseNotFound(message)
    storage = _default_scan_storage()
    try:
        stream = storage.open(identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("preview artifact missing")
    except OSError:
        return HttpResponse("storage unavailable", status=503)
    return FileResponse(stream, as_attachment=False, filename=Path(identifier).name)


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

    capture_layout = _resolve_capture_layout(payload.get("capture_layout"), scanner_id)
    if capture_layout is None:
        return HttpResponseBadRequest("invalid capture_layout (expected single_page or spread)")

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
        capture_layout=capture_layout,
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
        scantailor.auto_complete_single_page_split(page, requested_by_user_id=profile.user_id)

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
        scantailor.auto_complete_single_page_split(page, requested_by_user_id=profile.user_id)

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
    source = _scan_thumbnail_source(page)
    if source is None:
        return HttpResponseNotFound("no page version")

    etag = f'"{source.cache_key}-{SCAN_THUMBNAIL_MAX_EDGE}"'
    # The thumbnail URL is stable while its content follows stage processing, so
    # browsers must revalidate (cheap: ETag -> 304) instead of caching for an hour.
    headers = {"ETag": etag, "Cache-Control": "private, max-age=0, must-revalidate"}
    if request.headers.get("If-None-Match") == etag:
        return HttpResponse(status=304, headers=headers)

    cache_path = _scan_thumbnail_cache_path(source)
    if not cache_path.exists():
        try:
            _render_scan_thumbnail(source, cache_path)
        except (FileNotFoundError, StorageConfigurationError):
            return HttpResponseNotFound("artifact missing")
        except Exception:  # undecodable image data
            return HttpResponse("unsupported image", status=415)

    response = FileResponse(cache_path.open("rb"), as_attachment=False)
    response["Content-Type"] = "image/jpeg"
    for key, value in headers.items():
        response[key] = value
    return response


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


@require_GET
@login_required
@_require_role("admin")
def admin_only(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "admin"})


REVIEW_QUEUE_SORTS: Final = {
    "-created": ("-created_at", "-id"),
    "created": ("created_at", "id"),
    "-confidence": ("-top_score", "-created_at"),
    "confidence": ("top_score", "-created_at"),
}
REVIEW_QUEUE_STATES: Final = (ReviewTask.State.OPEN, ReviewTask.State.CLAIMED)


def _parse_score(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not 0.0 <= value <= 1.0:  # also rejects NaN
        return None
    return value


def _review_queue_queryset(profile: AppUserProfile) -> Any:
    """Open/claimed review tasks visible to ``profile``, annotated with their top score."""

    def top_score(candidates: Any) -> Subquery:
        return Subquery(
            candidates.filter(superseded_at__isnull=True).order_by("-score").values("score")[:1]
        )

    tasks = (
        ReviewTask.objects.select_related(
            "import_job",
            "import_job__uploader",
            "import_job__source_location",
            "scan_job",
            "scan_job__operator",
        )
        .filter(state__in=REVIEW_QUEUE_STATES)
        .annotate(
            top_score=Coalesce(
                top_score(MetadataCandidate.objects.filter(import_job=OuterRef("import_job"))),
                top_score(MetadataCandidate.objects.filter(scan_job=OuterRef("scan_job"))),
            ),
        )
    )
    if profile.role != "admin":
        tasks = tasks.filter(
            Q(import_job__uploader=profile.user) | Q(scan_job__operator=profile.user)
        )
    return tasks


def _review_queue_filters(request: HttpRequest) -> dict[str, Any]:
    state = request.GET.get("state", "").strip()
    if state not in {choice.value for choice in REVIEW_QUEUE_STATES}:
        state = ""
    has_candidates = request.GET.get("has_candidates", "").strip().lower()
    if has_candidates not in {"yes", "no"}:
        has_candidates = ""
    sort = request.GET.get("sort", "-created").strip()
    if sort not in REVIEW_QUEUE_SORTS:
        sort = "-created"
    source = request.GET.get("source", "").strip()
    if source not in {"upload", "scan"} and not source.isdigit():
        source = ""
    return {
        "source": source,
        "state": state,
        "min_score": _parse_score(request.GET.get("min_score")),
        "max_score": _parse_score(request.GET.get("max_score")),
        "has_candidates": has_candidates,
        "sort": sort,
    }


def _apply_review_queue_filters(tasks: Any, filters: dict[str, Any]) -> Any:
    source = filters["source"]
    if source == "upload":
        tasks = tasks.filter(import_job__isnull=False, import_job__source_location__isnull=True)
    elif source == "scan":
        tasks = tasks.filter(scan_job__isnull=False)
    elif source:
        tasks = tasks.filter(import_job__source_location_id=int(source))
    if filters["state"]:
        tasks = tasks.filter(state=filters["state"])
    if filters["min_score"] is not None:
        tasks = tasks.filter(top_score__gte=Decimal(str(filters["min_score"])))
    if filters["max_score"] is not None:
        tasks = tasks.filter(top_score__lte=Decimal(str(filters["max_score"])))
    if filters["has_candidates"] == "yes":
        tasks = tasks.filter(top_score__isnull=False)
    elif filters["has_candidates"] == "no":
        tasks = tasks.filter(top_score__isnull=True)
    ordering: list[Any] = []
    for field in REVIEW_QUEUE_SORTS[filters["sort"]]:
        if field == "top_score":
            ordering.append(F("top_score").asc(nulls_last=True))
        elif field == "-top_score":
            ordering.append(F("top_score").desc(nulls_last=True))
        else:
            ordering.append(field)
    return tasks.order_by(*ordering)


def _review_queue_querystring(filters: dict[str, Any], page_size: int) -> str:
    """Filter state for pagination links, so paging never drops the active filters."""
    params: dict[str, str] = {"view": "html", "page_size": str(page_size)}
    for key in ("source", "state", "has_candidates", "sort"):
        if filters[key]:
            params[key] = filters[key]
    for key in ("min_score", "max_score"):
        if filters[key] is not None:
            params[key] = f"{filters[key]:g}"
    return urlencode(params)


@require_GET
@login_required
def review_queue(request: HttpRequest) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    filters = _review_queue_filters(request)
    tasks = _apply_review_queue_filters(_review_queue_queryset(profile), filters)

    page_size = min(
        _as_positive_int(request.GET.get("page_size"), review_batch.queue_page_size()),
        review_batch.MAX_QUEUE_PAGE_SIZE,
    )
    paginator = Paginator(tasks, page_size)
    page_object = paginator.get_page(_as_positive_int(request.GET.get("page"), 1))

    min_score = review_batch.batch_min_score()
    margin = review_batch.batch_ambiguity_margin()
    rows: list[dict[str, Any]] = []
    payload: list[dict[str, Any]] = []
    task_list = list(page_object.object_list)
    for task in task_list:
        candidates = list(_review_candidates(task))
        eligibility = review_batch.evaluate_top_candidates(
            [candidate.score for candidate in candidates],
            min_score=min_score,
            ambiguity_margin=margin,
        )
        rows.append(
            {
                "task": task,
                "top_candidate": candidates[0] if candidates else None,
                "candidate_count": len(candidates),
                "eligibility": eligibility,
            },
        )
        payload.append(
            {
                **_serialize_task(task),
                "source_location_id": (
                    task.import_job.source_location_id if task.import_job is not None else None
                ),
                "top_score": eligibility.top_score,
                "batch_accept_eligibility": eligibility.reason,
                "candidate_count": len(candidates),
                "candidates": [_serialize_candidate(candidate) for candidate in candidates],
            },
        )
    # Formats of one book on this page collapse into one entry (#49); only the primary
    # entry offers a batch checkbox, its siblings are re-assessed when it is accepted.
    groups = _group_sibling_review_tasks(task_list, payload)
    row_by_task = {row["task"].id: row for row in rows}
    group_rows = [{**row_by_task[group.primary.id], "group": group} for group in groups]
    if _wants_html(request):
        return render(
            request,
            "core/review_queue.html",
            {
                "rows": group_rows,
                "groups": groups,
                "count": paginator.count,
                "page_object": page_object,
                "filters": filters,
                "querystring": _review_queue_querystring(filters, page_size),
                "source_locations": SourceLocation.objects.order_by("name"),
                "batch_min_score": min_score,
                "batch_ambiguity_margin": margin,
                "batch_max_tasks": review_batch.batch_max_tasks(),
            },
        )
    return JsonResponse(
        {
            "items": payload,
            "count": paginator.count,
            "page": page_object.number,
            "num_pages": paginator.num_pages,
            "page_size": page_size,
            "group_count": len(groups),
        },
    )


# Formats most likely to carry real embedded metadata first: the primary entry of a
# sibling group should be the most informative file to review (#49).
REVIEW_FORMAT_PRIORITY = ("epub", "azw3", "mobi", "fb2", "pdf", "djvu", "html", "rtf", "txt")


@dataclass
class ReviewQueueGroup:
    primary: ReviewTask
    primary_item: dict[str, Any]
    others: list[tuple[ReviewTask, dict[str, Any]]]

    @property
    def other_count(self) -> int:
        return len(self.others)


def _group_sibling_review_tasks(
    tasks: list[ReviewTask],
    items: list[dict[str, Any]],
) -> list[ReviewQueueGroup]:
    """Collapse open review tasks for formats of one book into one queue entry.

    Uses the sibling key of ``intake._sibling_jobs`` (source, folder, case-folded name;
    only for sources with ``group_sibling_formats``). The primary entry is the task with
    the best candidate score, then the most metadata-rich format, then the oldest. Each
    item in ``items`` gains ``sibling_group`` (``None`` when it stands alone).
    """
    keyed: dict[tuple[int, str, str], list[int]] = {}
    order: list[tuple[int, str, str] | int] = []
    for index, task in enumerate(tasks):
        key = intake.sibling_group_key(task.import_job) if task.import_job else None
        if key is None:
            order.append(index)
            continue
        if key not in keyed:
            keyed[key] = []
            order.append(key)
        keyed[key].append(index)

    def rank(index: int) -> tuple[float, int, datetime]:
        scores = [candidate["score"] for candidate in items[index]["candidates"]]
        job = tasks[index].import_job
        fmt = job.detected_format if job is not None else ""
        priority = (
            REVIEW_FORMAT_PRIORITY.index(fmt)
            if fmt in REVIEW_FORMAT_PRIORITY
            else len(REVIEW_FORMAT_PRIORITY)
        )
        return (-max(scores, default=0.0), priority, tasks[index].created_at)

    groups: list[ReviewQueueGroup] = []
    for entry in order:
        members = sorted(keyed[entry], key=rank) if isinstance(entry, tuple) else [entry]
        primary, *others = members
        if len(members) > 1:
            annotation = {
                "primary_task_id": str(tasks[primary].id),
                "task_ids": [str(tasks[index].id) for index in members],
                "size": len(members),
            }
            for index in members:
                items[index]["sibling_group"] = annotation
        else:
            items[primary]["sibling_group"] = None
        groups.append(
            ReviewQueueGroup(
                primary=tasks[primary],
                primary_item=items[primary],
                others=[(tasks[index], items[index]) for index in others],
            )
        )
    return groups


@require_GET
@login_required
def review_detail(request: HttpRequest, task_id: str) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    payload = _serialize_task(task)
    candidates = list(_review_candidates(task))
    _attach_genre_suggestions(task, candidates)
    payload["candidates"] = [
        {
            **_serialize_candidate(candidate),
            "genre_suggestion": _serialize_genre_suggestion(candidate.genre_suggestion),
        }
        for candidate in candidates
    ]
    auto_accept_block = _auto_accept_block(task)
    payload["auto_accept_block"] = auto_accept_block
    local_suggestion = _local_suggestion(task, candidates)
    payload["local_suggestion"] = (
        {key: value for key, value in local_suggestion.items() if key != "confirmed_candidate"}
        if local_suggestion is not None
        else None
    )
    llm_panel = _llm_panel(task)
    payload["llm_suggestion"] = llm_panel["suggestion_payload"] if llm_panel else None
    multipart_group = multipart.group_for_task(task)
    payload["multipart"] = multipart_group
    search_context = _review_search_context(task)
    active_job = search_context["active_search_job"]
    payload["searches"] = [
        metadata_search.serialize_search(search) for search in search_context["searches"]
    ]
    payload["search_in_progress"] = active_job is not None
    payload["active_search_job"] = (
        {"job_id": str(active_job.job_id), "state": active_job.state}
        if active_job is not None
        else None
    )
    payload["failed_search_error"] = search_context["failed_search_error"]
    scanner_opf_review = scanner_opf.review_context(task)
    payload["scanner_opf"] = (
        {
            "kind": scanner_opf_review["kind"],
            "rows": scanner_opf_review.get("rows", []),
            "preselected": scanner_opf_review.get("preselected"),
            "reason": scanner_opf_review.get("reason"),
            # Send back with action=resolve; a review whose evidence changed is rejected.
            "revision": scanner_opf_review.get("revision"),
            "open": scanner_opf_review.get("open"),
            "obsolete": scanner_opf_review.get("obsolete"),
            "possible_duplicate_book_ids": [
                book.pk for book in scanner_opf_review.get("duplicates", [])
            ],
        }
        if scanner_opf_review is not None
        else None
    )
    isbn_lookup = search_context["isbn_lookup"]
    payload["isbn_lookup"] = (
        {
            "isbn": isbn_lookup["isbn"],
            "state": isbn_lookup["state"],
            "verified_candidate_ids": [str(pk) for pk in isbn_lookup["verified_candidate_ids"]],
            "unambiguous": isbn_lookup["unambiguous"],
            "best_candidate_id": (
                str(isbn_lookup["best"].id) if isbn_lookup["best"] is not None else None
            ),
            "auto_accepted": (
                task.state == ReviewTask.State.RESOLVED
                and isinstance(task.decision_payload, dict)
                and task.decision_payload.get("resolution_source") == "reviewer_isbn"
            ),
        }
        if isbn_lookup is not None
        else None
    )
    if _wants_html(request):
        return render(
            request,
            "core/review_detail.html",
            {
                "task": task,
                "payload": payload,
                "auto_accept_block": auto_accept_block,
                "local_suggestion": local_suggestion,
                "multipart": multipart_group,
                "candidates": candidates,
                "all_genres": list(Genre.objects.order_by("name")),
                "books": Book.objects.order_by("title")[:250],
                "llm_panel": llm_panel,
                "llm_candidate_ids": _llm_candidate_ids(task),
                "scanner_opf_review": scanner_opf_review,
                **search_context,
            },
        )
    return JsonResponse(payload)


def _llm_candidate_ids(task: ReviewTask) -> set[int]:
    """Candidates found only through LLM-proposed search terms (labelled on the page)."""
    if task.import_job_id is None:
        return set()
    return {
        candidate.pk
        for candidate in MetadataCandidate.objects.filter(import_job_id=task.import_job_id)
        if isinstance(candidate.raw_payload, dict)
        and candidate.raw_payload.get(llm_identification.HINT_SOURCE_KEY)
        == llm_identification.HINT_SOURCE_LLM
    }


def _llm_suggestion_payload(suggestion: LLMSuggestion) -> dict[str, Any]:
    output = suggestion.output if isinstance(suggestion.output, dict) else {}
    grounding = suggestion.grounding if isinstance(suggestion.grounding, dict) else {}
    return {
        "id": suggestion.pk,
        "label": "AI suggestion, unverified",
        "trigger": suggestion.trigger,
        "outcome": suggestion.outcome,
        "model": suggestion.model,
        "model_version": suggestion.model_version,
        "prompt_template_version": suggestion.prompt_template_version,
        "created_at": suggestion.created_at.isoformat(),
        "new_candidate_count": suggestion.new_candidate_count,
        "output": output,
        "grounding": grounding,
    }


def _llm_panel(task: ReviewTask) -> dict[str, Any] | None:
    """The "AI suggestion (unverified)" panel for an import review task, or None."""
    if task.import_job_id is None:
        return None
    suggestion = llm_identification.latest_suggestion(task)
    enabled = llm.llm_enabled()
    if suggestion is None and not enabled:
        return None
    grounding = (
        suggestion.grounding if suggestion and isinstance(suggestion.grounding, dict) else {}
    )
    output = suggestion.output if suggestion and isinstance(suggestion.output, dict) else {}
    active = llm_identification.active_job(task)
    return {
        "enabled": enabled,
        "suggestion": suggestion,
        "suggestion_payload": _llm_suggestion_payload(suggestion) if suggestion else None,
        "output": output,
        "title_hints": grounding.get("title_hints", []),
        "author_hints": grounding.get("author_hints", []),
        "isbn_hints_kept": grounding.get("isbn_hints_kept", []),
        "isbn_hints_dropped": grounding.get("isbn_hints_dropped", []),
        "first_title": (output.get("title_hints") or [""])[0],
        "authors_text": ", ".join(output.get("author_hints") or []),
        "active_job": active,
    }


_book_for_candidate = review_accept.book_for_candidate


def _accept_review_candidate(
    *,
    task: ReviewTask,
    candidate: MetadataCandidate,
    reviewer: Any,
    decision_notes: str = "",
    decision_payload: dict[str, Any] | None = None,
    reviewer_authors: list[str] | None = None,
    reviewer_genres: list[Genre] | None = None,
    inline_cover_fallback: bool = True,
) -> tuple[Book, FileVariant | None]:
    """Resolve ``task`` with ``candidate``; shared by single and batch accept.

    ``reviewer_authors`` only fill a Book that has no authors yet (provider authors
    always win). ``reviewer_genres`` is an explicit reviewer choice; ``None`` assigns
    genres automatically from provider/embedded subjects. Front-cover extraction is
    a background job queued after commit; batch accept passes
    ``inline_cover_fallback=False`` so it never extracts in the request, even without
    a configured queue.
    """
    return review_accept.accept_candidate(
        task=task,
        candidate=candidate,
        reviewer=reviewer,
        reviewer_authors=reviewer_authors,
        reviewer_genres=reviewer_genres,
        decision_notes=decision_notes,
        extra_payload=decision_payload,
        inline_cover_fallback=inline_cover_fallback,
    )


@require_POST
@login_required
def review_accept_candidate(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")

    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    blocker = multipart.confirmation_blocker(task)
    if blocker is not None:
        return HttpResponseBadRequest(blocker)

    payload = _request_payload(request)
    candidate_id = _coerce_int(payload.get("candidate_id"), minimum=1)
    if candidate_id is None:
        return HttpResponseBadRequest("candidate_id required")

    try:
        candidate = MetadataCandidate.objects.get(pk=candidate_id)
    except MetadataCandidate.DoesNotExist:
        return HttpResponseBadRequest("candidate not found")
    if candidate.import_job_id != task.import_job_id or candidate.scan_job_id != task.scan_job_id:
        return HttpResponseBadRequest("candidate not found")
    if candidate.superseded_at is not None:
        return HttpResponseBadRequest(
            "candidate is from an earlier search; accept one of the current candidates"
        )
    try:
        reviewer_genres = _reviewer_genre_choice(request, payload)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    try:
        _note_review_redecided(task, profile)
        book, variant = review_accept.accept_candidate(
            task=task,
            candidate=candidate,
            reviewer=profile.user,
            reviewer_authors=_parse_author_list(payload.get("authors")),
            reviewer_genres=reviewer_genres,
            decision_notes=f"Accepted candidate {candidate.id}",
        )
        contributions.record_review_resolution(task)
        return _review_action_response(
            request,
            {
                "task_id": str(task.id),
                "source_id": str(task.import_job_id or task.scan_job_id),
                "book_id": str(book.id),
                "file_variant_id": str(variant.id) if variant is not None else None,
            },
        )
    except Exception as exc:  # pragma: no cover
        return HttpResponseBadRequest(str(exc))


def _batch_task_ids(request: HttpRequest) -> list[int] | None:
    """Ordered, de-duplicated task ids from a form (``task_ids``) or JSON body."""
    payload = _json_body(request)
    raw: Any = payload.get("task_ids") if payload else request.POST.getlist("task_ids")
    if not isinstance(raw, list):
        return None
    task_ids: list[int] = []
    for value in raw:
        task_id = _coerce_int(value, minimum=1)
        if task_id is None:
            return None
        if task_id not in task_ids:
            task_ids.append(task_id)
    return task_ids


def _batch_accept_one(
    *,
    profile: AppUserProfile,
    task_id: int,
    min_score: float,
    ambiguity_margin: float,
) -> dict[str, Any]:
    """Accept one task's top candidate if it is clearly eligible; report the outcome."""
    result: dict[str, Any] = {"task_id": str(task_id)}
    try:
        with transaction.atomic():
            try:
                task = ReviewTask.objects.select_for_update().get(pk=task_id)
            except ReviewTask.DoesNotExist:
                return {**result, "outcome": "not_found"}
            # Same rule as every single-task review action; checked before anything
            # about the task (title, state, candidates) is revealed.
            if not _can_manage_review_task(profile, task):
                return {**result, "outcome": "forbidden"}
            result["title"] = task.inferred_title or task.source_filename
            if task.state not in REVIEW_QUEUE_STATES:
                return {**result, "outcome": "closed"}
            if task.task_type == ReviewTask.TaskType.MULTIPART_WORK:
                return {
                    **result,
                    "outcome": "requires_individual_review",
                    "detail": "Confirm multipart membership and reading order on its review page.",
                }
            candidates = list(_review_candidates(task))
            eligibility = review_batch.evaluate_top_candidates(
                [candidate.score for candidate in candidates],
                min_score=min_score,
                ambiguity_margin=ambiguity_margin,
            )
            result["top_score"] = eligibility.top_score
            result["runner_up_score"] = eligibility.runner_up_score
            if not eligibility.eligible:
                return {**result, "outcome": eligibility.reason}
            candidate = candidates[0]
            book, variant = _accept_review_candidate(
                task=task,
                candidate=candidate,
                reviewer=profile.user,
                decision_notes=(
                    f"Batch-accepted top candidate {candidate.id} "
                    f"(score {eligibility.top_score:.2f})"
                ),
                decision_payload={
                    "batch_accept": True,
                    "candidate_id": candidate.id,
                    "top_score": eligibility.top_score,
                    "runner_up_score": eligibility.runner_up_score,
                    "min_score": min_score,
                    "ambiguity_margin": ambiguity_margin,
                },
                # Up to BATCH_MAX_TASKS books: covers are background jobs only.
                inline_cover_fallback=False,
            )
            contributions.record_review_resolution(task)
    except Exception as exc:  # one failing task must not abort the rest of the batch
        logger.warning(
            "review_batch_accept_task_failed",
            review_task_id=task_id,
            error_type=type(exc).__name__,
        )
        return {**result, "outcome": "error", "detail": str(exc)}
    return {
        **result,
        "outcome": "accepted",
        "candidate_id": str(candidate.id),
        "book_id": str(book.id),
        "file_variant_id": str(variant.id) if variant is not None else None,
    }


@require_POST
@login_required
def review_batch_accept(request: HttpRequest) -> HttpResponseBase:
    """Accept the top candidate of each selected task that is clearly unambiguous.

    Every task is checked individually: permissions, open state, a top score at or
    above the batch minimum, and a runner-up outside the ambiguity margin. Anything
    else is reported and left in the queue for individual review.
    """
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    task_ids = _batch_task_ids(request)
    if not task_ids:
        return HttpResponseBadRequest("task_ids required")
    max_tasks = review_batch.batch_max_tasks()
    if len(task_ids) > max_tasks:
        return HttpResponseBadRequest(f"at most {max_tasks} tasks per batch")

    payload = _request_payload(request)
    raw_min_score = payload.get("min_score")
    min_score = review_batch.effective_min_score(
        _parse_score(str(raw_min_score)) if raw_min_score is not None else None,
    )
    margin = review_batch.batch_ambiguity_margin()

    results = [
        _batch_accept_one(
            profile=profile,
            task_id=task_id,
            min_score=min_score,
            ambiguity_margin=margin,
        )
        for task_id in task_ids
    ]
    outcomes: dict[str, int] = {}
    for item in results:
        outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
    logger.info(
        "review_batch_accept",
        reviewer_id=profile.user_id,
        requested=len(task_ids),
        min_score=min_score,
        ambiguity_margin=margin,
        **{f"outcome_{key}": value for key, value in sorted(outcomes.items())},
    )
    response_payload = {
        "accepted": outcomes.get("accepted", 0),
        "skipped": len(results) - outcomes.get("accepted", 0),
        "min_score": min_score,
        "ambiguity_margin": margin,
        "results": results,
    }
    if _wants_html(request):
        return render(request, "core/review_batch_result.html", response_payload)
    return JsonResponse(response_payload)


@require_POST
@login_required
def review_mark_unidentified(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    payload = _request_payload(request)
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    _note_review_redecided(task, profile)
    task.state = ReviewTask.State.RESOLVED
    task.decision = ReviewTask.Decision.MARK_UNIDENTIFIED
    task.reviewer = profile.user
    task.decision_notes = str(payload.get("notes", ""))
    task.decision_payload = {"mark": "unidentified"}
    task.decided_at = timezone.now()
    task.save(
        update_fields=[
            "state",
            "decision",
            "decision_notes",
            "decision_payload",
            "reviewer_id",
            "decided_at",
            "updated_at",
        ],
    )
    contributions.record_review_resolution(task)
    return _review_action_response(request, {"task_id": str(task.id), "state": task.state})


def _save_review_terms(task: ReviewTask, terms: dict[str, Any]) -> None:
    """Store reviewer terms on the task and, for scans, as the scan's hints.

    The task's terms are the reviewer's saved search terms; later automatic scan
    identifications use the scan job's hints, so both are kept in step.
    """
    metadata_search.apply_terms(task, terms)
    scan_job = task.scan_job if task.scan_job_id is not None else None
    if scan_job is not None:
        scan_job.provisional_title = task.inferred_title
        scan_job.provisional_authors = task.inferred_authors
        scan_job.save(update_fields=["provisional_title", "provisional_authors", "updated_at"])


def _closed_or_forbidden_review(
    request: HttpRequest,
    task_id: str,
) -> tuple[AppUserProfile, ReviewTask] | HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
        return HttpResponseBadRequest("review task is already closed")
    return profile, task


def _queue_review_search(
    request: HttpRequest,
    *,
    profile: AppUserProfile,
    task: ReviewTask,
    terms: dict[str, Any],
    decision_payload: dict[str, Any],
    notes: str,
    busy_prefix: str,
) -> HttpResponseBase:
    """Claim the task and queue a provider search with ``terms`` (text or ISBN)."""
    task.state = ReviewTask.State.CLAIMED
    task.decision = ReviewTask.Decision.SEARCH_AGAIN
    task.reviewer = profile.user
    task.decision_notes = notes
    task.decided_at = timezone.now()
    task.decision_payload = decision_payload
    task.save(
        update_fields=[
            "state",
            "decision",
            "decision_notes",
            "decision_payload",
            "inferred_title",
            "inferred_authors",
            "reviewer_id",
            "decided_at",
            "updated_at",
        ],
    )
    try:
        if task.scan_job_id is not None:
            scan_job = task.scan_job
            if scan_job is None:
                raise scan_identification.ScanIdentificationError("scan job is unavailable")
            processing_job = scan_identification.request_scan_identification(
                scan_job=scan_job,
                requested_by_user_id=profile.user_id,
                terms=terms,
            )
            job_payload = scan_identification.job_payload(processing_job)
        else:
            processing_job = review_identification.request_review_identification(
                review_task=task,
                requested_by_user_id=profile.user_id,
                terms=terms,
            )
            job_payload = review_identification.job_payload(processing_job)
    except metadata_search.SearchInProgressError as exc:
        task.state = ReviewTask.State.OPEN
        task.save(update_fields=["state", "updated_at"])
        if _wants_html(request):
            messages.warning(request, f"{busy_prefix} {exc}.")
            return redirect("review-detail", task_id=task.id)
        return JsonResponse({"task_id": str(task.id), "error": str(exc)}, status=409)
    except (
        review_identification.ReviewIdentificationError,
        scan_identification.ScanIdentificationError,
    ) as exc:
        task.state = ReviewTask.State.OPEN
        task.save(update_fields=["state", "updated_at"])
        if _wants_html(request):
            messages.error(request, f"The search could not be started: {exc}")
            return redirect("review-detail", task_id=task.id)
        return HttpResponseBadRequest(str(exc))

    if _wants_html(request):
        # The review page shows the search while it runs and its outcome afterwards.
        return redirect("review-detail", task_id=task.id)
    return JsonResponse(
        {
            "task_id": str(task.id),
            "state": task.state,
            "identification_job": job_payload,
        },
        status=202,
    )


@require_POST
@login_required
def review_ask_llm(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    """Queue the local-LLM identification assist for one import review task (#26)."""
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if not llm.llm_enabled():
        return HttpResponseBadRequest("the AI identification assist is disabled")
    try:
        processing_job = llm_identification.request_llm_identification(
            review_task=task,
            trigger=LLMSuggestion.Trigger.REVIEWER,
            requested_by_user_id=profile.user_id,
        )
    except llm_identification.LLMIdentificationError as exc:
        return HttpResponseBadRequest(str(exc))
    if _wants_html(request):
        if processing_job.state in {
            ProcessingJob.JobState.SUCCESS,
            ProcessingJob.JobState.FAILED,
        }:
            return redirect("review-detail", task_id=task.id)
        return redirect("llm-identification-job-status", job_id=processing_job.job_id)
    return JsonResponse(
        {
            "task_id": str(task.id),
            "llm_job": llm_identification.job_payload(processing_job),
        },
        status=202,
    )


@require_GET
@login_required
def llm_identification_job_status(
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
    if job.job_type != ProcessingJob.JobType.LLM_IDENTIFICATION:
        return HttpResponseNotFound()
    payload = llm_identification.job_payload(job)
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(
            pk=payload.get("review_task_id"),
        )
    except (ReviewTask.DoesNotExist, ValueError, TypeError):
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if _wants_html(request):
        if job.state in {ProcessingJob.JobState.SUCCESS, ProcessingJob.JobState.FAILED}:
            return redirect("review-detail", task_id=task.id)
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "AI identification assist"},
        )
    return JsonResponse(payload)


@require_POST
@login_required
def review_search_again(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    """Save the posted search terms (if any) and queue a provider search with them."""
    loaded = _closed_or_forbidden_review(request, task_id)
    if isinstance(loaded, HttpResponseBase):
        return loaded
    profile, task = loaded
    payload = _request_payload(request)
    posted_terms = metadata_search.terms_from_payload(payload)
    if posted_terms is not None:
        _save_review_terms(task, posted_terms)
    terms = metadata_search.task_terms(task)
    return _queue_review_search(
        request,
        profile=profile,
        task=task,
        terms=terms,
        decision_payload={"search_again": True, "terms": terms},
        notes=str(payload.get("notes", "")),
        busy_prefix="Search terms saved, but",
    )


@require_POST
@login_required
def review_isbn_lookup(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    """Queue a provider lookup by the ISBN the reviewer typed (#59).

    The typed ISBN is only a query: a record counts as verified when a provider
    returns it carrying that ISBN. The task's saved title/author terms are kept.
    """
    loaded = _closed_or_forbidden_review(request, task_id)
    if isinstance(loaded, HttpResponseBase):
        return loaded
    profile, task = loaded
    payload = _request_payload(request)
    isbn = metadata_search.parse_isbn(payload.get("isbn"))
    if isbn is None:
        message = "enter one valid ISBN-10 or ISBN-13 (the check digit did not match)"
        if _wants_html(request):
            messages.error(request, f"ISBN lookup not started: {message}.")
            return redirect("review-detail", task_id=task.id)
        return HttpResponseBadRequest(message)
    return _queue_review_search(
        request,
        profile=profile,
        task=task,
        terms=metadata_search.isbn_terms(isbn),
        decision_payload={"isbn_lookup": isbn},
        notes=str(payload.get("notes", "")),
        busy_prefix="ISBN lookup not started:",
    )


@require_GET
@login_required
def review_identification_job_status(
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
    if job.job_type != ProcessingJob.JobType.REVIEW_IDENTIFICATION:
        return HttpResponseNotFound()
    payload = review_identification.job_payload(job)
    task_id = payload.get("review_task_id")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except (ReviewTask.DoesNotExist, ValueError, TypeError):
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if _wants_html(request):
        if job.state == ProcessingJob.JobState.SUCCESS:
            return redirect("review-detail", task_id=task.id)
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Metadata search"},
        )
    return JsonResponse(payload)


@require_POST
@login_required
def review_edit_search_terms(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    payload = _request_payload(request)
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    posted_terms = metadata_search.terms_from_payload(payload)
    if posted_terms is not None:
        _save_review_terms(task, posted_terms)

    contributions.record_review_reversal(
        task,
        source=ReviewDecisionCorrection.Source.REOPENED,
        found_by=profile.user,
        note="search terms edited on a resolved review",
    )
    task.state = ReviewTask.State.OPEN
    task.decision = ReviewTask.Decision.EDIT_SEARCH_TERMS
    task.reviewer = profile.user
    task.decision_notes = str(payload.get("notes", ""))
    task.decided_at = timezone.now()
    task.save(
        update_fields=[
            "state",
            "decision",
            "decision_notes",
            "inferred_title",
            "inferred_authors",
            "reviewer_id",
            "decided_at",
            "updated_at",
        ],
    )
    return _review_action_response(
        request,
        {"task_id": str(task.id), "state": task.state},
        task_id=task.id,
    )


@require_POST
@login_required
def review_manual_entry(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    blocker = multipart.confirmation_blocker(task)
    if blocker is not None:
        return HttpResponseBadRequest(blocker)

    payload = _request_payload(request)
    if not isinstance(payload.get("title"), str):
        return HttpResponseBadRequest("title required")
    try:
        reviewer_genres = _reviewer_genre_choice(request, payload)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    book = _build_manual_book_from_payload(payload)
    # A manually entered book has no provider record, so only the file's own tags apply.
    _apply_review_genres(
        book=book,
        task=task,
        reviewer_genres=reviewer_genres,
        provider_subjects=None,
        reviewer=request.user,
    )

    _note_review_redecided(task, profile)
    variant = _resolve_review_source(
        task=task,
        book=book,
        reviewer=profile.user,
        decision=cast(str, ReviewTask.Decision.MANUAL_ENTRY),
        decision_payload=payload,
    )
    contributions.record_review_resolution(task)
    return _review_action_response(
        request,
        {
            "task_id": str(task.id),
            "source_id": str(task.import_job_id or task.scan_job_id),
            "book_id": str(book.id),
            "variant_id": str(variant.id) if variant is not None else None,
        },
    )


@require_POST
@login_required
def review_scanner_opf(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Scanner-OPF review actions (#130).

    ``action=resolve`` with ``choice`` decides a metadata conflict (provider record vs
    scanner OPF: ``scanner``/``provider``; hand-edited field vs OPF:
    ``current``/``scanner``). ``action=create_book`` creates a Book from the scanner's
    OPF for a scan that finish handed to identification (ambiguous ISBN, likely
    duplicate).
    """
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()
    try:
        task = ReviewTask.objects.select_related("scan_job").get(pk=task_id)
    except (ReviewTask.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    payload = _request_payload(request)
    action = str(payload.get("action") or "")
    try:
        if action == "resolve":
            revision = str(payload.get("revision") or "")
            if not revision:
                return HttpResponseBadRequest(
                    "revision is required: send the scanner_opf.revision of the review you "
                    "decided on"
                )
            book = scanner_opf.resolve_conflict(
                task,
                choice=str(payload.get("choice") or ""),
                user=profile.user,
                revision=revision,
            )
        elif action == "create_book":
            book = scanner_opf.create_book_from_review(task, user=profile.user)
            contributions.record_review_resolution(task)
        else:
            return HttpResponseBadRequest("action must be resolve or create_book")
    except scanner_opf.StaleReview as exc:
        # The decision was made on evidence that no longer applies: reject it and show
        # the refreshed review (form error on the page, 409 for API clients).
        if _wants_html(request):
            messages.error(request, str(exc))
            return redirect("review-detail", task_id=task.id)
        task.refresh_from_db()
        context = scanner_opf.review_context(task)
        return JsonResponse(
            {
                "task_id": str(task.id),
                "error": str(exc),
                "state": task.state,
                "revision": context["revision"] if context is not None else None,
            },
            status=409,
        )
    except scanner_opf.ScannerOpfError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {"task_id": str(task.id), "book_id": str(book.pk), "state": ReviewTask.State.RESOLVED},
        task_id=task.id,
    )


@require_POST
@login_required
def review_merge_existing(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    payload = _request_payload(request)
    raw_book_id = payload.get("book_id")
    if isinstance(raw_book_id, int):
        book_id = raw_book_id
    elif isinstance(raw_book_id, str) and raw_book_id.isdigit():
        book_id = int(raw_book_id)
    else:
        return HttpResponseBadRequest("book_id required")
    try:
        book = Book.objects.get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseBadRequest("book not found")
    blocker = multipart.confirmation_blocker(task)
    if blocker is not None:
        return HttpResponseBadRequest(blocker)

    _note_review_redecided(task, profile)
    _resolve_review_source(
        task=task,
        book=book,
        reviewer=profile.user,
        decision=cast(str, ReviewTask.Decision.MERGED_BOOK),
        decision_payload={"book_id": book_id},
    )
    contributions.record_review_resolution(task)
    return _review_action_response(
        request,
        {"task_id": str(task.id), "book_id": str(book.id)},
    )


def _multipart_review_task(
    request: HttpRequest,
    task_id: str,
) -> tuple[ReviewTask | None, AppUserProfile | None, HttpResponseBase | None]:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return None, None, HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "import_job__import_group").get(
            pk=task_id
        )
    except (ReviewTask.DoesNotExist, ValueError):
        return None, None, HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return None, None, HttpResponseForbidden()
    if task.task_type != ReviewTask.TaskType.MULTIPART_WORK:
        return None, None, HttpResponseBadRequest("not a multi-part review")
    if task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
        return None, None, HttpResponseBadRequest("review task is already closed")
    return task, profile, None


@require_POST
@login_required
def review_multipart_order(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Save the reviewer's part order/labels before confirming a multi-part work.

    Form posts send ``part_index_<import_job_id>`` / ``part_label_<import_job_id>``;
    JSON posts send ``{"parts": [{"import_job_id", "part_index", "part_label"}]}``.
    """
    task, _profile, error = _multipart_review_task(request, task_id)
    if error is not None or task is None:
        return error or HttpResponseBadRequest("invalid request")
    payload = _request_payload(request)
    order: dict[str, tuple[int, str]] = {}
    raw_parts = payload.get("parts")
    if isinstance(raw_parts, list):
        for entry in raw_parts:
            if not isinstance(entry, dict):
                return HttpResponseBadRequest("invalid parts")
            index = _coerce_int(entry.get("part_index"), minimum=1)
            if index is None:
                return HttpResponseBadRequest("part_index must be a positive number")
            order[str(entry.get("import_job_id"))] = (index, str(entry.get("part_label") or ""))
    else:
        for key, value in payload.items():
            if not key.startswith("part_index_"):
                continue
            job_id = key.removeprefix("part_index_")
            index = _coerce_int(value, minimum=1)
            if index is None:
                return HttpResponseBadRequest("part_index must be a positive number")
            order[job_id] = (index, str(payload.get(f"part_label_{job_id}", "") or ""))
    if not order:
        return HttpResponseBadRequest("parts required")
    try:
        multipart.update_part_order(task, order)
    except multipart.MultipartError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {"task_id": str(task.id), "multipart": multipart.group_for_task(task)},
        task_id=task.id,
    )


@require_POST
@login_required
def review_multipart_acknowledge_incomplete(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Acknowledge the parts that block confirming a multi-part work.

    Missing (failed/absent) parts are left out; parts identical to another Book's file
    are included as parts of this work.
    """
    task, profile, error = _multipart_review_task(request, task_id)
    if error is not None or task is None or profile is None:
        return error or HttpResponseBadRequest("invalid request")
    try:
        absent, duplicates = multipart.acknowledge_incomplete(
            review_task=task, reviewer=profile.user
        )
    except multipart.MultipartError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {
            "task_id": str(task.id),
            "acknowledged_missing_parts": absent,
            "acknowledged_duplicate_parts": duplicates,
        },
        task_id=task.id,
    )


@require_POST
@login_required
def review_multipart_reject(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Not one work: close the group and identify every file as its own import."""
    task, profile, error = _multipart_review_task(request, task_id)
    if error is not None or task is None or profile is None:
        return error or HttpResponseBadRequest("invalid request")
    payload = _request_payload(request)
    try:
        split = multipart.reject_grouping(
            review_task=task,
            reviewer=profile.user,
            notes=str(payload.get("notes", "")),
        )
    except multipart.MultipartError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {
            "task_id": str(task.id),
            "state": ReviewTask.State.RESOLVED,
            "split_import_job_ids": [str(job.id) for job in split],
        },
    )


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
