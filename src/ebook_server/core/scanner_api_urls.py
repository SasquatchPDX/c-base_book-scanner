"""Routes of the scanner device API v1, mounted at ``/api/scanner/v1/``."""

from __future__ import annotations

from django.urls import re_path

from . import scanner_api

urlpatterns = [
    re_path(r"^ping/?$", scanner_api.ping, name="scanner-api-ping"),
    # Any single segment: the view answers 400 invalid_job_id for a bad format (§6.1).
    re_path(r"^jobs/(?P<job_id>[^/]+)/?$", scanner_api.job, name="scanner-api-job"),
    re_path(
        r"^jobs/(?P<job_id>[^/]+)/capture-complete/?$",
        scanner_api.capture_complete,
        name="scanner-api-capture-complete",
    ),
    re_path(r"^jobs/(?P<job_id>[^/]+)/abort/?$", scanner_api.abort, name="scanner-api-abort"),
    re_path(
        r"^jobs/(?P<job_id>[^/]+)/metadata/?$",
        scanner_api.metadata,
        name="scanner-api-metadata",
    ),
    # Must stay last: everything else under the namespace is a JSON 404 (after auth).
    re_path(r"^(?P<path>.*)$", scanner_api.not_found, name="scanner-api-not-found"),
]
