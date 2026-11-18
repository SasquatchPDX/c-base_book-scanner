"""Backfill working images for page versions uploaded before migration 0050 (#195)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from ... import scan_images


class Command(BaseCommand):
    help = (
        "Queue a working-image job for every active scan page version without one (pages "
        "uploaded before migration 0050). Idempotent; the editor also generates a "
        "missing image on first open, this only does it ahead of time."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--all-versions",
            action="store_true",
            help="Include superseded (inactive) page versions, not only active ones.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many versions (0: no limit)."
        )
        parser.add_argument(
            "--dry-run", action="store_true", help="Only count the versions without an image."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        version_ids = scan_images.missing_page_image_versions(
            include_inactive=options["all_versions"]
        ).values_list("pk", flat=True)
        if options["limit"] > 0:
            version_ids = version_ids[: options["limit"]]
        missing = queued = 0
        for version_id in version_ids.iterator(chunk_size=500):
            missing += 1
            if not options["dry_run"] and scan_images.request_page_image(version_id):
                queued += 1
        if options["dry_run"]:
            summary = f"{missing} without a working image (dry run, nothing queued)"
        else:
            summary = f"{missing} without a working image, {queued} queued"
        self.stdout.write(self.style.SUCCESS(f"Scan page images - {summary}"))
