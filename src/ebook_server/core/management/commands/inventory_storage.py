"""Report storage ownership without creating locations, directories or artifacts."""

from __future__ import annotations

import json
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ...storage_inventory import inventory


class Command(BaseCommand):
    help = "Read-only JSON inventory of referenced, missing and unreferenced artifacts."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--location", action="append", dest="locations")
        parser.add_argument("--min-age-days", type=int, default=30)
        parser.add_argument("--limit", type=int, default=10000)
        parser.add_argument("--verify-hashes", action="store_true")

    def handle(self, *args: Any, **options: Any) -> None:
        if options["min_age_days"] < 1 or options["limit"] < 1:
            raise CommandError("age and entry limit must be positive")
        report = inventory(
            locations=options["locations"],
            min_age_days=options["min_age_days"],
            limit=options["limit"],
            verify_hashes=options["verify_hashes"],
        )
        self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
