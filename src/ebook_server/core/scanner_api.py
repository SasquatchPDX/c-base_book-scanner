"""Scanner device API v1 (``/api/scanner/v1/``) for the libreflip scanner (``sans``).

The canonical contract is libreflip/serif ``docs/sans-serif-interface.md``; "§" below
refers to it. Every request authenticates as a :class:`ScannerDevice` with
``Authorization: Bearer <key>`` (§3, D9). Sessions and cookies play no part: a browser
session without a valid key gets ``401``, and the views are CSRF-exempt because nothing
here is cookie-authenticated. Device keys are only ever checked in this module, so they
authorise nothing outside this namespace.

Errors always use the envelope ``{"error": {"code", "message", "details"}}`` (§6.1).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Final

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from .models import ScanJob, ScannerDevice
from .scan_job_ids import is_device_job_id
from .scanner_devices import authenticate_key, bearer_token

API_VERSION: Final = 1
# §6.12: event batches are fixed by the contract rather than configured per deployment.
MAX_EVENT_BATCH_BYTES: Final = 1024 * 1024
MAX_EVENT_BATCH_LINES: Final = 1000

DeviceView = Callable[..., JsonResponse]


def iso_utc(value: datetime) -> str:
    """ISO 8601 UTC with a ``Z`` suffix and whole seconds (§6.1)."""
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def error_response(
    status: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    *,
    headers: dict[str, str] | None = None,
) -> JsonResponse:
    response = JsonResponse(
        {"error": {"code": code, "message": message, "details": details or {}}},
        status=status,
    )
    for name, value in (headers or {}).items():
        response[name] = value
    response["Cache-Control"] = "no-store"
    return response


def _unauthorized(code: str, message: str) -> JsonResponse:
    return error_response(
        401, code, message, headers={"WWW-Authenticate": 'Bearer realm="scanner"'}
    )


def _bearer_token(request: HttpRequest) -> str | None:
    return bearer_token(request.headers.get("Authorization", ""))


def scanner_api_view(methods: Iterable[str]) -> Callable[[DeviceView], Callable[..., JsonResponse]]:
    """Wrap a device view: API switch, Bearer authentication, method check.

    The wrapped view is called as ``view(request, device, *args, **kwargs)``.
    """
    allowed = tuple(method.upper() for method in methods)

    def decorator(view: DeviceView) -> Callable[..., JsonResponse]:
        @csrf_exempt
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
            if not getattr(settings, "BOOK_SCANNER_SCANNER_API_ENABLED", True):
                return error_response(
                    503, "scanner_api_disabled", "The scanner API is disabled on this server."
                )
            token = _bearer_token(request)
            if token is None:
                return _unauthorized(
                    "authentication_required", "Send the device key as Authorization: Bearer."
                )
            device = authenticate_key(token)
            if device is None:
                return _unauthorized("invalid_api_key", "The device key is invalid or revoked.")
            seen_at = timezone.now()
            ScannerDevice.objects.filter(pk=device.pk).update(last_seen_at=seen_at)
            device.last_seen_at = seen_at
            if request.method not in allowed:
                response = error_response(
                    405,
                    "method_not_allowed",
                    f"{request.method} is not allowed here.",
                    {"allowed": list(allowed)},
                )
                response["Allow"] = ", ".join(allowed)
                return response
            response = view(request, device, *args, **kwargs)
            response.setdefault("Cache-Control", "no-store")
            return response

        return wrapper

    return decorator


def limits() -> dict[str, int]:
    return {
        "max_image_bytes": int(settings.BOOK_SCANNER_SCANNER_MAX_IMAGE_BYTES),
        "max_opf_bytes": int(settings.BOOK_SCANNER_SCANNER_MAX_OPF_BYTES),
        "max_event_batch_bytes": MAX_EVENT_BATCH_BYTES,
        "max_event_batch_lines": MAX_EVENT_BATCH_LINES,
    }


@scanner_api_view(["GET"])
def ping(request: HttpRequest, device: ScannerDevice) -> JsonResponse:
    """§6.2: reachability, authentication and clock check."""
    return JsonResponse(
        {
            "api_version": API_VERSION,
            "scanner_id": device.scanner_id,
            "server_time": iso_utc(timezone.now()),
            "limits": limits(),
        }
    )


# --- jobs (§6.3/§6.4) -------------------------------------------------------------------

MAX_JOB_REQUEST_BYTES: Final = 16 * 1024


def _invalid_job_id(job_id: str) -> JsonResponse:
    return error_response(
        400,
        "invalid_job_id",
        "The job ID must match YYYYMMDDTHHMMSSZ[-N].",
        {"job_id": job_id[:80]},
    )


def portal_url(request: HttpRequest, job_id: str) -> str:
    """Link to the job's portal page (``/j/<job_id>``, contract §9)."""
    base = str(getattr(settings, "BOOK_SCANNER_PUBLIC_BASE_URL", "") or "")
    if base:
        return f"{base}j/{job_id}"
    return request.build_absolute_uri(f"/j/{job_id}")


