"""Core app configuration."""

from __future__ import annotations

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ebook_server.core"
    verbose_name = "Core"

    def ready(self) -> None:
        from . import (
            checks,  # noqa: F401  (registers system checks)
            scanner_opf,
        )
        from .search import connect_signals

        connect_signals()
        # Scanner metadata.opf (#130): manifest source, raw-scan annotator, book linker.
        scanner_opf.register()
