"""Cleanly stop a running source import (database state and RQ registries together)."""

from __future__ import annotations

import argparse
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from ebook_server.core import intake
from ebook_server.core.models import ImportJob, SourceLocation
from ebook_server.jobs import discard_rq_jobs

IMPORT_JOB_FUNC = f"{intake.process_import_job.__module__}.{intake.process_import_job.__name__}"


class Command(BaseCommand):
    help = (
        "Stop the import run of a source location: pause the source, mark its queued and "
        "in-progress ImportJobs CANCELLED, and delete their RQ jobs from the queue and "
        "the started/scheduled/deferred/failed registries so a worker restart cannot "
        "resurrect them. Jobs already archiving are allowed to finish. Dry-run unless "
        "--apply is given."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--source-id",
            action="append",
            type=int,
            required=True,
            help="Source location id to stop. Repeat for multiple locations.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Actually cancel; without it, only report what would be cancelled.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        apply = bool(options["apply"])
        locations = list(SourceLocation.objects.filter(id__in=options["source_id"]))
        if not locations:
            raise CommandError("No matching source locations found")
        self.stdout.write(f"mode: {'APPLY' if apply else 'dry-run (pass --apply to act)'}")

        for location in locations:
            active = ImportJob.objects.filter(
                source_location=location,
                state__in=intake.IN_FLIGHT_STATES,
            )
            archiving = active.filter(state=ImportJob.State.ARCHIVING).count()
            if not apply:
                self.stdout.write(
                    f"source {location.id}: would pause and cancel "
                    f"{active.count() - archiving} job(s); {archiving} archiving job(s) "
                    "would be left to finish",
                )
                continue
            cancelled = intake.cancel_source_imports(location)
            removed = discard_rq_jobs(IMPORT_JOB_FUNC, set(cancelled))
            rq_note = (
                "RQ queue not inspectable; cancelled jobs are skipped if they run"
                if removed is None
                else f"removed {removed} RQ job(s)"
            )
            self.stdout.write(
                f"source {location.id}: paused, cancelled {len(cancelled)} job(s), "
                f"{rq_note}; {archiving} archiving job(s) left to finish",
            )
        if apply:
            self.stdout.write(
                self.style.SUCCESS(
                    "Done. Resume the source in admin and run ingest_sources to import "
                    "the cancelled files again.",
                ),
            )
