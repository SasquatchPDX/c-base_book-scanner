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
            scan_covers,
            scan_events,
            scan_manifest,
        )
        from .search import connect_signals

        connect_signals()
        scan_covers.register()
        # capture-complete and GET jobs/{id} compare against the stored event lines (S5).
        scan_manifest.register_event_high_water_mark(scan_events.sans_high_water_mark)
