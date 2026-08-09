from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from ebook_server.core import intake
from ebook_server.core.intake import IntakeError
from ebook_server.core.models import ImportJob, SourceLocation
from ebook_server.jobs import enqueue


class Command(BaseCommand):
    help = "Discover files from configured source locations and enqueue import jobs."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--source-id",
            action="append",
            type=int,
            help="Optional source location id filter. Repeat for multiple locations.",
        )
        parser.add_argument(
            "--preview",
            action="store_true",
            help="Show discovered jobs without enqueuing.",
        )
        parser.add_argument(
            "--retry-failed",
            action="store_true",
            help="Retry previously failed jobs for selected source locations.",
        )
        parser.add_argument(
            "--system-user",
            default="system-source-ingest",
            help="Fallback username for source jobs when no uploader is assigned.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit JSON payload in preview mode.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        source_ids = options.get("source_id") or []
        preview = options.get("preview", False)
        retry_failed = options.get("retry_failed", False)
        username = options.get("system_user", "system-source-ingest")
        output_json = options.get("json", False)

        queryset = SourceLocation.objects.filter(is_active=True)
        if source_ids:
            queryset = queryset.filter(id__in=source_ids)
        locations = list(queryset)

        if not locations:
            raise CommandError("No active source locations found")

        if retry_failed:
            retried = []
            for job in ImportJob.objects.filter(
                source_location__in=locations,
                state=ImportJob.State.FAILED,
            ):
                if not job.source_path:
                    continue
                if not Path(job.source_path).exists():
                    continue
                job.state = ImportJob.State.DISCOVERED
                job.last_error = ""
                job.next_retry_at = None
                job.save(update_fields=["state", "last_error", "next_retry_at", "updated_at"])
                enqueue(intake.process_import_job, str(job.id))
                retried.append(str(job.id))

            self.stdout.write(
                json.dumps({"retried_jobs": retried}) if output_json else f"retried={len(retried)}"
            )
            return

        if preview:
            payload = []
            for location in locations:
                try:
                    for row in intake.preview_source_jobs(location):
                        payload.append(row)
                except IntakeError as exc:
                    payload.append(
                        {
                            "source_location_id": str(location.id),
                            "error": str(exc),
                        },
                    )
            if output_json:
                self.stdout.write(json.dumps(payload, indent=2))
            else:
                for row in payload:
                    if "error" in row:
                        self.stdout.write(
                            f"source {row['source_location_id']} error: {row['error']}"
                        )
                        continue
                    self.stdout.write(
                        f"{row['source_type']} => {row['path']} (source "
                        f"#{row['source_location_id']})"
                    )
            return

        user_model = get_user_model()
        user = user_model.objects.filter(username=username).first()
        if user is None:
            user = user_model.objects.create_user(username=username)

        created_job_ids = []
        for location in locations:
            try:
                jobs = intake.discover_source_jobs_for_location(
                    source_location=location,
                    uploader=user,
                )
            except IntakeError as exc:
                self.stdout.write(f"source {location.id} error: {exc}")
                continue
            for job in jobs:
                enqueue(intake.process_import_job, str(job.id))
                created_job_ids.append(str(job.id))

        self.stdout.write(
            f"ingest queued={len(created_job_ids)} total_files={len(created_job_ids)}"
        )
