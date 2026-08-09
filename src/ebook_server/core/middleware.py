"""Cross-cutting application security checks."""

from __future__ import annotations

from collections.abc import Callable

from django.contrib.auth import logout
from django.http import HttpRequest, HttpResponse, HttpResponseForbidden

from .models import AppUserProfile


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
