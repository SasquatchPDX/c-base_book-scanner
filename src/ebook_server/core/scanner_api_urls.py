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
        r"^jobs/(?P<job_id>[^/]+)/pages/(?P<sequence_number>[^/]+)/?$",
        scanner_api.page,
        name="scanner-api-page",
    ),
    re_path(
        r"^jobs/(?P<job_id>[^/]+)/covers/(?P<position>[^/]+)/?$",
        scanner_api.cover,
        name="scanner-api-cover",
    ),
    re_path(
        r"^jobs/(?P<job_id>[^/]+)/events/?$",
        scanner_api.job_events,
        name="scanner-api-job-events",
    ),
    re_path(r"^rescans/?$", scanner_api.rescans, name="scanner-api-rescans"),
    re_path(
        r"^jobs/(?P<job_id>[^/]+)/rescans/?$",
        scanner_api.job_rescans,
        name="scanner-api-job-rescans",
    ),
    re_path(
        r"^jobs/(?P<job_id>[^/]+)/rescans/(?P<todo_id>[^/]+)/replacement/?$",
        scanner_api.rescan_replacement,
        name="scanner-api-rescan-replacement",
    ),
    re_path(
        r"^jobs/(?P<job_id>[^/]+)/pages/(?P<sequence_number>[^/]+)/thumbnail/?$",
        scanner_api.page_thumbnail,
        name="scanner-api-page-thumbnail",
    ),
    # Must stay last: everything else under the namespace is a JSON 404 (after auth).
    re_path(r"^(?P<path>.*)$", scanner_api.not_found, name="scanner-api-not-found"),
]
