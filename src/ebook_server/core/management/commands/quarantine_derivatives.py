"""Apply a reviewed inventory plan, or restore one journal, explicitly and reversibly."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ...derivative_retention import RetentionError, collect_plan, restore_manifest
from ...models import StorageLocation


class Command(BaseCommand):
    help = "Dry-run a derivative quarantine plan or restore manifest; --apply changes names, never purges bytes."

    def add_arguments(self, parser: CommandParser) -> None:
        source = parser.add_mutually_exclusive_group(required=True)
        source.add_argument("--plan")
        source.add_argument("--restore")
        parser.add_argument(
            "--location", default="scan", help="location holding a restore manifest"
        )
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--limit", type=int, default=100)

    def handle(self, *args: Any, **options: Any) -> None:
        try:
            if not 1 <= options["limit"] <= 1000:
                raise RetentionError("limit must be between 1 and 1000")
            if options["plan"]:
                path = Path(options["plan"])
                if path.stat().st_size > 16 * 1024 * 1024:
                    raise RetentionError("inventory plan exceeds 16 MiB")
                results = collect_plan(
                    json.loads(path.read_text(encoding="utf-8")),
                    apply=options["apply"],
                    limit=options["limit"],
                )
            else:
                location = StorageLocation.objects.get(identifier=options["location"])
                results = [restore_manifest(location, options["restore"], apply=options["apply"])]
        except (OSError, ValueError, KeyError, RetentionError, StorageLocation.DoesNotExist) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(
            json.dumps(
                {"mode": "apply" if options["apply"] else "dry-run", "results": results}, indent=2
            )
        )
