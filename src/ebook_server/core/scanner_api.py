"""Scanner device API v1 (``/api/scanner/v1/``) for the libreflip scanner (``sans``).

The canonical contract is libreflip/serif ``docs/sans-serif-interface.md``; "§" below
refers to it. There is exactly one scanner (#125): every request authenticates with its
key as ``Authorization: Bearer <key>`` (§3), checked against the SHA-256 in
``BOOK_SCANNER_SCANNER_API_KEY_SHA256`` (:mod:`core.scanner_auth`). With that setting
empty the API is disabled and answers ``503``. Sessions and cookies play no part: a
browser session without a valid key gets ``401``, member-session middleware skips this
namespace, and the views are CSRF-exempt because nothing here is cookie-authenticated.

Errors always use the envelope ``{"error": {"code", "message", "details"}}`` (§6.1).
Every request writes one log line (route, status, job ID); the key is never logged.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Final

import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.views.decorators.csrf import csrf_exempt

from . import scan_events
from .models import ScanJob
from .scan_job_ids import is_device_job_id
from .scanner_auth import bearer_token, is_scanner_key, scanner_api_enabled

API_VERSION: Final = 1
# §6.12: event batches are fixed by the contract rather than configured per deployment.
MAX_EVENT_BATCH_BYTES: Final = scan_events.MAX_EVENT_BATCH_BYTES
MAX_EVENT_BATCH_LINES: Final = scan_events.MAX_EVENT_BATCH_LINES

logger = structlog.get_logger(__name__)

ScannerView = Callable[..., JsonResponse]


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


def _route_name(request: HttpRequest) -> str:
    match = request.resolver_match
    if match is None:
        return request.path
    return match.url_name or match.route


def _log_request(request: HttpRequest, status: int, job_id: object) -> None:
    """One line per request: route, method, status and job ID, never the key."""
    logger.info(
        "scanner_api_request",
        route=_route_name(request),
        method=request.method,
        status=status,
        job_id=str(job_id) if job_id is not None else None,
    )


def _refusal(request: HttpRequest, allowed: tuple[str, ...]) -> JsonResponse | None:
    """The response for a request that may not reach the view, else ``None``."""
    if not scanner_api_enabled():
        return error_response(
            503, "scanner_api_disabled", "The scanner API is disabled on this server."
        )
    token = _bearer_token(request)
    if token is None:
        return _unauthorized(
            "authentication_required", "Send the scanner key as Authorization: Bearer."
        )
    if not is_scanner_key(token):
        return _unauthorized("invalid_api_key", "The scanner key is invalid.")
    if request.method not in allowed:
        response = error_response(
            405,
            "method_not_allowed",
            f"{request.method} is not allowed here.",
            {"allowed": list(allowed)},
        )
        response["Allow"] = ", ".join(allowed)
        return response
    return None


def scanner_api_view(
    methods: Iterable[str],
) -> Callable[[ScannerView], Callable[..., JsonResponse]]:
    """Wrap a scanner view: API switch, Bearer authentication, method check, logging.

    The wrapped view is called as ``view(request, *args, **kwargs)`` once the scanner
    key has been verified.
    """
    allowed = tuple(method.upper() for method in methods)

    def decorator(view: ScannerView) -> Callable[..., JsonResponse]:
        @csrf_exempt
        @wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> JsonResponse:
            job_id = kwargs.get("job_id")
            try:
                response = _refusal(request, allowed)
                if response is None:
                    response = view(request, *args, **kwargs)
                    response.setdefault("Cache-Control", "no-store")
            except Exception:
                _log_request(request, 500, job_id)
                raise
            _log_request(request, response.status_code, job_id)
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
def ping(request: HttpRequest) -> JsonResponse:
    """§6.2: reachability, authentication and clock check."""
    return JsonResponse(
        {
            "api_version": API_VERSION,
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

    Covers and ``metadata_opf`` are added by the tickets that store them.
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
        "events": {"sans_last_line_no": scan_events.sans_high_water_mark(job)},
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
    started = _parse_timestamp(payload.get("scan_started_at"))
    if started is None:
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
    return {"scan_started_at": started, "capture_layout": layout}, None


def _parse_timestamp(value: object) -> datetime | None:
    """An ISO 8601 timestamp with a time zone as aware UTC, or ``None`` if it is not one.

    ``parse_datetime`` raises ``ValueError`` for well-formed strings that are not a
    calendar date or time (``2026-02-30T00:00:00Z``, ``T25:00``), and converting an
    extreme value to UTC can overflow. Both are a client error (``400``), not a ``500``.
    """
    if not isinstance(value, str):
        return None
    try:
        started = parse_datetime(value)
        if started is None or started.tzinfo is None:
            return None
        return started.astimezone(UTC)
    except (ValueError, OverflowError):
        return None


def _conflict(job_id: str) -> JsonResponse:
    return error_response(
        409,
        "job_id_conflict",
        "This job ID is already registered with other data.",
        {"job_id": job_id},
    )


def scanner_job(job_id: str, *, lock: bool = False) -> ScanJob | None:
    """The job if the scanner registered it (``source=scanner``), else ``None``.

    Scanner job routes answer ``404`` for ``None``: a web-created job and an unknown ID
    look the same, so job IDs cannot be probed (§6.1). Registration (``PUT``) is the
    exception and answers ``409 job_id_conflict`` for an existing web job (§6.3).
    """
    jobs = ScanJob.objects.select_for_update() if lock else ScanJob.objects.all()
    return jobs.filter(pk=job_id, source=ScanJob.Source.SCANNER).first()


@scanner_api_view(["PUT"])
def job(request: HttpRequest, job_id: str) -> JsonResponse:
    """Register a job (§6.3): 201 created, 200 identical repeat, 409 conflict.

    Repeating the registration with the same ``scan_started_at`` (and
    ``capture_layout``) gets ``200`` and changes nothing. Any other data for an existing
    scanner job, or an existing web-created job with that ID, is
    ``409 job_id_conflict``; nothing is overwritten.
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
                        source=ScanJob.Source.SCANNER,
                        operator=None,
                        **registration,
                    )
                    return JsonResponse(job_status_payload(request, created), status=201)
                same = (
                    current.source == ScanJob.Source.SCANNER
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


# --- events (§6.7) ----------------------------------------------------------------------

# The JSON body escapes the raw lines (every quote gains a backslash), so it may be
# larger than the 1 MiB of line bytes the batch limit counts; a body beyond this is
# refused before it is read.
MAX_EVENT_REQUEST_BYTES: Final = 4 * MAX_EVENT_BATCH_BYTES


def _too_large() -> JsonResponse:
    return error_response(
        413,
        "payload_too_large",
        f"An event batch may hold at most {MAX_EVENT_BATCH_LINES} lines and "
        f"{MAX_EVENT_BATCH_BYTES} bytes.",
        {
            "max_event_batch_bytes": MAX_EVENT_BATCH_BYTES,
            "max_event_batch_lines": MAX_EVENT_BATCH_LINES,
        },
    )


def _read_limited_body(request: HttpRequest, limit: int) -> bytes | None:
    """The request body, or None when it is (or claims to be) larger than ``limit``."""
    try:
        declared = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        declared = 0
    if declared > limit:
        return None
    body = request.read(limit + 1)
    if len(body) > limit:
        return None
    return body


def _parse_event_batch(
    request: HttpRequest,
) -> tuple[tuple[int, list[str]] | None, JsonResponse | None]:
    body = _read_limited_body(request, MAX_EVENT_REQUEST_BYTES)
    if body is None:
        return None, _too_large()
    try:
        payload = json.loads(body or b"null")
    except (UnicodeDecodeError, ValueError):
        return None, error_response(400, "invalid_json", "The body must be a JSON object.")
    if not isinstance(payload, dict):
        return None, error_response(400, "invalid_json", "The body must be a JSON object.")
    first_line_no = payload.get("first_line_no")
    if not isinstance(first_line_no, int) or isinstance(first_line_no, bool) or first_line_no < 1:
        return None, error_response(
            400,
            "invalid_request",
            "first_line_no must be an integer of at least 1.",
            {"field": "first_line_no"},
        )
    lines = payload.get("lines")
    if not isinstance(lines, list) or not all(isinstance(line, str) for line in lines):
        return None, error_response(
            400,
            "invalid_request",
            "lines must be a list of strings.",
            {"field": "lines"},
        )
    if len(lines) > MAX_EVENT_BATCH_LINES:
        return None, _too_large()
    total = 0
    for line in lines:
        total += len(line.encode("utf-8", errors="surrogatepass"))
        if total > MAX_EVENT_BATCH_BYTES:
            return None, _too_large()
    return (first_line_no, lines), None


@scanner_api_view(["POST"])
def job_events(request: HttpRequest, job_id: str) -> JsonResponse:
    """Append ``scan.jsonl`` lines (§6.7).

    ``200 {"sans_last_line_no": N}`` when stored, or when every line was already stored
    identically. ``409 event_gap`` (with ``details.expected_first_line_no``),
    ``409 event_conflict``, ``409 job_state`` (cancelled), ``422 invalid_event``,
    ``413 payload_too_large`` beyond 1000 lines or 1 MiB. A web-created or unknown job
    is ``404`` (§6.1, :func:`scanner_job`).
    """
    if not is_device_job_id(job_id):
        return _invalid_job_id(job_id)
    batch, problem = _parse_event_batch(request)
    if problem is not None:
        return problem
    assert batch is not None
    first_line_no, lines = batch
    if scanner_job(job_id) is None:
        return error_response(404, "not_found", "No such job.", {"job_id": job_id})
    try:
        result = scan_events.append_sans_lines(job_id, first_line_no, lines)
    except ScanJob.DoesNotExist:
        return error_response(404, "not_found", "No such job.", {"job_id": job_id})
    except scan_events.ScanEventError as exc:
        return error_response(exc.status, exc.code, exc.message, exc.details)
    return JsonResponse({"sans_last_line_no": result.sans_last_line_no}, status=200)


@scanner_api_view(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
def not_found(request: HttpRequest, path: str = "") -> JsonResponse:
    """Unknown routes under the namespace answer in the envelope, after authentication."""
    return error_response(404, "not_found", "No such scanner API resource.")
