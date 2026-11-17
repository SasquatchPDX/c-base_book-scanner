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

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from functools import wraps
from typing import Any, Final

from django.conf import settings
from django.http import HttpRequest, JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from .models import ScannerDevice
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


@scanner_api_view(["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE"])
def not_found(request: HttpRequest, device: ScannerDevice, path: str = "") -> JsonResponse:
    """Unknown routes under the namespace answer in the envelope, after authentication."""
    return error_response(404, "not_found", "No such scanner API resource.")
