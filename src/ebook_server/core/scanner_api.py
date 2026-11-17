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

from . import scan_lifecycle, scan_manifest
from .models import ScanJob, ScannerDevice, ScanTodo
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
    """Job status and received inventory (§6.4).

    The item sections come from :mod:`.scan_manifest`: ``pages`` now; ``covers`` and
    ``metadata_opf`` appear once their stores register a source (S4/S6). ``events``
    carries the stored-event high-water mark (``0`` until S5 stores events).
    """
    payload: dict[str, Any] = {
        "job_id": job.pk,
        "state": job.state,
        "portal_url": portal_url(request, job.pk),
        "scan_started_at": iso_utc(job.scan_started_at) if job.scan_started_at else None,
        "capture_layout": job.capture_layout,
    }
    payload.update(scan_manifest.status_sections(job))
    payload["open_rescans"] = job.todos.filter(
        state__in=[ScanTodo.State.OPEN, ScanTodo.State.CLAIMED],
        todo_type=ScanTodo.TodoType.RESCAN,
    ).count()
    if job.refinish_needed:
        payload["note"] = scan_lifecycle.REFINISH_NOTE
    return payload


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


def _not_found() -> JsonResponse:
    return error_response(404, "not_found", "No such job.")


def _device_job(device: ScannerDevice, job_id: str, *, lock: bool = False) -> ScanJob | None:
    """The job if this device registered it. Another device's or a web job is ``None``
    (answered ``404``, not ``403``, so job IDs cannot be probed, §6.1)."""
    jobs = ScanJob.objects.select_for_update() if lock else ScanJob.objects.all()
    return jobs.filter(pk=job_id, device=device).first()


def _job_state_error(job: ScanJob, message: str) -> JsonResponse:
    return error_response(409, "job_state", message, {"state": job.state})


@scanner_api_view(["GET", "PUT"])
def job(request: HttpRequest, device: ScannerDevice, job_id: str) -> JsonResponse:
    """``GET``: job status (§6.4). ``PUT``: register a job (§6.3)."""
    if not is_device_job_id(job_id):
        return _invalid_job_id(job_id)
    if request.method == "GET":
        current = _device_job(device, job_id)
        if current is None:
            return _not_found()
        return JsonResponse(job_status_payload(request, current))
    return _register_job(request, device, job_id)


def _register_job(request: HttpRequest, device: ScannerDevice, job_id: str) -> JsonResponse:
    """Register a job (§6.3): 201 created, 200 identical repeat, 409 conflict.

    The same device repeating the same ``scan_started_at`` (and ``capture_layout``) gets
    ``200`` and changes nothing. Any other data for an existing ID, another device, or a
    web-created job with that ID is ``409 job_id_conflict``; nothing is overwritten.
    """
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


# --- capture-complete (§6.9) and abort (§6.10) ------------------------------------------

# A manifest lists every page; 4 MiB holds tens of thousands of entries.
MAX_MANIFEST_BYTES: Final = 4 * 1024 * 1024


def _json_object(request: HttpRequest, limit: int) -> tuple[dict[str, Any], JsonResponse | None]:
    if len(request.body) > limit:
        return {}, error_response(413, "payload_too_large", "The request body is too large.")
    try:
        payload = json.loads(request.body or b"null")
    except (UnicodeDecodeError, ValueError):
        payload = None
    if not isinstance(payload, dict):
        return {}, error_response(400, "invalid_json", "The body must be a JSON object.")
    return payload, None


def _invalid_request(field_name: str, message: str) -> JsonResponse:
    return error_response(400, "invalid_request", message, {"field": field_name})


def _capture_verified(request: HttpRequest, job: ScanJob) -> JsonResponse:
    return JsonResponse(
        {"verified": True, "state": job.state, "portal_url": portal_url(request, job.pk)}
    )


@scanner_api_view(["POST"])
def capture_complete(request: HttpRequest, device: ScannerDevice, job_id: str) -> JsonResponse:
    """Verify the scanner's full manifest and move the job to ``captured`` (§6.9).

    ``200`` when every listed item is held with exactly that active checksum and the
    event high-water marks agree; repeating it (or calling it once the job got past
    ``captured``) answers ``200`` again. ``409 incomplete`` lists ``missing`` and
    ``mismatched`` item keys; ``409 job_state`` for a cancelled (or paused) job.
    """
    if not is_device_job_id(job_id):
        return _invalid_job_id(job_id)
    payload, problem = _json_object(request, MAX_MANIFEST_BYTES)
    if problem is not None:
        return problem
    raw_finished = payload.get("finished_at")
    finished_at = None
    if raw_finished is not None:
        finished_at = parse_datetime(raw_finished) if isinstance(raw_finished, str) else None
        if finished_at is None or finished_at.tzinfo is None:
            return _invalid_request(
                "finished_at", "finished_at must be an ISO 8601 timestamp with a time zone."
            )
    with transaction.atomic():
        current = _device_job(device, job_id, lock=True)
        if current is None:
            return _not_found()
        if current.state in scan_lifecycle.CAPTURE_VERIFIED_STATES:
            return _capture_verified(request, current)
        if current.state not in scan_lifecycle.CAPTURE_COMPLETE_FROM:
            return _job_state_error(
                current, f"capture-complete is not allowed for a {current.state} job."
            )
        try:
            check = scan_manifest.check_manifest(current, payload)
        except scan_manifest.ManifestError as exc:
            return _invalid_request(exc.field, str(exc))
        if not check.verified:
            return error_response(
                409,
                "incomplete",
                "Some items are missing or differ; upload them and call again.",
                {
                    "missing": check.missing,
                    "mismatched": check.mismatched,
                    "sans_last_line_no": check.sans_last_line_no,
                },
            )
        scan_lifecycle.mark_captured(
            current, finished_at=finished_at.astimezone(UTC) if finished_at else None
        )
    return _capture_verified(request, current)


@scanner_api_view(["POST"])
def abort(request: HttpRequest, device: ScannerDevice, job_id: str) -> JsonResponse:
    """Abort Book (§6.10): ``cancelled``, idempotent, uploaded data kept."""
    if not is_device_job_id(job_id):
        return _invalid_job_id(job_id)
    if _device_job(device, job_id) is None:
        return _not_found()
    try:
        aborted = scan_lifecycle.abort(job_id)
    except scan_lifecycle.LifecycleError as exc:
        return error_response(409, "job_state", str(exc), {"state": exc.state})
    return JsonResponse(job_status_payload(request, aborted))


@scanner_api_view(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
def not_found(request: HttpRequest, device: ScannerDevice, path: str = "") -> JsonResponse:
    """Unknown routes under the namespace answer in the envelope, after authentication."""
    return error_response(404, "not_found", "No such scanner API resource.")
