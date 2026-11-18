"""Top-level URL configuration."""

from __future__ import annotations

from django.contrib import admin
from django.urls import include, path

urlpatterns = [
    path("admin/", admin.site.urls),
    # Scanner device API v1: the scanner key only, never sessions (core.scanner_api).
    path("api/scanner/v1/", include("ebook_server.core.scanner_api_urls")),
    path("", include("ebook_server.core.urls")),
    path("", include("ebook_server.core.review_reader_urls")),
]
