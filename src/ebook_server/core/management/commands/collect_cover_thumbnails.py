"""Garbage-collect cover thumbnail files nothing references any more (#28)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from ... import thumbnails


class Command(BaseCommand):
    help = (
        "Delete cover thumbnail files that no thumbnail row has referenced for longer "
        "than BOOK_SCANNER_COVER_THUMBNAIL_GC_GRACE_SECONDS (default one day). Files "
        "still or again referenced are kept. Safe to run while workers generate "
        "thumbnails; run it periodically (cron or a systemd timer, e.g. hourly)."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--grace-seconds",
            type=int,
            default=None,
            help="Override the grace period for this run.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=10_000,
            help="Delete at most this many files per run (default 10000).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        report = thumbnails.collect_unreferenced_files(
            grace_seconds=options["grace_seconds"], limit=max(int(options["limit"]), 0)
        )
        self.stdout.write(
            self.style.SUCCESS(
                f"Cover thumbnail files - marked: {report.marked}, deleted: {report.deleted}, "
                f"kept: {report.kept}"
            )
        )
