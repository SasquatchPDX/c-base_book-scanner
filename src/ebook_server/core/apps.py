"""Core app configuration."""

from __future__ import annotations

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "ebook_server.core"
    verbose_name = "Core"

    def ready(self) -> None:
        from . import (
            book_opf,
            checks,  # noqa: F401  (registers system checks)
            scan_covers,
            scan_events,
            scan_images,
            scan_manifest,
            scanner_opf,
            search,
            thumbnails,
            worker_failures,
        )

        search.connect_signals()
        book_opf.connect_signals()
        thumbnails.connect_signals()
        worker_failures.connect_signals()
        # Working image and thumbnail of every new page version (#195).
        scan_images.connect_signals()
        scan_covers.register()
        # capture-complete and GET jobs/{id} compare against the stored event lines (S5).
        scan_manifest.register_event_high_water_mark(scan_events.sans_high_water_mark)
        # Scanner metadata.opf (#130): manifest source, raw-scan annotator, book linker.
        scanner_opf.register()
