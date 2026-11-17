"""Run contribution settlement (#27): void, settle, claw back, prune past retention."""

from __future__ import annotations

import argparse
from typing import Any

from django.core.management.base import BaseCommand

from ebook_server.core import contributions
from ebook_server.jobs import get_queue


class Command(BaseCommand):
    help = (
        "Settle contribution events whose hold period has passed, void or claw back "
        "reversed outcomes, and fold events past the retention period into monthly "
        "totals. Idempotent; meant to run daily (see deploy/systemd)."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--enqueue",
            action="store_true",
            help="Hand the run to an RQ worker instead of running it in this process.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not contributions.enabled():
            self.stdout.write("contribution recognition is disabled; nothing to do")
            return
        if options["enqueue"]:
            queue = get_queue()
            if queue is not None:
                job = queue.enqueue(contributions.settle_job)
                self.stdout.write(f"enqueued settlement job {getattr(job, 'id', '')}")
                return
            self.stdout.write("no queue configured; running in this process")
        report = contributions.settle()
        self.stdout.write(
            self.style.SUCCESS(
                f"settled {report.settled}, voided {report.voided}, clawed back "
                f"{report.clawed_back}, pruned {report.pruned}, still pending "
                f"{report.still_pending}, errors {len(report.errors)}",
            ),
        )
