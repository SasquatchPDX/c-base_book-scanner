"""Check that scan jobs survive a migration intact (used by scripts/rehearse_migrations.sh).

Two modes:

``--snapshot PATH``
    Record every scan job ID (in canonical form) plus row counts of the scan tables and
    of every foreign-key column that references ``core_scanjob``. Uses raw SQL only, so
    it works on a database at any migration state, including the pre-0041 UUID schema.

``--compare PATH``
    Take the same snapshot of the (migrated) database and compare it with ``PATH``:
    identical job ID sets, unchanged row and foreign-key counts, no dangling references,
    and a sample of scan jobs that open through ``/scan/jobs/<id>/`` with the Django
    test client.

    Rows that a migration retires on purpose (:data:`RETIRED_ROWS`, e.g. the Page Split
    stage states that 0049 deletes) are counted separately. Exactly those rows may
    disappear; every other row must still be there, so an unexpected loss of the same
    size is still reported. The sample request runs inside a transaction that is rolled back, so
    the temporary user and session it needs never persist.

The command exits non-zero (``CommandError``) on any mismatch.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path
from typing import Any

from django.apps import apps as global_apps
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import connection, transaction
from django.test import Client, override_settings
from django.urls import reverse

from ...scan_job_ids import normalize_scan_job_id

SNAPSHOT_VERSION = 2
SCAN_JOB_LABEL = "core.scanjob"
# Row counts reported for these tables when they exist (logs/events arrived later).
SCAN_TABLES = (
    "core_scanjob",
    "core_scanpage",
    "core_scanpageversion",
    "core_scantodo",
    "core_scantailorstagestate",
    "core_scanlog",
    "core_scanevent",
)
# Rows a migration deletes on purpose: table -> (column, value, why). Their count is
# recorded per table and per scan-job reference column of that table.
RETIRED_ROWS: dict[str, tuple[str, str, str]] = {
    "core_scantailorstagestate": ("stage", "page_split", "Page Split stage removed (#195)"),
}


def canonical_scan_job_id(value: object) -> str:
    """Canonical text of a stored scan job ID (UUID objects, hex and uppercase included)."""
    return normalize_scan_job_id(value) or str(value)


def scan_job_reference_columns() -> list[tuple[str, str]]:
    """``(table, column)`` of every concrete foreign key to ScanJob in the current models."""
    columns = set()
    for model in global_apps.get_models():
        for field in model._meta.local_concrete_fields:
            related = getattr(field, "related_model", None)
            if field.many_to_one and related is not None:
                if related._meta.label_lower == SCAN_JOB_LABEL:
                    columns.add((model._meta.db_table, field.column))
    return sorted(columns)


def _count(sql: str) -> int:
    with connection.cursor() as cursor:
        cursor.execute(sql)
        row = cursor.fetchone()
    return int(row[0]) if row else 0


def _literal(value: str) -> str:
    """SQL string literal for a constant from :data:`RETIRED_ROWS` (never user input)."""
    return "'" + value.replace("'", "''") + "'"


def _count_retired(table: str, condition: str) -> int:
    return _count(f"SELECT count(*) FROM {connection.ops.quote_name(table)} WHERE {condition}")


def take_snapshot() -> dict[str, Any]:
    quote = connection.ops.quote_name
    existing = set(connection.introspection.table_names())
    if "core_scanjob" not in existing:
        raise CommandError("core_scanjob does not exist; is this a book-scanner database?")
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {quote('id')} FROM {quote('core_scanjob')}")
        ids = sorted(canonical_scan_job_id(row[0]) for row in cursor.fetchall())
    tables = {
        table: _count(f"SELECT count(*) FROM {quote(table)}")
        for table in SCAN_TABLES
        if table in existing
    }
    references = {}
    dangling = {}
    retired = {}
    for table, (column, value, _why) in RETIRED_ROWS.items():
        if table in existing:
            retired[table] = _count_retired(table, f"{quote(column)} = {_literal(value)}")
    for table, column in scan_job_reference_columns():
        if table not in existing:
            continue
        key = f"{table}.{column}"
        references[key] = _count(
            f"SELECT count(*) FROM {quote(table)} WHERE {quote(column)} IS NOT NULL"
        )
        if table in retired:
            retired_column, value, _why = RETIRED_ROWS[table]
            retired[key] = _count_retired(
                table,
                f"{quote(column)} IS NOT NULL AND {quote(retired_column)} = {_literal(value)}",
            )
        dangling[key] = _count(
            f"SELECT count(*) FROM {quote(table)} AS ref "
            f"WHERE ref.{quote(column)} IS NOT NULL AND NOT EXISTS ("
            f"SELECT 1 FROM {quote('core_scanjob')} AS job "
            f"WHERE job.{quote('id')} = ref.{quote(column)})"
        )
    return {
        "version": SNAPSHOT_VERSION,
        "vendor": connection.vendor,
        "scan_job_ids": ids,
        "tables": tables,
        "references": references,
        "dangling": dangling,
        "retired": retired,
    }


def compare_snapshots(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Human-readable problems; empty when ``after`` preserves everything in ``before``."""
    problems = []
    before_ids = {canonical_scan_job_id(value) for value in before.get("scan_job_ids", [])}
    after_ids = set(after["scan_job_ids"])
    missing = sorted(before_ids - after_ids)
    added = sorted(after_ids - before_ids)
    if missing:
        problems.append(f"{len(missing)} scan job ID(s) no longer resolve, e.g. {missing[:5]}")
    if added:
        problems.append(f"{len(added)} unexpected new scan job ID(s), e.g. {added[:5]}")
    retired_before = before.get("retired", {})
    retired_after = after.get("retired", {})
    for group in ("tables", "references"):
        for key, count in sorted(before.get(group, {}).items()):
            now = after[group].get(key)
            if now is None:
                problems.append(f"{key}: present before ({count} rows), missing after")
                continue
            was_retired = int(retired_before.get(key, 0))
            still_retired = int(retired_after.get(key, 0))
            if still_retired > was_retired:
                problems.append(
                    f"{key}: {still_retired} retired row(s) after, only {was_retired} before"
                )
            # Only the retired rows may go; every other row must survive.
            if now - still_retired != count - was_retired:
                expected = count - was_retired + still_retired
                detail = (
                    f" (expected {expected}: {was_retired - still_retired} retired row(s) removed)"
                    if was_retired
                    else ""
                )
                problems.append(f"{key}: {count} rows before, {now} after{detail}")
    for key, count in sorted(after["dangling"].items()):
        if count:
            problems.append(f"{key}: {count} row(s) reference a scan job that does not exist")
    return problems


