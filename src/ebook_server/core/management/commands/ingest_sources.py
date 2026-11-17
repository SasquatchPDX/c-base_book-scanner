from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

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
                # Conditional: a queue retry may have claimed the job since it was read.
                reset = ImportJob.objects.filter(
                    pk=job.pk,
                    state=ImportJob.State.FAILED,
                    lease_owner="",
                ).update(
                    state=ImportJob.State.DISCOVERED,
                    last_error="",
                    next_retry_at=None,
                    attempt_count=0,
                    lease_expires_at=None,
                    updated_at=timezone.now(),
                )
                if not reset:
                    continue
                enqueue(intake.process_import_job, str(job.id))
                retried.append(str(job.id))

            self.stdout.write(
                json.dumps({"retried_jobs": retried}) if output_json else f"retried={len(retried)}"
            )
            return

        if preview:
            payload: list[dict[str, Any]] = []
            for location in locations:
                try:
                    source_preview = intake.preview_source_location(location)
                except IntakeError as exc:
                    payload.append(
                        {
                            "source_location_id": str(location.id),
                            "error": str(exc),
                        },
                    )
                    continue
                payload.extend(
                    {**row, "status": "skipped" if "skipped" in row else "importable"}
                    for row in source_preview.rows
                )
                payload.append(
                    {
                        "source_location_id": str(location.id),
                        "summary": {
                            "importable": len(source_preview.jobs),
                            "skipped": len(source_preview.skipped),
                            "skipped_audio": source_preview.skipped_audio,
                            "skipped_by_extension": source_preview.skipped_by_extension,
                        },
                    },
                )
            if output_json:
                self.stdout.write(json.dumps(payload, indent=2))
            else:
                audio_rows = 0
                for row in payload:
                    if "error" in row:
                        self.stdout.write(
                            f"source {row['source_location_id']} error: {row['error']}"
                        )
                        continue
                    if "summary" in row:
                        summary = row["summary"]
                        by_extension = ", ".join(
                            f"{extension}={count}"
                            for extension, count in summary["skipped_by_extension"].items()
                        )
                        self.stdout.write(
                            f"source #{row['source_location_id']}: "
                            f"importable={summary['importable']} "
                            f"skipped={summary['skipped']}"
                            + (f" ({by_extension})" if by_extension else "")
                        )
                        continue
                    if row.get("skipped") == "audio":
                        audio_rows += 1
                        self.stdout.write(
                            f"skipped [{row['reason']}] => {row['path']} (source "
                            f"#{row['source_location_id']})"
                        )
                        continue
                    if row.get("skipped") == "unsupported":
                        self.stdout.write(
                            f"SKIPPED ({row['reason']}: {row['extension']}) => {row['path']} "
                            f"(source #{row['source_location_id']})"
                        )
                        continue
                    line = (
                        f"{row['source_type']} => {row['path']} (source "
                        f"#{row['source_location_id']})"
                    )
                    if "path_layout" in row:
                        hints = row["path_layout"]
                        line += (
                            " [layout: no match]"
                            if hints is None
                            else f" [layout: author={hints['authors']!r} "
                            f"title={hints['title']!r} series={hints['series']!r}]"
                        )
                    self.stdout.write(line)
                if audio_rows:
                    self.stdout.write(
                        f"skipped_audio={audio_rows} (audiobooks are not managed here; "
                        "see docs/design/32-audiobooks.md)"
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
        skipped_audio: list[int] = []
        budget = intake.archive_space_budget()
        for location in locations:
            if location.is_paused:
                self.stdout.write(f"source {location.id} paused; skipped")
                continue
            if requeue_orphans:
                requeued_job_ids.extend(self._requeue_orphans(location, pending_rq_ids))
            if budget is not None and budget.exhausted:
                continue
            capacity = intake.source_ingest_capacity(location, batch_size=limit)
            try:
                jobs = intake.discover_source_jobs_for_location(
                    source_location=location,
                    uploader=user,
                    limit=capacity,
                    byte_budget=budget,
                    skipped_audio=skipped_audio,
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

        summary = (
            f"ingest queued={len(created_job_ids)} total_files={len(created_job_ids)} "
            f"requeued={len(requeued_job_ids)} throttled={'yes' if throttled else 'no'}"
        )
        if sum(skipped_audio):
            summary += f" skipped_audio={sum(skipped_audio)}"
        self.stdout.write(summary)
        if budget is not None and budget.exhausted:
            self.stderr.write(
                "insufficient free space on the archive volume: stopped queueing new "
                "imports to keep BOOK_SCANNER_STORAGE_MIN_FREE_BYTES free. Free space "
                "(or enlarge the volume) and run ingest_sources again.",
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
        ``process_import_job`` takes an exclusive ownership lease before doing any
        work, so a job that ends up queued twice is still processed once.
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
