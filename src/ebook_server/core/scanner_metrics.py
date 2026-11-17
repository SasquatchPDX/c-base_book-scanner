"""Traffic counters of the scanner device API (contract §9, issue #133).

Every request that reaches :func:`ebook_server.core.scanner_api.scanner_api_view` is
counted here, keyed by the route *name* (never the raw path, which carries job IDs) and
the HTTP status. The counters are persisted in :class:`ScannerApiRequestCount` so that all
web processes share them; :mod:`ebook_server.core.metrics` exposes them at scrape time as
``scanner_api_requests_total`` and ``scanner_bytes_received_total``.
"""

from __future__ import annotations

from typing import Final

import structlog
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import F
from django.http import HttpRequest

from .models import ScannerApiRequestCount

logger = structlog.get_logger(__name__)

ROUTE_NAME_PREFIX: Final = "scanner-api-"
UNKNOWN_ENDPOINT: Final = "unknown"
_MAX_ENDPOINT_LENGTH: Final = 64


def endpoint_label(request: HttpRequest) -> str:
    """The bounded ``endpoint`` label: the resolved route name without its prefix.

    ``scanner-api-ping`` becomes ``ping``; the namespace's catch-all route becomes
    ``not-found``. The raw path is never used, so job IDs and sequence numbers cannot
    multiply label values.
    """
    match = getattr(request, "resolver_match", None)
    name = getattr(match, "url_name", None) if match is not None else None
    if not name:
        return UNKNOWN_ENDPOINT
    return str(name).removeprefix(ROUTE_NAME_PREFIX)[:_MAX_ENDPOINT_LENGTH]


def declared_body_bytes(request: HttpRequest) -> int:
    """The request body size the client sent, from ``Content-Length`` (0 if absent/bad).

    Reading the header rather than the body keeps the counter from forcing large
    uploads into memory, and it works for views that stream their input.
    """
    raw = request.META.get("CONTENT_LENGTH") or ""
    try:
        size = int(raw)
    except (TypeError, ValueError):
        return 0
    return max(size, 0)


def record_request(endpoint: str, status: int, body_bytes: int) -> None:
    """Add one request (and its body bytes) to the persisted counters.

    Failures are logged and swallowed: metrics must never turn a served response into
    an error for the scanner.
    """
    body_bytes = max(int(body_bytes), 0)
    try:
        if _increment(endpoint, status, body_bytes):
            return
        try:
            with transaction.atomic():
                ScannerApiRequestCount.objects.create(
                    endpoint=endpoint,
                    status=status,
                    requests=1,
                    bytes_received=body_bytes,
                )
        except IntegrityError:
            # Another process created the row between our update and insert.
            _increment(endpoint, status, body_bytes)
    except DatabaseError as error:
        logger.warning(
            "scanner_api_metrics_record_failed",
            endpoint=endpoint,
            status=status,
            error_class=type(error).__name__,
        )


def _increment(endpoint: str, status: int, body_bytes: int) -> bool:
    updated = ScannerApiRequestCount.objects.filter(endpoint=endpoint, status=status).update(
        requests=F("requests") + 1,
        bytes_received=F("bytes_received") + body_bytes,
    )
    return updated > 0
