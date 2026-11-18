"""Sort archived files into the canonical book-folder tree: dry-run plan first, then apply."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ...archive_reorganize import (
    ReorganizeError,
    process_reorganize_job,
    queued_reorganize_job_ids,
    request_reorganize,
)
from ...models import ProcessingJob


class Command(BaseCommand):
    help = (
        "Plan (dry run, the default) or apply moving archived files into the folder each "
        "Book's current metadata implies under BOOK_SCANNER_ARCHIVE_LAYOUT. Applying "
        "copies, verifies the SHA-256, re-points the record and only then deletes the old "
        "copy. Files referenced in place (read-only locations) are never touched, and "
        "index-in-place sources cannot be selected."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--book", type=int, action="append", default=[], metavar="ID")
        parser.add_argument(
            "--source",
            type=int,
            action="append",
            default=[],
            metavar="ID",
            help="Books imported from this COPY/MOVE source (repeatable).",
        )
        parser.add_argument("--all", action="store_true", help="Every book in the library.")
        parser.add_argument(
            "--apply",
            metavar="PLAN_JOB_ID",
            help="Apply a finished dry-run plan (its ProcessingJob id).",
        )
        parser.add_argument(
            "--run-queued",
            action="store_true",
            help="Run reorganize jobs queued from the admin (deployments without a worker).",
        )
        parser.add_argument("--json", action="store_true", help="Print the job details as JSON.")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        if options["run_queued"]:
            for job_id in queued_reorganize_job_ids():
                self._run(job_id, as_json=bool(options["json"]))
            return
        plan_job = None
        if options["apply"]:
            plan_job = ProcessingJob.objects.filter(job_id=options["apply"]).first()
            if plan_job is None:
                raise CommandError(f"unknown plan job {options['apply']}")
        elif not (options["book"] or options["source"] or options["all"]):
            raise CommandError("select --book, --source or --all (or --apply a plan)")
        try:
            job, _ = request_reorganize(
                book_ids=options["book"],
                source_location_ids=options["source"],
                all_books=bool(options["all"]),
                apply_plan_job=plan_job,
            )
        except ReorganizeError as exc:
            raise CommandError(str(exc)) from exc
        self._run(str(job.job_id), as_json=bool(options["json"]))

    def _run(self, job_id: str, *, as_json: bool) -> None:
        # Runs in this process; an enqueued copy finding it finished is a no-op.
        process_reorganize_job(job_id)
        job = ProcessingJob.objects.get(job_id=job_id)
        details = job.details if isinstance(job.details, dict) else {}
        if as_json:
            self.stdout.write(
                json.dumps({"job_id": job_id, "state": job.state, **details}, indent=2)
            )
            return
        self.stdout.write(
            f"{job_id} {details.get('mode')} {job.state}: {details.get('summary', '')}"
        )
        for entry in details.get("plan") or []:
            self.stdout.write(
                f"  {entry['kind']}:{entry['id']} {entry['from']} -> "
                f"{entry['to_folder']}/{entry['name']}",
            )
        for result in details.get("results") or []:
            if result["status"] != "moved":
                self.stdout.write(
                    f"  {result['kind']}:{result['id']} {result['status']}: {result['detail']}"
                )
        if details.get("mode") == "plan" and details.get("plan"):
            self.stdout.write(f"Apply with: manage.py reorganize_archive --apply {job_id}")
        if job.state == ProcessingJob.JobState.FAILED:
            raise CommandError(details.get("error") or "reorganize failed")
