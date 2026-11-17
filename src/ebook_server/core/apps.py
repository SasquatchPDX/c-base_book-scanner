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
            search,
            thumbnails,
        )

        search.connect_signals()
        thumbnails.connect_signals()
