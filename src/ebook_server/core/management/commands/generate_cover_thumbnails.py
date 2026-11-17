"""Backfill cover thumbnails (#28): covers without current thumbnails get them."""

from __future__ import annotations

from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand, CommandParser

from ebook_server import jobs

from ... import thumbnails
from ...models import CoverImage


class Command(BaseCommand):
    help = (
        "Generate WebP thumbnails for front covers that have none or whose cover changed. "
        "Idempotent; run once after deploying migration 0035 and whenever "
        'ebook_server_library_derived_rows_missing{kind="cover_thumbnails"} is above zero.'
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--force",
            action="store_true",
            help="Regenerate thumbnails for every front cover, current or not.",
        )
        parser.add_argument(
            "--enqueue",
            action="store_true",
            help="Queue one background job per cover instead of rendering in this process.",
        )
        parser.add_argument(
            "--limit", type=int, default=0, help="Stop after this many covers (0: no limit)."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        covers = (
            CoverImage.objects.filter(position=CoverImage.CoverPosition.FRONT)
            if options["force"]
            else thumbnails.covers_needing_thumbnails()
        )
        cover_ids = covers.order_by("pk").values_list("pk", flat=True)
        if options["limit"] > 0:
            cover_ids = cover_ids[: options["limit"]]
        outcomes: Counter[str] = Counter()
        for cover_id in cover_ids.iterator(chunk_size=500):
            if options["enqueue"]:
                jobs.enqueue(thumbnails.generate_cover_thumbnails, cover_id, force=options["force"])
                outcomes["queued"] += 1
            else:
                outcomes[
                    thumbnails.generate_cover_thumbnails(cover_id, force=options["force"])
                ] += 1
        summary = ", ".join(f"{name}: {count}" for name, count in sorted(outcomes.items()))
        self.stdout.write(self.style.SUCCESS(f"Cover thumbnails - {summary or 'nothing to do'}"))
