"""Detect drift in files indexed in place (missing or changed on their share)."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ...models import ProcessingJob, StorageLocation
from ...referenced_files import (
    check_referenced_files,
    process_reference_check_job,
    queued_reference_check_ids,
)


class Command(BaseCommand):
    help = (
        "Compare indexed-in-place FileVariants with their files on storage. Missing or "
        "changed files are flagged (reference_state) and no longer served. A location "
        "that is not mounted (missing sentinel_file, or an empty bare mount point) is "
        "reported as an outage and nothing on it is flagged. Read-only: never modifies "
        "files on storage."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--verify-hashes",
            action="store_true",
            help="Re-hash every referenced file, not only those whose size/mtime changed.",
        )
        parser.add_argument(
            "--location",
            action="append",
            default=None,
            metavar="IDENTIFIER",
            help="Only check variants on this storage location (repeatable).",
        )
        parser.add_argument(
            "--run-queued",
            action="store_true",
            help=(
                "Instead of a direct check, run the reference-check jobs queued from the "
                "admin (for deployments without a worker queue)."
            ),
        )
        parser.add_argument("--json", action="store_true", help="Emit a JSON report.")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        if options["run_queued"]:
            self._run_queued()
            return
        location_ids = None
        if options["location"]:
            identifiers = list(options["location"])
            found = dict(
                StorageLocation.objects.filter(identifier__in=identifiers).values_list(
                    "identifier", "pk"
                ),
            )
            unknown = sorted(set(identifiers) - set(found))
            if unknown:
                raise CommandError(f"unknown storage location(s): {', '.join(unknown)}")
            location_ids = list(found.values())
        report = check_referenced_files(
            verify_hashes=bool(options["verify_hashes"]),
            storage_location_ids=location_ids,
        )
        if options["json"]:
            self.stdout.write(json.dumps(report.as_dict(), indent=2))
        else:
            self.stdout.write(report.summary())
            for variant_id in report.missing:
                self.stderr.write(f"MISSING file_variant:{variant_id}")
            for variant_id in report.changed:
                self.stderr.write(f"CHANGED file_variant:{variant_id}")
            for identifier in report.unavailable_locations:
                self.stderr.write(f"UNAVAILABLE storage_location:{identifier}")
            for error in report.errors:
                self.stderr.write(f"ERROR {error}")
        if report.has_findings:
            raise CommandError("referenced file check found drift or errors")

    def _run_queued(self) -> None:
        failed = 0
        job_ids = queued_reference_check_ids()
        for job_id in job_ids:
            process_reference_check_job(job_id)
            job = ProcessingJob.objects.get(job_id=job_id)
            details = job.details if isinstance(job.details, dict) else {}
            self.stdout.write(f"{job_id} {job.state} {details.get('summary', '')}")
            if job.state != ProcessingJob.JobState.SUCCESS:
                failed += 1
        self.stdout.write(f"ran {len(job_ids)} queued reference check(s)")
        if failed:
            raise CommandError(f"{failed} reference check job(s) did not succeed")
