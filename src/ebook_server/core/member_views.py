"""Member sign-in, sign-out and role-protected access."""

from __future__ import annotations

from collections.abc import Callable
from functools import wraps

from django.contrib.auth import authenticate, login, logout
from django.contrib.auth.decorators import login_required
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import (
    request_limits,
)
from .view_helpers import (
    _limited_response,
    _profile_for_request,
)


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
@login_required
@_require_role("admin")
def admin_only(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "admin"})
