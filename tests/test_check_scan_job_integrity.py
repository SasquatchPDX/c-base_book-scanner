"""``manage.py check_scan_job_integrity`` (migration rehearsal post-checks)."""

from __future__ import annotations

import json
import uuid
from io import StringIO
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError

from ebook_server.core.management.commands.check_scan_job_integrity import (
    canonical_scan_job_id,
    compare_snapshots,
    scan_job_reference_columns,
    take_snapshot,
)
from ebook_server.core.models import ScanJob, ScanPage, ScanTailorStageState, ScanTodo

pytestmark = pytest.mark.django_db


def _seed() -> list[ScanJob]:
    jobs = [
        ScanJob.objects.create(id=str(uuid.uuid4())),
        ScanJob.objects.create(id=str(uuid.uuid4())),
        ScanJob.objects.create(id="20261117T143201Z", source=ScanJob.Source.SCANNER),
    ]
    for job in jobs:
        page = ScanPage.objects.create(scan_job=job, logical_sequence_number=1)
        ScanTodo.objects.create(scan_job=job, scan_page=page)
        ScanTailorStageState.objects.create(scan_job=job, scan_page=page, stage="deskew")
    return jobs


def test_canonical_scan_job_id_normalizes_uuid_spellings() -> None:
    value = uuid.uuid4()
    assert canonical_scan_job_id(value) == str(value)
    assert canonical_scan_job_id(value.hex.upper()) == str(value)
    assert canonical_scan_job_id("20261117T143201Z-2") == "20261117T143201Z-2"
    assert canonical_scan_job_id("not-an-id") == "not-an-id"


def test_reference_columns_cover_the_scan_tables() -> None:
    tables = {table for table, _ in scan_job_reference_columns()}
    assert {"core_scanpage", "core_scantodo", "core_scantailorstagestate"} <= tables


def test_snapshot_records_ids_and_counts() -> None:
    jobs = _seed()
    snapshot = take_snapshot()
    assert snapshot["scan_job_ids"] == sorted(str(job.pk) for job in jobs)
    assert snapshot["tables"]["core_scanjob"] == 3
    assert snapshot["tables"]["core_scanpage"] == 3
    assert snapshot["references"]["core_scantodo.scan_job_id"] == 3
    assert snapshot["references"]["core_scantailorstagestate.scan_job_id"] == 3
    assert not any(snapshot["dangling"].values())


def test_compare_accepts_non_canonical_ids_in_the_old_snapshot() -> None:
    _seed()
    after = take_snapshot()
    before = dict(after)
    before["scan_job_ids"] = [
        uuid.UUID(value).hex.upper() if "-" in value else value for value in after["scan_job_ids"]
    ]
    assert compare_snapshots(before, after) == []


def test_compare_reports_lost_jobs_and_changed_counts() -> None:
    _seed()
    before = take_snapshot()
    after = json.loads(json.dumps(before))
    lost = after["scan_job_ids"].pop()
    after["tables"]["core_scanpage"] -= 1
    after["references"]["core_scantodo.scan_job_id"] -= 1
    del after["tables"]["core_scantodo"]
    after["dangling"]["core_scanpage.scan_job_id"] = 2
    problems = compare_snapshots(before, after)
    assert any(lost in problem for problem in problems)
    assert any(problem.startswith("core_scanpage: 3 rows before, 2 after") for problem in problems)
    assert any(problem.startswith("core_scantodo.scan_job_id") for problem in problems)
    assert any("core_scantodo: present before" in problem for problem in problems)
    assert any("does not exist" in problem for problem in problems)


def test_command_snapshot_then_compare_passes_and_leaves_no_user(tmp_path: Path) -> None:
    _seed()
    snapshot = tmp_path / "before.json"
    call_command("check_scan_job_integrity", "--snapshot", str(snapshot), stdout=StringIO())
    assert json.loads(snapshot.read_text())["tables"]["core_scanjob"] == 3
    users_before = get_user_model().objects.count()
    out = StringIO()
    call_command(
        "check_scan_job_integrity", "--compare", str(snapshot), "--sample", "5", stdout=out
    )
    assert "sample scan-job URLs checked: 3" in out.getvalue()
    assert "integrity: OK" in out.getvalue()
    assert get_user_model().objects.count() == users_before


def test_command_compare_fails_when_a_job_disappears(tmp_path: Path) -> None:
    jobs = _seed()
    snapshot = tmp_path / "before.json"
    call_command("check_scan_job_integrity", "--snapshot", str(snapshot), stdout=StringIO())
    jobs[0].delete()
    with pytest.raises(CommandError, match="integrity problem"):
        call_command(
            "check_scan_job_integrity",
            "--compare",
            str(snapshot),
            stdout=StringIO(),
            stderr=StringIO(),
        )


def test_command_compare_rejects_unreadable_snapshot(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="cannot read snapshot"):
        call_command("check_scan_job_integrity", "--compare", str(tmp_path / "missing.json"))
