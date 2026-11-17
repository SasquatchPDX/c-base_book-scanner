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
from ebook_server.jobs import enqueue, queue_is_async, rq_pending_job_args


class Command(BaseCommand):
    help = (
        "Discover files from configured source locations and enqueue import jobs, "
        "respecting each source's pause flag, batch size, and hourly rate limit."
    )

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
            "--limit",
            type=int,
            default=None,
            help=(
                "Maximum import jobs in flight per source for this run, overriding the "
                "source's batch size (0 = unlimited). The hourly rate limit still applies."
            ),
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
        limit = options.get("limit")
        if limit is not None and limit < 0:
            raise CommandError("--limit must be zero or positive")

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
                job.attempt_count = 0
                job.save(
                    update_fields=[
                        "state",
                        "last_error",
                        "next_retry_at",
                        "attempt_count",
                        "updated_at",
                    ],
                )
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

        pending_rq_ids, _ = rq_pending_job_args()
        requeue_orphans = pending_rq_ids is not None or not queue_is_async()
        if not requeue_orphans:
            self.stdout.write(
                "warning: cannot inspect the RQ queue; DISCOVERED jobs are not re-queued",
            )
        created_job_ids: list[str] = []
        requeued_job_ids: list[str] = []
        throttled = False
        for location in locations:
            if location.is_paused:
                self.stdout.write(f"source {location.id} paused; skipped")
                continue
            if requeue_orphans:
                requeued_job_ids.extend(self._requeue_orphans(location, pending_rq_ids))
            capacity = intake.source_ingest_capacity(location, batch_size=limit)
            try:
                jobs = intake.discover_source_jobs_for_location(
                    source_location=location,
                    uploader=user,
                    limit=capacity,
                )
            except IntakeError as exc:
                self.stdout.write(f"source {location.id} error: {exc}")
                continue
            if capacity is not None and len(jobs) >= capacity:
                throttled = True
                self.stdout.write(
                    f"source {location.id} batch/rate limit reached; "
                    "run again later to queue more files",
                )
            for job in jobs:
                enqueue(intake.process_import_job, str(job.id))
                created_job_ids.append(str(job.id))

        self.stdout.write(
            f"ingest queued={len(created_job_ids)} requeued={len(requeued_job_ids)} "
            f"throttled={'yes' if throttled else 'no'}"
        )

    def _requeue_orphans(
        self,
        location: SourceLocation,
        pending_rq_ids: set[str] | None,
    ) -> list[str]:
        """Re-queue DISCOVERED jobs that no longer have an RQ job.

        They are left behind when the queue is emptied, Redis is flushed, or a job was
        skipped while its source was paused. With no RQ queue (inline or none
        configured) nothing waits in a queue, so every DISCOVERED job is an orphan.
        ``process_import_job`` claims DISCOVERED jobs atomically, so a job that ends up
        queued twice is still processed once.
        """
        requeued: list[str] = []
        orphans = ImportJob.objects.filter(
            source_location=location,
            state=ImportJob.State.DISCOVERED,
        ).order_by("created_at")
        for job in orphans:
            if pending_rq_ids is not None and str(job.id) in pending_rq_ids:
                continue
            enqueue(intake.process_import_job, str(job.id))
            requeued.append(str(job.id))
        return requeued
