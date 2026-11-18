"""Recover background jobs orphaned by killed workers or lost queue entries."""

from __future__ import annotations

import argparse
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from ebook_server.core import conversion, intake, scan_pipeline
from ebook_server.core.models import ProcessingJob, ScanJob, ScanLog
from ebook_server.jobs import is_stale_running, rq_pending_job_args, stale_after


class Command(BaseCommand):
    help = (
        "Find ProcessingJobs stuck RUNNING (worker killed) or QUEUED with no RQ job "
        "(Redis flushed, enqueue lost), ImportJobs whose worker lease expired in an "
        "active phase, and ScanJobs stuck FINALIZING. Dry-run unless --apply is given."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Mark stale jobs FAILED and return stuck scans to a retryable state.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        apply = bool(options["apply"])
        now = timezone.now()
        rq_ids, scheduler = rq_pending_job_args()
        self.stdout.write(f"mode: {'APPLY' if apply else 'dry-run (pass --apply to act)'}")
        if rq_ids is None:
            self.stdout.write(
                "note: RQ queue not inspectable; skipping QUEUED-without-RQ-job check"
            )
        elif scheduler is False:
            self.stdout.write(
                self.style.WARNING(
                    "WARNING: no rq scheduler lock found. Start a worker with --with-scheduler "
                    "or delayed retries will never run.",
                ),
            )

        stale: list[tuple[ProcessingJob, str]] = []
        for job in ProcessingJob.objects.filter(
            state__in=[ProcessingJob.JobState.RUNNING, ProcessingJob.JobState.QUEUED],
        ).order_by("scheduled_at"):
            if job.state == ProcessingJob.JobState.RUNNING:
                if is_stale_running(job):
                    stale.append((job, "RUNNING past job timeout (worker likely killed)"))
            elif rq_ids is not None and str(job.job_id) not in rq_ids:
                age = now - job.scheduled_at
                if age > stale_after(job.job_type):
                    stale.append((job, "QUEUED but no RQ job exists for it"))

        for job, reason in stale:
            self.stdout.write(f"stale {job.job_type} {job.job_id}: {reason}")
            if apply:
                self._fail(job, reason)

        stale_ids = {job.pk for job, _ in stale}
        for scan in ScanJob.objects.filter(state=ScanJob.State.FINALIZING):
            active = [
                other
                for other in ProcessingJob.objects.filter(
                    job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
                    state__in=[ProcessingJob.JobState.RUNNING, ProcessingJob.JobState.QUEUED],
                    details__scan_job_id=str(scan.id),
                )
                if other.pk not in stale_ids
            ]
            if active:
                continue
            self.stdout.write(f"scan {scan.id} stuck FINALIZING with no live finalization job")
            if apply:
                last = (
                    ProcessingJob.objects.filter(
                        job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
                        details__scan_job_id=str(scan.id),
                    )
                    .order_by("-scheduled_at")
                    .first()
                )
                details = (
                    last.details if last is not None and isinstance(last.details, dict) else {}
                )
                scan_pipeline._restore_scan_after_failure(scan, details)
                self.stdout.write(f"  -> scan {scan.id} returned to retryable state")

        recovered_imports = self._recover_imports(apply)

        verb = "recovered" if apply else "would recover"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(stale)} stale job(s)"))
        self.stdout.write(
            self.style.SUCCESS(f"{verb} {recovered_imports} stale import job(s)"),
        )

    def _recover_imports(self, apply: bool) -> int:
        """Fail ImportJobs stuck in an active phase after their lease expired.

        The lease outlives the RQ job timeout, so its worker was killed. The job becomes
        FAILED without an automatic retry; ``ingest_sources --retry-failed`` re-queues it.
        """
        recovered = 0
        for job in intake.stale_import_jobs():
            reason = f"import lease expired in {job.state} (worker likely killed)"
            self.stdout.write(f"stale import {job.id}: {reason}")
            if not apply:
                recovered += 1
            elif intake.recover_stale_import_job(job, reason):
                recovered += 1
                self.stdout.write(f"  -> import {job.id} marked FAILED")
            else:
                self.stdout.write(f"  -> import {job.id} changed meanwhile; left alone")
        return recovered

    def _fail(self, job: ProcessingJob, reason: str) -> None:
        if job.job_type == ProcessingJob.JobType.CONVERSION:
            if not conversion.recover_stale_conversion_job(
                job, f"recovered by recover_stale_jobs: {reason}"
            ):
                self.stdout.write(f"  -> conversion {job.job_id} changed meanwhile; left alone")
            return
        details = dict(job.details) if isinstance(job.details, dict) else {}
        details["error"] = f"recovered by recover_stale_jobs: {reason}"
        job.state = ProcessingJob.JobState.FAILED
        job.finished_at = timezone.now()
        job.details = details
        job.save(update_fields=["state", "finished_at", "details"])
        if job.job_type == ProcessingJob.JobType.SCAN_FINALIZATION:
            scan = ScanJob.objects.filter(pk=details.get("scan_job_id")).first()
            if scan is not None:
                scan_pipeline._restore_scan_after_failure(scan, details)
        elif job.job_type == ProcessingJob.JobType.SCAN_LOG:
            # The staged upload is kept; the log's state shows the scanner it failed.
            ScanLog.objects.filter(processing_job=job, state=ScanLog.State.QUEUED).update(
                state=ScanLog.State.FAILED,
                error=details["error"][:500],
                updated_at=timezone.now(),
            )
