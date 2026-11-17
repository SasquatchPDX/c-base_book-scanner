"""Cross-cutting application security checks."""

from __future__ import annotations

from collections.abc import Callable

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse

from .models import AppUserProfile
from .scanner_devices import bearer_token, device_for_key

SCANNER_API_PREFIX = "/api/scanner/"


class LocalBanMiddleware:
    """Invalidate existing sessions immediately when a local profile is disabled."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if request.user.is_authenticated:
            try:
                profile = request.user.library_profile
            except AppUserProfile.DoesNotExist:
                profile = None
            if profile is not None and profile.is_disabled:
                logout(request)
                return HttpResponseForbidden("account disabled")
        return self.get_response(request)


class ScannerDeviceKeyMiddleware:
    """Refuse scanner device keys everywhere outside the scanner device API.

    Device keys never authenticate a user, so they would already be ignored. Rejecting
    them explicitly (``403``) instead of answering with a login redirect makes a
    misrouted scanner or a leaked key visible, and it keeps the key from ever reaching a
    library, review, portal or admin view.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not request.path.startswith(SCANNER_API_PREFIX):
            token = bearer_token(request.headers.get("Authorization", ""))
            if token is not None and device_for_key(token) is not None:
                return JsonResponse(
                    {
                        "error": {
                            "code": "device_key_not_allowed",
                            "message": "Scanner device keys are valid only under /api/scanner/v1/.",
                            "details": {},
                        }
                    },
                    status=403,
                )
        return self.get_response(request)
