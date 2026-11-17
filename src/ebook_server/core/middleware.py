"""Cross-cutting application security checks."""

from __future__ import annotations

from collections.abc import Callable

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden, JsonResponse

from .models import AppUserProfile
from .scanner_auth import bearer_token, is_scanner_key

SCANNER_API_PREFIX = "/api/scanner/"


def is_scanner_api_path(path: str) -> bool:
    """Whether ``path`` is in the scanner API namespace, which ignores member sessions."""
    return path.startswith(SCANNER_API_PREFIX)


class LocalBanMiddleware:
    """Invalidate existing sessions immediately when a local profile is disabled.

    The scanner API (``/api/scanner/``) is exempt: it authenticates with the scanner key
    only and must answer in its own JSON envelope whatever session cookie comes along, so
    member-session state never decides its response.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if is_scanner_api_path(request.path):
            return self.get_response(request)
        if request.user.is_authenticated:
            try:
                profile = request.user.library_profile
            except AppUserProfile.DoesNotExist:
                profile = None
            if profile is not None and profile.is_disabled:
                logout(request)
                return HttpResponseForbidden("account disabled")
        return self.get_response(request)


class ScannerKeyMiddleware:
    """Refuse the scanner API key everywhere outside the scanner API.

    The scanner key never authenticates a user, so it would already be ignored. Rejecting
    it explicitly (``403``) instead of answering with a login redirect makes a misrouted
    scanner or a leaked key visible, and it keeps the key from ever reaching a library,
    review, portal or admin view.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        if not is_scanner_api_path(request.path):
            token = bearer_token(request.headers.get("Authorization", ""))
            if is_scanner_key(token):
                return JsonResponse(
                    {
                        "error": {
                            "code": "scanner_key_not_allowed",
                            "message": "The scanner key is valid only under /api/scanner/v1/.",
                            "details": {},
                        }
                    },
                    status=403,
                )
        return self.get_response(request)
