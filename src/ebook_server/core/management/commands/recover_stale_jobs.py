"""Recover background jobs orphaned by killed workers or lost queue entries."""

from __future__ import annotations

import argparse
from typing import Any

from django.core.management.base import BaseCommand
from django.utils import timezone

from ebook_server.core import scan_pipeline
from ebook_server.core.models import ProcessingJob, ScanJob
from ebook_server.jobs import get_queue, is_stale_running, scheduler_active, stale_after


def _queued_rq_job_ids() -> tuple[set[str] | None, bool | None]:
    """Return (ProcessingJob ids that still have an RQ job, scheduler running?).

    ``None`` for the id set means Redis could not be consulted (or the inline
    queue is in use), so QUEUED rows cannot be judged safely.
    """
    queue = get_queue()
    rq_queue = getattr(queue, "queue", None)
    if rq_queue is None:
        return None, None
    from rq.registry import (
        DeferredJobRegistry,
        ScheduledJobRegistry,
        StartedJobRegistry,
    )

    try:
        jobs = list(rq_queue.get_jobs())
        for registry_cls in (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry):
            registry = registry_cls(queue=rq_queue)
            jobs.extend(rq_queue.fetch_job(job_id) for job_id in registry.get_job_ids())
        scheduler = scheduler_active(rq_queue)
    except Exception:
        return None, None
    ids = {str(job.args[0]) for job in jobs if job is not None and job.args}
    return ids, scheduler


class Command(BaseCommand):
    help = (
        "Find ProcessingJobs stuck RUNNING (worker killed) or QUEUED with no RQ job "
        "(Redis flushed, enqueue lost), and ScanJobs stuck FINALIZING. Dry-run unless "
        "--apply is given."
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
        rq_ids, scheduler = _queued_rq_job_ids()
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

        verb = "recovered" if apply else "would recover"
        self.stdout.write(self.style.SUCCESS(f"{verb} {len(stale)} stale job(s)"))

    def _fail(self, job: ProcessingJob, reason: str) -> None:
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
