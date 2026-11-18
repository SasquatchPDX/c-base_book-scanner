"""Surface historical terminal worker failures without rerunning or changing jobs."""

from __future__ import annotations

import argparse
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from ebook_server.core import worker_failures
from ebook_server.core.models import ProcessingJob


class Command(BaseCommand):
    help = "Create missing worker-failure reviews in bounded batches; dry-run unless --apply."

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--after-pk", type=int, default=0)

    def handle(self, *args: Any, **options: Any) -> None:
        limit, after = options["limit"], options["after_pk"]
        if not 1 <= limit <= 1000 or after < 0:
            raise CommandError("Use --limit between 1 and 1000 and a nonnegative --after-pk.")
        jobs = list(
            ProcessingJob.objects.filter(
                pk__gt=after,
                job_type__in=worker_failures.SUPPORTED,
                state=ProcessingJob.JobState.FAILED,
                recovery_task__isnull=True,
            ).order_by("pk")[:limit]
        )
        for job in jobs:
            self.stdout.write(
                f"{'reconcile' if options['apply'] else 'would reconcile'} {job.pk} {job.job_id}"
            )
            if options["apply"]:
                # Rechecks state under the same row lock used by live failure handling.
                worker_failures.sync_outcome(ProcessingJob, job)
        self.stdout.write(f"examined={len(jobs)} next_after_pk={jobs[-1].pk if jobs else after}")
