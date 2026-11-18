"""Request parsing, authentication context and responses shared by HTTP workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import UUID

from django.http import (
    HttpRequest,
    HttpResponse,
)

from . import (
    request_limits,
)
from .models import (
    AppUserProfile,
)


def _limited_response(kind: str, error: request_limits.RequestLimited) -> HttpResponse:
    request_limits.record_rejection(kind)
    response = HttpResponse(
        f"Too many requests. Try again in {error.retry_after} seconds.",
        status=429,
        content_type="text/plain; charset=utf-8",
    )
    response["Retry-After"] = str(error.retry_after)
    return response


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


def _cleanup_staged_upload(path: str) -> None:
    Path(path).unlink(missing_ok=True)


def _active_library_profile(request: HttpRequest) -> AppUserProfile | None:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return None
    return profile


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


def _as_positive_int(raw: str | None, default: int, minimum: int = 1) -> int:
    try:
        value = int(raw or 0)
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    return value
