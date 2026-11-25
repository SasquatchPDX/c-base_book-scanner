"""Delete ScanTailor-Web stage files no stage row refers to (#204)."""

from __future__ import annotations

import argparse
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from ebook_server.core import scan_artifact_pruning
from ebook_server.jobs import get_queue
from ebook_server.storage import StorageUnavailableError


class Command(BaseCommand):
    help = (
        "Delete stage artifacts under scan-jobs/ (earlier revisions, invalidated runs, "
        "deleted jobs) that no ScanTailor stage row refers to. Raw scanner pages are "
        "never touched. Meant to run periodically (see deploy/systemd)."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Only report how many files and bytes would be deleted.",
        )
        parser.add_argument(
            "--min-age-seconds",
            type=int,
            default=scan_artifact_pruning.DEFAULT_SWEEP_MIN_AGE_SECONDS,
            help="Leave files younger than this alone (default: one hour).",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=scan_artifact_pruning.DEFAULT_SWEEP_LIMIT,
            help="Stop with an error after listing this many entries.",
        )
        parser.add_argument(
            "--enqueue",
            action="store_true",
            help="Hand the sweep to an RQ worker instead of running it in this process.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        dry_run = bool(options["dry_run"])
        if options["enqueue"] and not dry_run:
            queue = get_queue()
            if queue is not None:
                job = queue.enqueue(scan_artifact_pruning.sweep_job)
                self.stdout.write(f"enqueued scan artifact sweep {getattr(job, 'id', '')}")
                return
            self.stdout.write("no queue configured; running in this process")
        try:
            report = scan_artifact_pruning.sweep(
                dry_run=dry_run,
                min_age_seconds=options["min_age_seconds"],
                limit=options["limit"],
            )
        except (OSError, ValueError, StorageUnavailableError) as exc:
            raise CommandError(f"scan artifact sweep failed, nothing deleted: {exc}") from exc
        verb = "would delete" if dry_run else "deleted"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verb} {report.files} file(s), {report.bytes} bytes; "
                f"kept {report.kept}, failed {report.failed}",
            ),
        )
