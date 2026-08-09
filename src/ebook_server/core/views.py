"""Core application views."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Final, cast
from uuid import UUID

import redis
from django.conf import settings as django_settings
from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.core.files.base import ContentFile
from django.core.paginator import Paginator
from django.db import connections, transaction
from django.db.models import Count, Q
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from ebook_server.jobs import enqueue
from ebook_server.storage import LocalFilesystemStorage

from . import book_deletion, conversion, intake, ocr, scan_pipeline, scantailor
from .models import (
    AppUserProfile,
    Author,
    Book,
    CoverImage,
    FileVariant,
    Identifier,
    ImportJob,
    MetadataCandidate,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    ScanTailorStageState,
    ScanTodo,
    StorageLocation,
)
from .storage_registry import StorageConfigurationError, backend_for_location

SCANNING_STORAGE_ROOT = "scan-artifacts"
SCANNING_NAMESPACE: Final[str] = "scan-jobs"


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
        user = authenticate(request, username=username, password=password)
        if user is not None:
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


def _serialize_scan_page(page: ScanPage) -> dict[str, Any]:
    active_version = page.active_version()
    return {
        "id": str(page.id),
        "logical_sequence_number": page.logical_sequence_number,
        "side": page.side,
        "open_rescan_todos": page.todos.filter(state=ScanTodo.State.OPEN).count(),
        "active_version": _serialize_scan_page_version(active_version),
        "available_versions": [
            _serialize_scan_page_version(v) for v in page.versions.order_by("-version")
        ],
        "created_at": page.created_at.isoformat(),
        "updated_at": page.updated_at.isoformat(),
    }


def _serialize_stage_state(state: ScanTailorStageState) -> dict[str, Any]:
    return {
        "id": state.id,
        "scan_job_id": str(state.scan_job_id),
        "scan_page_id": str(state.scan_page_id),
        "stage": state.stage,
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


def _normalize_scan_side(raw_side: str | None) -> str:
    raw = (raw_side or "").strip().lower()
    if raw in {ScanPage.Side.SINGLE, ScanPage.Side.LEFT, ScanPage.Side.RIGHT}:
        return raw
    return ScanPage.Side.SINGLE


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
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(raw, str):
        normalized = raw.strip().replace("Z", "+00:00")
        try:
            return datetime.fromisoformat(normalized)
        except ValueError:
            return None
    return None


def _parse_legacy_scan_put_payload(raw_body: bytes) -> dict[str, Any]:
    if not raw_body:
        return {}
    body = raw_body.decode(errors="replace").strip()
    if not (body.startswith("{") and body.endswith("}")):
        return {}
    if "'file':" not in body:
        return {}
    result: dict[str, Any] = {}
    checksum_match = re.search(
        r"'checksum_sha256'\s*:\s*'([a-fA-F0-9]{40,64})'",
        body,
    )
    if checksum_match is not None:
        result["checksum_sha256"] = checksum_match.group(1)
    return result


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


def _coerce_int(value: Any, *, minimum: int = 0) -> int | None:
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return None
    if parsed < minimum:
        return None
    return parsed


def _coerce_uuid(value: Any) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if value is None:
        return None
    if isinstance(value, str):
        candidate = value.strip()
    else:
        candidate = str(value)
    try:
        return UUID(candidate)
    except (TypeError, ValueError, AttributeError):
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {"1", "true", "t", "yes", "y", "on"}


def _coerce_optional_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "t", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "f", "no", "n", "off"}:
        return False
    return default


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
        job_id = _coerce_uuid(raw_job_id)
        if job_id is None:
            return None, "invalid job_id"
        queryset = queryset.filter(scan_job_id=job_id)
    if raw_page_id := _coerce_str_or_none(request.GET.get("scan_page_id")):
        page_id = _coerce_uuid(raw_page_id)
        if page_id is None:
            return None, "invalid scan_page_id"
        queryset = queryset.filter(scan_page_id=page_id)
    return queryset, ""


def _cleanup_staged_upload(path: str) -> None:
    Path(path).unlink(missing_ok=True)


def _build_page_storage_destination(
    *,
    scan_job_id: str,
    logical_sequence_number: int,
    side: str,
    version: int,
    original_name: str,
) -> str:
    safe_name = Path(original_name).name
    return _default_scan_storage().generate_destination(
        f"{SCANNING_NAMESPACE}/{scan_job_id}/{logical_sequence_number:04d}-{side}-v{version}",
        safe_name,
    )


def _append_version(
    *,
    page: ScanPage,
    uploader_id: int | None,
    staged_path: str,
    checksum_sha256: str,
    mime_type: str,
    size_bytes: int,
    capture_timestamp: datetime | None,
    scanner_metadata: dict[str, Any],
) -> ScanPageVersion:
    latest = page.versions.order_by("version").last()
    next_version = (latest.version + 1) if latest is not None else 1
    storage = _default_scan_storage()
    destination = _build_page_storage_destination(
        scan_job_id=str(page.scan_job_id),
        logical_sequence_number=page.logical_sequence_number,
        side=page.side,
        version=next_version,
        original_name=Path(staged_path).name,
    )
    storage.store(staged_path, destination)
    size = size_bytes
    if size <= 0:
        size = Path(staged_path).stat().st_size
    page.versions.filter(is_active=True).update(is_active=False)
    return ScanPageVersion.objects.create(
        scan_page=page,
        storage_location=_default_scan_storage_location(),
        storage_identifier=destination,
        checksum_sha256=checksum_sha256,
        mime_type=mime_type,
        size_bytes=size,
        capture_timestamp=capture_timestamp,
        scanner_metadata=scanner_metadata,
        version=next_version,
        uploader_id=uploader_id,
        original_filename=Path(staged_path).name,
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
) -> tuple[ScanPage, ScanPageVersion, bool]:
    staged_path, size_bytes = _stage_scan_upload(uploaded_file)
    try:
        checksum_sha256 = intake.compute_sha256(staged_path)
        if requested_checksum is not None and requested_checksum.lower() != checksum_sha256.lower():
            raise ValueError("checksum mismatch")

        with transaction.atomic():
            locked_job = ScanJob.objects.select_for_update().get(pk=job.pk)
            if locked_job.state not in {
                ScanJob.State.OPEN,
                ScanJob.State.CAPTURING,
                ScanJob.State.PAUSED,
            }:
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
            if existing_version is not None:
                if not existing_version.is_active:
                    page.versions.filter(is_active=True).update(is_active=False)
                    existing_version.is_active = True
                    existing_version.save(update_fields=["is_active", "updated_at"])
                page.refresh_from_db()
                return page, existing_version, False

            version = _append_version(
                page=page,
                uploader_id=uploader_id,
                staged_path=staged_path,
                checksum_sha256=checksum_sha256,
                mime_type=getattr(uploaded_file, "content_type", "application/octet-stream"),
                size_bytes=size_bytes,
                capture_timestamp=capture_timestamp,
                scanner_metadata=scanner_metadata,
            )
            scantailor.mark_state_stale(
                scan_job_id=str(page.scan_job_id),
                page_id=str(page.id),
                reason="source changed",
            )
            scan_pipeline.invalidate_scan_job_library_artifacts(page.scan_job)
            return page, version, True
    finally:
        _cleanup_staged_upload(staged_path)


def _stage_scan_page_from_existing_version(page: ScanPage) -> tuple[str, int]:
    active_version = page.active_version()
    if active_version is None:
        raise ValueError("no active page version")
    storage = _default_scan_storage()
    with storage.open(active_version.storage_identifier) as stream:
        staged_path, size_bytes = _stage_scan_upload(
            ContentFile(stream.read(), name=active_version.original_filename or "scan-page.bin"),
        )
    return staged_path, size_bytes


def _active_library_profile(request: HttpRequest) -> AppUserProfile | None:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return None
    return profile


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
    storage = LocalFilesystemStorage(str(django_settings.APP_DATA_DIR))
    try:
        storage_root = storage.resolve_location("")
        storage_root.mkdir(parents=True, exist_ok=True)
        return DependencyResult(name="storage", status="ok", details={"root": str(storage_root)})
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
    client = redis.Redis.from_url(queue_url)
    try:
        client.ping()
        return DependencyResult(name="queue", status="ok")
    except Exception as exc:  # pragma: no cover - infrastructure-specific failures
        return DependencyResult(name="queue", status="down", details={"error": str(exc)})


def _profile_for_request(request: HttpRequest) -> AppUserProfile | None:
    if not request.user.is_authenticated:
        return None
    try:
        return request.user.library_profile
    except AppUserProfile.DoesNotExist:
        return None


def _json_body(request: HttpRequest) -> dict[str, Any]:
    if not request.body:
        return {}
    try:
        payload = json.loads(request.body.decode("utf-8"))
    except (TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return {}
    if isinstance(payload, dict):
        return payload
    return {}


def _request_payload(request: HttpRequest) -> dict[str, Any]:
    payload = _json_body(request)
    if payload:
        return payload
    return {key: value for key, value in request.POST.items()}


def _wants_html(request: HttpRequest) -> bool:
    if request.GET.get("view") == "html":
        return True
    return "text/html" in request.headers.get("Accept", "").lower()


def _can_manage_review_task(profile: AppUserProfile, task: ReviewTask) -> bool:
    if profile.role == "admin":
        return True
    if task.import_job_id is None:
        return False
    return task.import_job.uploader_id == profile.user_id


def _serialize_task(task: ReviewTask) -> dict[str, Any]:
    uploader_id = task.import_job.uploader_id if task.import_job_id else None
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
        "uploader_id": uploader_id,
        "reviewer_id": task.reviewer_id,
        "resolved_book_id": str(task.resolved_book_id) if task.resolved_book_id else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


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


def _build_manual_book_from_payload(payload: dict[str, Any]) -> Book:
    from . import identification

    raw_title = payload.get("title")
    if not isinstance(raw_title, str):
        raise ValueError("title required")
    title = raw_title.strip()
    if not title:
        raise ValueError("title required")

    raw_authors = payload.get("authors", [])
    if isinstance(raw_authors, str):
        authors = [entry.strip() for entry in raw_authors.split(",") if entry.strip()]
    elif isinstance(raw_authors, list):
        authors = [str(name).strip() for name in raw_authors if str(name).strip()]
    else:
        authors = []

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
        recent_books = Book.objects.prefetch_related("authors", "file_variants").order_by(
            "-created_at",
        )[:12]
        return render(
            request,
            "core/library_home.html",
            {
                "profile": profile,
                "recent_books": [
                    _serialize_book(book, include_variants=False) for book in recent_books
                ],
                "book_count": Book.objects.count(),
                "variant_count": FileVariant.objects.filter(
                    processing_state=FileVariant.ProcessingState.COMPLETE,
                ).count(),
            },
        )
    return JsonResponse(
        {"status": "ok", "user": request.user.username, "role": profile.role},
    )


def _serialize_book(
    book: Book,
    *,
    include_variants: bool = False,
    include_conversion_options: bool = False,
) -> dict[str, Any]:
    book_payload = {
        "id": book.id,
        "title": book.title,
        "subtitle": book.subtitle,
        "publisher": book.publisher,
        "edition": book.edition,
        "language": book.language,
        "description": book.description,
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
    if book.back_cover_id and book.back_cover is not None:
        book_payload["cover_back"] = _serialize_cover(book.back_cover)
    if include_variants:
        file_variants = list(
            book.file_variants.filter(
                processing_state=FileVariant.ProcessingState.COMPLETE,
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
            }
            for file_variant in file_variants
        ]
        book_payload["variants"] = variants
        book_payload["formats_available"] = sorted(
            {file_variant.format for file_variant in file_variants}
        )
    else:
        book_payload["formats_available"] = sorted(
            {
                variant.format
                for variant in book.file_variants.filter(
                    processing_state=FileVariant.ProcessingState.COMPLETE,
                )
            }
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
        "url": f"/library/books/{cover.book_id}/covers/{cover.position}/",
    }


def _as_positive_int(raw: str | None, default: int, minimum: int = 1) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value


@require_GET
@login_required
def library_books(request: HttpRequest) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    query = request.GET.get("q", "").strip()
    title = request.GET.get("title", "").strip()
    author = request.GET.get("author", "").strip()
    category = request.GET.get("category", "").strip()
    genre = request.GET.get("genre", "").strip()
    language = request.GET.get("language", "").strip()
    book_format = request.GET.get("format", "").strip()
    sort = request.GET.get("sort", "-created_at").strip()

    books = Book.objects.all().prefetch_related(
        "authors",
        "categories",
        "genres",
        "identifiers",
        "file_variants",
    )

    if query:
        books = books.filter(
            Q(title__icontains=query)
            | Q(subtitle__icontains=query)
            | Q(description__icontains=query)
            | Q(authors__full_name__icontains=query),
        )
    if title:
        books = books.filter(title__icontains=title)
    if author:
        books = books.filter(authors__full_name__icontains=author)
    if category:
        books = books.filter(categories__name__icontains=category)
    if genre:
        books = books.filter(genres__name__icontains=genre)
    if language:
        books = books.filter(language__iexact=language)
    if book_format:
        books = books.filter(
            file_variants__format__iexact=book_format,
            file_variants__processing_state=FileVariant.ProcessingState.COMPLETE,
        )

    sort_map = {
        "created_at": "created_at",
        "-created_at": "-created_at",
        "title": "title",
        "-title": "-title",
    }
    books = books.distinct().order_by(sort_map.get(sort, "-created_at"))

    page = _as_positive_int(request.GET.get("page"), 1)
    page_size = _as_positive_int(request.GET.get("page_size"), 25)
    page_size = min(page_size, 100)
    paginator = Paginator(books, page_size)
    page_object = paginator.get_page(page)
    page_books = list(page_object.object_list)
    total = paginator.count
    serialized = [_serialize_book(book, include_variants=False) for book in page_books]
    if _wants_html(request):
        return render(
            request,
            "core/library_books.html",
            {
                "books": serialized,
                "page_object": page_object,
                "count": total,
                "filters": {
                    "q": query,
                    "title": title,
                    "author": author,
                    "category": category,
                    "genre": genre,
                    "language": language,
                    "format": book_format,
                    "sort": sort,
                },
            },
        )
    return JsonResponse(
        {
            "count": total,
            "page": page,
            "page_size": len(serialized),
            "items": serialized,
        },
    )


@require_GET
@login_required
def library_book_detail(request: HttpRequest, book_id: int) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        book = Book.objects.prefetch_related(
            "authors",
            "categories",
            "genres",
            "identifiers",
            "file_variants",
            "front_cover",
            "back_cover",
        ).get(pk=book_id)
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
        book = Book.objects.prefetch_related("file_variants__storage_location").get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    variant = None
    variant_id = request.GET.get("variant_id")
    requested_format = request.GET.get("format", "").strip()
    if variant_id:
        try:
            variant = book.file_variants.get(
                pk=int(variant_id),
                processing_state=FileVariant.ProcessingState.COMPLETE,
            )
        except (ValueError, FileVariant.DoesNotExist):
            return HttpResponseBadRequest("variant_id invalid")
    elif requested_format:
        variant = (
            book.file_variants.filter(
                format__iexact=requested_format,
                processing_state=FileVariant.ProcessingState.COMPLETE,
            )
            .order_by("id")
            .first()
        )
    if variant is None and requested_format:
        return HttpResponseNotFound("requested format not available")
    if variant is None:
        variant = book.file_variants.filter(
            processing_state=FileVariant.ProcessingState.COMPLETE,
        ).first()

    if variant is None:
        return HttpResponseNotFound("variant not available")

    try:
        storage = backend_for_location(variant.storage_location)
    except StorageConfigurationError:
        return HttpResponse("variant storage unavailable", status=503)
    try:
        stream = storage.open(variant.storage_identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("variant missing")

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
        return job.state in {ScanJob.State.OPEN, ScanJob.State.CAPTURING, ScanJob.State.PAUSED}
    if target_state == ScanJob.State.PAUSED:
        return job.state in {ScanJob.State.OPEN, ScanJob.State.CAPTURING}
    if target_state == ScanJob.State.CAPTURING:
        return job.state in {ScanJob.State.OPEN, ScanJob.State.PAUSED}
    if target_state == ScanJob.State.COMPLETE:
        return job.state in {ScanJob.State.OPEN, ScanJob.State.CAPTURING, ScanJob.State.PAUSED}
    return False


def _apply_scan_job_transition(job: ScanJob, target_state: str) -> tuple[bool, str]:
    if target_state not in dict(ScanJob.State.choices):
        return False, "invalid state"
    if not _scan_job_transition_allowed(job, target_state):
        return False, "transition not allowed"
    job.state = target_state
    if target_state in {ScanJob.State.COMPLETE, ScanJob.State.CANCELLED}:
        job.finished_at = timezone.now()
    job.save(update_fields=["state", "finished_at", "updated_at"])
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


def _scan_workspace_page_payload(job: ScanJob, page: ScanPage) -> dict[str, Any]:
    _ensure_workspace_stage_states(job, page)
    return _serialize_workspace_page(page)


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
        return any(
            key in parameters
            for key in {
                "text_box",
                "picture_box",
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
        state.parameters = merged

    _bind_stage_inputs(state, page)
    state.revision += 1
    state.last_error = ""
    state.status = ScanTailorStageState.StageStatus.QUEUED


@require_GET
@login_required
def scan_job_workspace(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(job_id) is None:
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

    pages = []
    for page in job.pages.all().order_by("logical_sequence_number", "side"):
        _ensure_workspace_stage_states(job, page)
        pages.append(_serialize_workspace_page(page))
    payload = {
        "job_id": str(job.id),
        "state": job.state,
        "pages": pages,
        "count": len(pages),
        "stage_order": list(scantailor.stage_order()),
    }
    if _wants_html(request):
        return render(
            request,
            "core/scan_workspace.html",
            {"job": job, "pages": pages, "stage_order": scantailor.stage_order()},
        )
    return JsonResponse(payload)


@require_GET
@login_required
def scan_page_stages(
    request: HttpRequest,
    job_id: str,
    page_id: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    del request
    if _coerce_uuid(job_id) is None or _coerce_uuid(page_id) is None:
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
@login_required
def scan_page_stage(
    request: HttpRequest,
    job_id: str,
    page_id: str,
    stage: str,
) -> HttpResponseBase:
    if _coerce_uuid(job_id) is None or _coerce_uuid(page_id) is None:
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

    if page.scan_job.state not in {
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
    }:
        return HttpResponseBadRequest("scan job is not accepting processing changes")

    payload = _request_payload(request)
    expected_revision = _coerce_int(payload.get("expected_revision"), minimum=1)
    if expected_revision is not None and expected_revision != state.revision:
        return _conflict_response(state, expected_revision)

    _apply_stage_payload_to_state(state, page, normalized_stage, payload)
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
@login_required
def scan_job_stage_batch(
    request: HttpRequest,
    job_id: str,
    stage: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    if _coerce_uuid(job_id) is None:
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
    if job.state not in {
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
    }:
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
        if preserve_exceptions and _is_manual_stage_state(state):
            skipped.append(str(page.id))
            continue

        _apply_stage_payload_to_state(state, page, normalized_stage, payload)
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
@login_required
def scan_page_stage_preview(
    request: HttpRequest,
    job_id: str,
    page_id: str,
    stage: str,
) -> HttpResponseBase:
    del request
    if _coerce_uuid(job_id) is None or _coerce_uuid(page_id) is None:
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

    identifier = state.preview_storage_identifier or state.output_storage_identifier
    if not identifier:
        return HttpResponseNotFound("preview not ready")
    storage = _default_scan_storage()
    try:
        stream = storage.open(identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("preview artifact missing")
    return FileResponse(stream, as_attachment=False, filename=Path(identifier).name)


@require_http_methods(["GET", "POST"])
@login_required
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
    scanner_id = _coerce_str_or_none(payload.get("scanner_id")) or ""
    book_id = _coerce_int(payload.get("book_id"), minimum=1)
    if book_id is not None:
        try:
            book = Book.objects.get(pk=book_id)
        except Book.DoesNotExist:
            return HttpResponseBadRequest("book not found")
    else:
        book = None

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
        notes=notes,
    )
    if _wants_html(request):
        return redirect("scan-job-workspace", job_id=job.id)
    return JsonResponse(_scan_job_payload(job), status=201)


@require_GET
@login_required
def scan_job_detail(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    del request
    if _coerce_uuid(job_id) is None:
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
    return JsonResponse(_scan_job_payload(job))


@require_GET
@login_required
def scan_job_todos(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    if _coerce_uuid(job_id) is None:
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
@login_required
def scan_job_ocr_request(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseBadRequest | HttpResponseNotFound | HttpResponseForbidden:
    if _coerce_uuid(job_id) is None:
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


@require_GET
@login_required
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
@login_required
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
    if page.scan_job.state not in {
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
    }:
        return HttpResponseBadRequest("scan job is not accepting rescan requests")

    payload = _scan_page_request_payload(request)
    reason = _coerce_str_or_none(payload.get("reason")) or ""

    existing_todo = (
        page.todos.filter(
            todo_type=ScanTodo.TodoType.RESCAN,
            state=ScanTodo.State.OPEN,
        )
        .order_by("-created_at")
        .first()
    )
    if existing_todo is not None:
        if _wants_html(request):
            return redirect("scan-job-workspace", job_id=page.scan_job_id)
        return JsonResponse(
            {
                "todo": _serialize_scan_todo(existing_todo),
                "created": False,
                "scan_job_id": str(page.scan_job_id),
            },
            status=200,
        )

    todo = ScanTodo.objects.create(
        scan_job=page.scan_job,
        scan_page=page,
        todo_type=ScanTodo.TodoType.RESCAN,
        reason=reason,
        creator=profile.user,
    )
    if _wants_html(request):
        return redirect("scan-job-workspace", job_id=page.scan_job_id)
    return JsonResponse(
        {
            "todo": _serialize_scan_todo(todo),
            "created": True,
            "scan_job_id": str(page.scan_job_id),
        },
        status=201,
    )


@require_POST
@login_required
def upload_scan_page(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(job_id) is None:
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
    capture_timestamp = _parse_capture_ts(payload.get("capture_timestamp"))
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
@login_required
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
    uploaded_file: Any = request.FILES.get("file")
    if uploaded_file is None:
        if request.content_type == "application/octet-stream":
            legacy_payload = _parse_legacy_scan_put_payload(request.body)
            if not legacy_payload:
                return HttpResponseBadRequest("missing_file")
            active_version = page.active_version()
            if active_version is None:
                return HttpResponseBadRequest("missing_file")
            storage = _default_scan_storage()
            try:
                with storage.open(active_version.storage_identifier) as source_stream:
                    source_payload = source_stream.read()
            except FileNotFoundError:
                return HttpResponseBadRequest("missing_file")
            uploaded_file = ContentFile(source_payload, name=active_version.original_filename)
            requested_checksum = _coerce_str_or_none(legacy_payload.get("checksum_sha256"))
        else:
            return HttpResponseBadRequest("missing_file")

    capture_timestamp = _parse_capture_ts(payload.get("capture_timestamp"))
    scanner_metadata = _normalize_scanner_metadata(payload.get("scanner_metadata"))
    job = page.scan_job

    force_new_version = False
    if request.content_type == "application/octet-stream" and request.body:
        legacy_payload = _parse_legacy_scan_put_payload(request.body)
        if legacy_payload:
            force_new_version = True
            # Legacy compatibility mode cannot verify content hash when body is form-string-encoded.
            requested_checksum = None

    if job.state not in {
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
    }:
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
            force_new_version=force_new_version,
        )
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

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
@login_required
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
@login_required
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
@login_required
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
@login_required
def scan_page_thumbnail(
    request: HttpRequest,
    page_id: str,
) -> HttpResponseBase:
    del request
    if _coerce_uuid(page_id) is None:
        return HttpResponseBadRequest("invalid page id")
    try:
        page = ScanPage.objects.get(pk=page_id)
    except (ScanPage.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    version = page.active_version()
    if version is None:
        return HttpResponseNotFound("no page version")
    storage = _default_scan_storage()
    try:
        stream = storage.open(version.storage_identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("artifact missing")

    response = FileResponse(stream, as_attachment=False, filename=version.original_filename)
    response["Content-Type"] = version.mime_type or "application/octet-stream"
    return response


@require_POST
@login_required
def scan_job_pause(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseNotFound | HttpResponseBadRequest | HttpResponseForbidden:
    del request
    if _coerce_uuid(job_id) is None:
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
@login_required
def scan_job_resume(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseNotFound | HttpResponseBadRequest | HttpResponseForbidden:
    del request
    if _coerce_uuid(job_id) is None:
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
@login_required
def scan_job_finish(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ScanJob.objects.get(pk=job_id)
    except (ScanJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()

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
        return HttpResponseBadRequest(str(error))

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
@login_required
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
@login_required
def scan_job_cancel(
    request: HttpRequest,
    job_id: str,
) -> JsonResponse | HttpResponseNotFound | HttpResponseBadRequest | HttpResponseForbidden:
    del request
    if _coerce_uuid(job_id) is None:
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


@require_http_methods(["GET", "POST"])
@login_required
def upload_file(request: HttpRequest) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    if request.method == "GET":
        return render(
            request,
            "core/upload.html",
            {"max_upload_bytes": django_settings.BOOK_SCANNER_MAX_UPLOAD_BYTES},
        )

    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return HttpResponseBadRequest("missing_file")
    original_name = uploaded_file.name
    if not original_name:
        return HttpResponseBadRequest("filename is required")

    try:
        normalized_filename = intake.normalize_filename(original_name)
    except intake.ValidationError as exc:
        return HttpResponseBadRequest(str(exc))
    try:
        detected_format, detected_mime = intake.detect_format(
            original_name,
            uploaded_file.content_type,
        )
    except intake.ValidationError as exc:
        return HttpResponseBadRequest(str(exc))

    staging_root = Path(django_settings.APP_DATA_DIR) / "uploads"
    try:
        source_path, size_bytes = intake.write_upload_to_staging(uploaded_file, staging_root)
    except intake.ValidationError as exc:
        return HttpResponseBadRequest(str(exc))

    job = ImportJob.objects.create(
        source_type=ImportJob.SourceType.UPLOAD,
        state=ImportJob.State.DISCOVERED,
        uploader=profile.user,
        source_path=source_path,
        original_filename=original_name,
        normalized_filename=normalized_filename,
        detected_format=detected_format,
        mime_type=detected_mime,
        size_bytes=size_bytes,
    )
    try:
        enqueue(intake.process_import_job, str(job.id))
    except Exception as exc:  # pragma: no cover - transport/path specific
        return HttpResponseBadRequest(str(exc))
    if _wants_html(request):
        return redirect("imports-status", job_id=job.id)
    return JsonResponse({"job_id": str(job.id), "state": job.state}, status=201)


@require_GET
@login_required
def import_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    try:
        job = ImportJob.objects.get(pk=job_id)
    except ImportJob.DoesNotExist:
        return HttpResponseNotFound()
    payload = {
        "job_id": str(job.id),
        "state": job.state,
        "filename": job.original_filename,
        "normalized_filename": job.normalized_filename,
        "sha256": job.sha256,
        "error": job.last_error,
        "book_id": str(job.book_id) if job.book_id else None,
        "file_variant_id": str(job.file_variant_id) if job.file_variant_id else None,
        "storage_identifier": job.storage_identifier,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }
    if _wants_html(request):
        return render(request, "core/import_status.html", {"job": job, "payload": payload})
    return JsonResponse(payload)


@require_GET
@login_required
def review_queue(request: HttpRequest) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    tasks = ReviewTask.objects.select_related(
        "import_job",
        "import_job__uploader",
    ).filter(state__in=[ReviewTask.State.OPEN, ReviewTask.State.CLAIMED])
    if profile.role != "admin":
        tasks = tasks.filter(import_job__uploader=profile.user)
    tasks = tasks.order_by("-created_at")

    payload = [
        {
            **_serialize_task(task),
            "candidate_count": task.import_job.metadata_candidates.count(),
            "candidates": [
                _serialize_candidate(candidate)
                for candidate in task.import_job.metadata_candidates.order_by("-score")
            ],
        }
        for task in tasks
    ]
    if _wants_html(request):
        return render(
            request,
            "core/review_queue.html",
            {"tasks": list(tasks), "items": payload, "count": len(payload)},
        )
    return JsonResponse({"items": payload, "count": len(payload)})


@require_GET
@login_required
def review_detail(request: HttpRequest, task_id: str) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    try:
        task = ReviewTask.objects.select_related("import_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    payload = _serialize_task(task)
    payload["candidates"] = [
        _serialize_candidate(candidate)
        for candidate in task.import_job.metadata_candidates.order_by("-score")
    ]
    if _wants_html(request):
        return render(
            request,
            "core/review_detail.html",
            {
                "task": task,
                "payload": payload,
                "candidates": task.import_job.metadata_candidates.order_by("-score"),
                "books": Book.objects.order_by("title")[:250],
            },
        )
    return JsonResponse(payload)


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
        task = ReviewTask.objects.select_related("import_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    payload = _request_payload(request)
    candidate_id = _coerce_int(payload.get("candidate_id"), minimum=1)
    if candidate_id is None:
        return HttpResponseBadRequest("candidate_id required")

    try:
        candidate = MetadataCandidate.objects.get(pk=candidate_id, import_job=task.import_job)
    except MetadataCandidate.DoesNotExist:
        return HttpResponseBadRequest("candidate not found")

    try:
        candidate_isbn13 = candidate.isbn13 or None
        if candidate_isbn13:
            existing_identifier = (
                Identifier.objects.filter(
                    value=candidate_isbn13,
                )
                .select_related("book")
                .first()
            )
            if existing_identifier is not None:
                book = existing_identifier.book
            else:
                book = None
        else:
            book = None

        if book is None:
            candidate_isbn10 = candidate.isbn10 or None
            if candidate_isbn10:
                existing_identifier = (
                    Identifier.objects.filter(
                        value=candidate_isbn10,
                    )
                    .select_related("book")
                    .first()
                )
                if existing_identifier is not None:
                    book = existing_identifier.book

        if book is None:
            candidate_author_set = {
                name.lower() for name in candidate.authors if isinstance(name, str)
            }
            for existing_book in Book.objects.filter(title__iexact=candidate.title):
                known_authors = [author.full_name.lower() for author in existing_book.authors.all()]
                if candidate_author_set.intersection(known_authors):
                    book = existing_book
                    break

        if book is None:
            candidate_edition = getattr(candidate, "edition", "")
            book = Book.objects.create(
                title=candidate.title,
                publisher=candidate.publisher or "",
                language=candidate.language or "",
                description=candidate.description or "",
                edition=candidate_edition or "",
            )
            if candidate.publication_year:
                try:
                    from datetime import date

                    book.publication_date = date(candidate.publication_year, 1, 1)
                except ValueError:
                    pass
            for author_name in candidate.authors:
                if not isinstance(author_name, str):
                    continue
                author, _ = Author.objects.get_or_create(full_name=author_name.strip())
                if author_name.strip():
                    book.authors.add(author)
            if candidate.isbn10:
                Identifier.objects.get_or_create(
                    book=book,
                    kind=Identifier.IdentifierType.ISBN10,
                    value=candidate.isbn10,
                    defaults={"normalized_value": candidate.isbn10},
                )
            if candidate.isbn13:
                Identifier.objects.get_or_create(
                    book=book,
                    kind=Identifier.IdentifierType.ISBN13,
                    value=candidate.isbn13,
                    defaults={"normalized_value": candidate.isbn13},
                )
            book.save()

        variant = intake.resolve_import_with_review_data(
            job=task.import_job,
            review_task=task,
            book=book,
            source_type=cast(str, FileVariant.SourceType.UPLOAD),
            selected_candidate=candidate,
            reviewer=profile.user,
            decision=cast(str, ReviewTask.Decision.ACCEPT_CANDIDATE),
            decision_notes=f"Accepted candidate {candidate.id}",
        )
        return _review_action_response(
            request,
            {
                "task_id": str(task.id),
                "job_id": str(task.import_job_id),
                "book_id": str(book.id),
                "file_variant_id": str(variant.id),
            },
        )
    except Exception as exc:  # pragma: no cover
        return HttpResponseBadRequest(str(exc))


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
        task = ReviewTask.objects.select_related("import_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
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
    return _review_action_response(request, {"task_id": str(task.id), "state": task.state})


@require_POST
@login_required
def review_search_again(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    payload = _request_payload(request)
    try:
        task = ReviewTask.objects.select_related("import_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    task.state = ReviewTask.State.CLAIMED
    task.decision = ReviewTask.Decision.SEARCH_AGAIN
    task.reviewer = profile.user
    task.decision_notes = str(payload.get("notes", ""))
    task.decided_at = timezone.now()
    task.decision_payload = {"search_again": True}
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
    return _review_action_response(
        request,
        {"task_id": str(task.id), "state": task.state},
        task_id=task.id,
    )


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
        task = ReviewTask.objects.select_related("import_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    inferred_title = payload.get("inferred_title")
    if isinstance(inferred_title, str) and inferred_title.strip():
        task.inferred_title = inferred_title.strip()

    raw_inferred_authors = payload.get("inferred_authors")
    if isinstance(raw_inferred_authors, str):
        task.inferred_authors = [
            author.strip() for author in raw_inferred_authors.split(",") if author.strip()
        ]
    elif isinstance(raw_inferred_authors, list):
        task.inferred_authors = [
            str(author).strip() for author in raw_inferred_authors if str(author).strip()
        ]

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
        task = ReviewTask.objects.select_related("import_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    payload = _request_payload(request)
    if not isinstance(payload.get("title"), str):
        return HttpResponseBadRequest("title required")
    book = _build_manual_book_from_payload(payload)

    variant = intake.resolve_import_with_review_data(
        job=task.import_job,
        review_task=task,
        book=book,
        source_type=cast(str, FileVariant.SourceType.UPLOAD),
        reviewer=profile.user,
        decision=cast(str, ReviewTask.Decision.MANUAL_ENTRY),
        decision_payload=payload,
    )
    return _review_action_response(
        request,
        {
            "task_id": str(task.id),
            "job_id": str(task.import_job_id),
            "book_id": str(book.id),
            "variant_id": str(variant.id),
        },
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
        task = ReviewTask.objects.select_related("import_job").get(pk=task_id)
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

    intake.resolve_import_with_review_data(
        job=task.import_job,
        review_task=task,
        book=book,
        source_type=cast(str, FileVariant.SourceType.UPLOAD),
        reviewer=profile.user,
        decision=cast(str, ReviewTask.Decision.MERGED_BOOK),
        decision_payload={"book_id": book_id},
    )
    return _review_action_response(
        request,
        {"task_id": str(task.id), "book_id": str(book.id)},
    )


def _scan_page_request_payload_for_multipart(request: HttpRequest) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in request.POST.items():
        result[key] = value
    return result
