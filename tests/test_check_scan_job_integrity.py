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


def _seed_page_split(jobs: list[ScanJob]) -> None:
    """Pre-0049 Page Split rows; the stage column has no database constraint."""
    for job in jobs[:2]:
        page = ScanPage.objects.get(scan_job=job)
        ScanTailorStageState.objects.create(scan_job=job, scan_page=page, stage="page_split")


def _delete_one(stage: str) -> None:
    state = ScanTailorStageState.objects.filter(stage=stage).first()
    assert state is not None
    state.delete()


def test_snapshot_counts_retired_page_split_rows() -> None:
    _seed_page_split(_seed())
    snapshot = take_snapshot()
    assert snapshot["tables"]["core_scantailorstagestate"] == 5
    assert snapshot["retired"] == {
        "core_scantailorstagestate": 2,
        "core_scantailorstagestate.scan_job_id": 2,
    }


def test_compare_accepts_exactly_the_retired_rows_disappearing() -> None:
    _seed_page_split(_seed())
    before = take_snapshot()
    ScanTailorStageState.objects.filter(stage="page_split").delete()
    after = take_snapshot()
    assert after["retired"]["core_scantailorstagestate"] == 0
    assert compare_snapshots(before, after) == []


def test_compare_still_reports_other_losses_next_to_retired_rows() -> None:
    _seed_page_split(_seed())
    before = take_snapshot()
    # One retired row and one ordinary stage row go: the same net count as half the
    # intended deletion plus a loss, which must not hide behind the retired rows.
    _delete_one("page_split")
    _delete_one("deskew")
    problems = compare_snapshots(before, take_snapshot())
    assert (
        "core_scantailorstagestate: 5 rows before, 3 after (expected 4: 1 retired row(s) removed)"
    ) in problems
    assert any(p.startswith("core_scantailorstagestate.scan_job_id") for p in problems)


def test_compare_reports_a_loss_equal_to_the_retired_count() -> None:
    _seed_page_split(_seed())
    before = take_snapshot()
    # Two ordinary rows lost while the Page Split rows stay: net -2, like the intended
    # deletion, but no retired row went.
    _delete_one("deskew")
    _delete_one("deskew")
    problems = compare_snapshots(before, take_snapshot())
    assert any(p.startswith("core_scantailorstagestate: 5 rows before, 3 after") for p in problems)


def test_compare_rejects_new_retired_rows() -> None:
    jobs = _seed()
    before = take_snapshot()
    _seed_page_split(jobs)
    ScanTailorStageState.objects.filter(stage="deskew").exclude(scan_job=jobs[2]).delete()
    problems = compare_snapshots(before, take_snapshot())
    assert any("2 retired row(s) after, only 0 before" in p for p in problems)


def test_command_compare_reports_removed_retired_rows(tmp_path: Path) -> None:
    _seed_page_split(_seed())
    snapshot = tmp_path / "before.json"
    call_command("check_scan_job_integrity", "--snapshot", str(snapshot), stdout=StringIO())
    ScanTailorStageState.objects.filter(stage="page_split").delete()
    out = StringIO()
    call_command(
        "check_scan_job_integrity", "--compare", str(snapshot), "--sample", "0", stdout=out
    )
    assert "core_scantailorstagestate: 5 -> 3 (2 retired row(s) removed)" in out.getvalue()
    assert "integrity: OK" in out.getvalue()
