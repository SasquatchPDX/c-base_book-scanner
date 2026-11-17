"""Detect drift in files indexed in place (missing or changed on their share)."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ...referenced_files import check_referenced_files


class Command(BaseCommand):
    help = (
        "Compare every indexed-in-place FileVariant with its file on storage. Missing or "
        "changed files are flagged (reference_state) and no longer served. Read-only: "
        "never modifies files on storage."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--verify-hashes",
            action="store_true",
            help="Re-hash every referenced file, not only those whose size/mtime changed.",
        )
        parser.add_argument("--json", action="store_true", help="Emit a JSON report.")

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        report = check_referenced_files(verify_hashes=bool(options["verify_hashes"]))
        if options["json"]:
            self.stdout.write(
                json.dumps(
                    {
                        "checked": report.checked,
                        "ok": report.ok,
                        "missing": report.missing,
                        "changed": report.changed,
                        "restored": report.restored,
                        "errors": report.errors,
                    },
                    indent=2,
                ),
            )
        else:
            self.stdout.write(
                f"checked={report.checked} ok={report.ok} missing={len(report.missing)} "
                f"changed={len(report.changed)} restored={len(report.restored)} "
                f"errors={len(report.errors)}",
            )
            for variant_id in report.missing:
                self.stderr.write(f"MISSING file_variant:{variant_id}")
            for variant_id in report.changed:
                self.stderr.write(f"CHANGED file_variant:{variant_id}")
            for error in report.errors:
                self.stderr.write(f"ERROR {error}")
        if report.missing or report.changed or report.errors:
            raise CommandError("referenced file check found drift or errors")
