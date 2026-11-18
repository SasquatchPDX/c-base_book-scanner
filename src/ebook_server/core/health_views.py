"""Application readiness, dependency checks and metrics."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import redis
from django.conf import settings as django_settings
from django.db import connections
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.views.decorators.http import require_GET
from prometheus_client import CollectorRegistry, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from ebook_server.storage import (
    check_storage_root,
    free_bytes,
    min_free_bytes,
    probe_storage_writable,
)

from .metrics import PersistentStateCollector


@require_GET
def prometheus_metrics(request: HttpRequest) -> HttpResponseBase:
    if not django_settings.BOOK_SCANNER_METRICS_ENABLED:
        return HttpResponseNotFound()
    expected_token = django_settings.BOOK_SCANNER_METRICS_BEARER_TOKEN
    if expected_token:
        authorization = request.headers.get("Authorization", "")
        supplied_token = authorization.removeprefix("Bearer ") if authorization else ""
        if not supplied_token or not secrets.compare_digest(supplied_token, expected_token):
            response = HttpResponseForbidden("invalid metrics credentials")
            response["WWW-Authenticate"] = "Bearer"
            return response

    registry = CollectorRegistry(auto_describe=True)
    registry.register(PersistentStateCollector())
    return HttpResponse(generate_latest(registry), content_type=CONTENT_TYPE_LATEST)


@dataclass(frozen=True)
class DependencyResult:
    name: str
    status: str
    details: dict[str, Any] | None = None


def _status_payload() -> list[dict[str, Any]]:
    results = [
        _check_database(),
        _check_storage(),
        _check_queue(),
    ]
    return [dep.__dict__ for dep in results]


def _check_database() -> DependencyResult:
    try:
        connection = connections["default"]
        connection.connect()
        connection.close()
        return DependencyResult(name="database", status="ok")
    except Exception as exc:  # pragma: no cover - transport-specific failures
        return DependencyResult(name="database", status="down", details={"error": str(exc)})


def _check_storage() -> DependencyResult:
    try:
        storage_root = Path(django_settings.APP_DATA_DIR)
        if not getattr(django_settings, "STORAGE_REQUIRE_EXISTING_ROOT", False):
            storage_root.mkdir(parents=True, exist_ok=True)
        check_storage_root(root=storage_root)
        probe_storage_writable(storage_root)
        details: dict[str, Any] = {"root": str(storage_root)}
        available = free_bytes(storage_root)
        reserve = min_free_bytes()
        if available is not None:
            details["free_bytes"] = available
            details["min_free_bytes"] = reserve
        if available is not None and reserve > 0 and available < reserve:
            # Imports refuse to archive below the reserve; the app itself still serves.
            details["warning"] = "free space below BOOK_SCANNER_STORAGE_MIN_FREE_BYTES"
            return DependencyResult(name="storage", status="degraded", details=details)
        return DependencyResult(name="storage", status="ok", details=details)
    except Exception as exc:  # pragma: no cover
        return DependencyResult(name="storage", status="down", details={"error": str(exc)})


def _check_queue() -> DependencyResult:
    queue_url = getattr(django_settings, "BOOK_SCANNER_QUEUE_URL", None)
    if not queue_url:
        return DependencyResult(
            name="queue",
            status="disabled",
            details={"reason": "not configured"},
        )
    if queue_url.startswith("inline://"):
        return DependencyResult(
            name="queue",
            status="ok",
            details={"mode": "inline development queue"},
        )
    client = redis.Redis.from_url(queue_url)
    try:
        client.ping()
        return DependencyResult(name="queue", status="ok")
    except Exception as exc:  # pragma: no cover - infrastructure-specific failures
        return DependencyResult(name="queue", status="down", details={"error": str(exc)})


@require_GET
def health(request: HttpRequest) -> JsonResponse:
    dependencies = _status_payload()
    any_down = any(item["status"] == "down" for item in dependencies)
    status_code = 200 if not any_down else 503
    return JsonResponse(
        {
            "status": "ok" if not any_down else "degraded",
            "dependencies": dependencies,
        },
        status=status_code,
    )


@require_GET
def readiness(request: HttpRequest) -> JsonResponse:
    dependencies = _status_payload()
    any_down = any(item["status"] == "down" for item in dependencies)
    if any_down:
        return JsonResponse({"status": "not_ready", "dependencies": dependencies}, status=503)
    return JsonResponse({"status": "ready", "dependencies": dependencies})