def job_status_payload(request: HttpRequest, job: ScanJob) -> dict[str, Any]:
    """Job status as in §6.4, limited to what exists so far.

    Covers, ``metadata_opf`` and ``events`` are added by the tickets that store them.
    """
    pages = []
    for page in job.pages.order_by("logical_sequence_number", "side"):
        active = page.versions.filter(is_active=True).first()
        if active is None:
            continue
        pages.append(
            {
                "sequence_number": page.logical_sequence_number,
                "sha256": active.checksum_sha256,
                "version": active.version,
            }
        )
    return {
        "job_id": job.pk,
        "state": job.state,
        "portal_url": portal_url(request, job.pk),
        "scan_started_at": iso_utc(job.scan_started_at) if job.scan_started_at else None,
        "capture_layout": job.capture_layout,
        "pages": pages,
        "open_rescans": job.todos.filter(state__in=["open", "claimed"], todo_type="rescan").count(),
    }


def _parse_registration(request: HttpRequest) -> tuple[dict[str, Any] | None, JsonResponse | None]:
    if len(request.body) > MAX_JOB_REQUEST_BYTES:
        return None, error_response(413, "payload_too_large", "The request body is too large.")
    try:
        payload = json.loads(request.body or b"null")
    except (UnicodeDecodeError, ValueError):
        return None, error_response(400, "invalid_json", "The body must be a JSON object.")
    if not isinstance(payload, dict):
        return None, error_response(400, "invalid_json", "The body must be a JSON object.")
    raw_started = payload.get("scan_started_at")
    started = parse_datetime(raw_started) if isinstance(raw_started, str) else None
    if started is None or started.tzinfo is None:
        return None, error_response(
            400,
            "invalid_request",
            "scan_started_at must be an ISO 8601 timestamp with a time zone.",
            {"field": "scan_started_at"},
        )
    layout = payload.get("capture_layout", ScanJob.CaptureLayout.SINGLE_PAGE)
    if layout not in ScanJob.CaptureLayout.values:
        return None, error_response(
            400,
            "invalid_request",
            "capture_layout must be one of: " + ", ".join(ScanJob.CaptureLayout.values),
            {"field": "capture_layout"},
        )
    return {"scan_started_at": started.astimezone(UTC), "capture_layout": layout}, None


def _conflict(job_id: str) -> JsonResponse:
    return error_response(
        409,
        "job_id_conflict",
        "This job ID is already registered with other data.",
        {"job_id": job_id},
    )


@scanner_api_view(["PUT"])
def job(request: HttpRequest, device: ScannerDevice, job_id: str) -> JsonResponse:
    """Register a job (§6.3): 201 created, 200 identical repeat, 409 conflict.

    The same device repeating the same ``scan_started_at`` (and ``capture_layout``) gets
    ``200`` and changes nothing. Any other data for an existing ID, another device, or a
    web-created job with that ID is ``409 job_id_conflict``; nothing is overwritten.
    """
    if not is_device_job_id(job_id):
        return _invalid_job_id(job_id)
    registration, problem = _parse_registration(request)
    if problem is not None:
        return problem
    assert registration is not None
    for _attempt in range(2):
        try:
            with transaction.atomic():
                current = ScanJob.objects.select_for_update().filter(pk=job_id).first()
                if current is None:
                    created = ScanJob.objects.create(
                        id=job_id,
                        state=ScanJob.State.OPEN,
                        device=device,
                        scanner_id=device.scanner_id,
                        operator=None,
                        **registration,
                    )
                    return JsonResponse(job_status_payload(request, created), status=201)
                same = (
                    current.device_id == device.pk
                    and current.scan_started_at == registration["scan_started_at"]
                    and current.capture_layout == registration["capture_layout"]
                )
                if not same:
                    return _conflict(job_id)
                return JsonResponse(job_status_payload(request, current), status=200)
        except IntegrityError:
            # A concurrent first registration won the insert; re-read it.
            continue
    return _conflict(job_id)


@scanner_api_view(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
def not_found(request: HttpRequest, device: ScannerDevice, path: str = "") -> JsonResponse:
    """Unknown routes under the namespace answer in the envelope, after authentication."""
    return error_response(404, "not_found", "No such scanner API resource.")
