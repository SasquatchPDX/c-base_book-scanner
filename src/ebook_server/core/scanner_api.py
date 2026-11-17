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

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Final

import structlog
from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .scanner_auth import bearer_token, is_scanner_key, scanner_api_enabled

API_VERSION: Final = 1
# §6.12: event batches are fixed by the contract rather than configured per deployment.
MAX_EVENT_BATCH_BYTES: Final = 1024 * 1024
MAX_EVENT_BATCH_LINES: Final = 1000

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


@scanner_api_view(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
def not_found(request: HttpRequest, path: str = "") -> JsonResponse:
    """Unknown routes under the namespace answer in the envelope, after authentication."""
    return error_response(404, "not_found", "No such scanner API resource.")