def check_sample_urls(job_ids: list[str], sample: int) -> tuple[int, list[str]]:
    """GET ``/scan/jobs/<id>/`` for an evenly spread sample; returns (checked, problems)."""
    if sample <= 0 or not job_ids:
        return 0, []
    step = max(1, len(job_ids) // sample)
    chosen = job_ids[::step][:sample]
    problems: list[str] = []
    allowed_hosts = [*settings.ALLOWED_HOSTS, "testserver"]
    with (
        override_settings(ALLOWED_HOSTS=allowed_hosts, SECURE_SSL_REDIRECT=False),
        transaction.atomic(),
    ):
        user = get_user_model().objects.create_user(
            username=f"integrity-check-{secrets.token_hex(6)}"
        )
        client = Client()
        client.force_login(user)
        for job_id in chosen:
            url = reverse("scan-job-detail", kwargs={"job_id": job_id})
            response = client.get(url, HTTP_ACCEPT="application/json")
            if response.status_code != 200:
                problems.append(f"GET {url}: HTTP {response.status_code}")
                continue
            payload = response.json()
            if payload.get("id") != job_id:
                problems.append(f"GET {url}: returned id {payload.get('id')!r}")
        transaction.set_rollback(True)
    return len(chosen), problems


class Command(BaseCommand):
    help = "Snapshot scan job IDs and reference counts, or compare against a snapshot."

    def add_arguments(self, parser: CommandParser) -> None:
        mode = parser.add_mutually_exclusive_group(required=True)
        mode.add_argument("--snapshot", type=Path, help="Write a snapshot JSON file.")
        mode.add_argument("--compare", type=Path, help="Compare with a snapshot JSON file.")
        parser.add_argument(
            "--sample",
            type=int,
            default=20,
            help="Scan jobs to open through the web view in --compare mode (default 20).",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        snapshot_path: Path | None = options["snapshot"]
        if snapshot_path is not None:
            snapshot = take_snapshot()
            snapshot_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))
            self.stdout.write(
                f"snapshot: {len(snapshot['scan_job_ids'])} scan jobs; "
                + ", ".join(f"{key}={value}" for key, value in snapshot["tables"].items())
            )
            return

        compare_path: Path = options["compare"]
        try:
            before = json.loads(compare_path.read_text())
        except (OSError, ValueError) as exc:
            raise CommandError(f"cannot read snapshot {compare_path}: {exc}") from exc
        after = take_snapshot()
        problems = compare_snapshots(before, after)
        checked, url_problems = check_sample_urls(after["scan_job_ids"], options["sample"])
        problems.extend(url_problems)
        self.stdout.write(
            f"scan job IDs: {len(before.get('scan_job_ids', []))} before, "
            f"{len(after['scan_job_ids'])} after"
        )
        retired_before = before.get("retired", {})
        retired_after = after.get("retired", {})
        for group in ("tables", "references"):
            for key, count in sorted(after[group].items()):
                removed = int(retired_before.get(key, 0)) - int(retired_after.get(key, 0))
                note = f" ({removed} retired row(s) removed)" if removed > 0 else ""
                self.stdout.write(
                    f"  {key}: {before.get(group, {}).get(key, '-')} -> {count}{note}"
                )
        self.stdout.write(f"sample scan-job URLs checked: {checked}")
        if problems:
            for problem in problems:
                self.stderr.write(f"FAIL: {problem}")
            raise CommandError(f"{len(problems)} integrity problem(s)")
        self.stdout.write("integrity: OK")
