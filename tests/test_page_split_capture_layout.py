"""Capture layout decides whether Page Split is a recorded passthrough (#55)."""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest
from django.db import connection
from django.test import Client, override_settings

from ebook_server.core import scantailor
from ebook_server.core.models import (
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanTailorStageState,
    get_or_create_profile,
)
from tests.test_scan_jobs import _create_scan_job, _put_scan_page, _upload_scan_page
from tests.test_scantailor import _facing_page_bytes, _stage_preview_size, _stage_url


@pytest.fixture(autouse=True)
def _app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    return tmp_path


def _split_state(job: ScanJob, page: ScanPage) -> ScanTailorStageState:
    return ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")


def _upload(client: object, job: ScanJob, *, side: str = "single", payload: bytes) -> ScanPage:
    response = _upload_scan_page(
        client,  # type: ignore[arg-type]
        str(job.id),
        sequence=1,
        side=side,
        payload=payload,
        filename="page.png",
    )
    assert response.status_code == 201
    return ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side=side)


@pytest.mark.django_db(transaction=True)
def test_new_jobs_default_to_single_page_and_layout_is_validated() -> None:
    client, job = _create_scan_job("layout-default")
    assert job.capture_layout == ScanJob.CaptureLayout.SINGLE_PAGE
    detail = client.get(f"/scan/jobs/{job.id}/", HTTP_ACCEPT="application/json")
    assert detail.json()["capture_layout"] == "single_page"

    _, spread = _create_scan_job("layout-explicit", capture_layout="spread")
    assert spread.capture_layout == ScanJob.CaptureLayout.SPREAD

    bad = client.post("/scan/jobs/", data={"capture_layout": "triptych"})
    assert bad.status_code == 400
    # The HTML form's "Scanner default" option sends an empty value.
    blank = client.post("/scan/jobs/", data={"capture_layout": ""})
    assert blank.json()["capture_layout"] == "single_page"


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_SCAN_SPREAD_SCANNER_IDS=["overhead-cam"])
def test_spread_scanner_ids_and_default_setting_choose_the_layout() -> None:
    client, _ = _create_scan_job("layout-scanner")
    by_scanner = client.post("/scan/jobs/", data={"scanner_id": "overhead-cam"})
    assert by_scanner.json()["capture_layout"] == "spread"
    explicit = client.post(
        "/scan/jobs/", data={"scanner_id": "overhead-cam", "capture_layout": "single_page"}
    )
    assert explicit.json()["capture_layout"] == "single_page"
    with override_settings(BOOK_SCANNER_SCAN_DEFAULT_CAPTURE_LAYOUT="spread"):
        other = client.post("/scan/jobs/", data={"scanner_id": "desk-01"})
    assert other.json()["capture_layout"] == "spread"


@pytest.mark.django_db(transaction=True)
def test_single_page_upload_auto_completes_page_split_as_recorded_passthrough() -> None:
    client, job = _create_scan_job("layout-auto")
    # A landscape image with a gutter-like gap: in a single-page job it is never split.
    page = _upload(client, job, payload=_facing_page_bytes())

    state = _split_state(job, page)
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.algorithm == scantailor.SINGLE_PAGE_SPLIT_ALGORITHM
    assert state.algorithm_version == "splitter-v2"
    assert state.parameters["split_mode"] == "none"
    assert state.parameters["resolved_split_mode"] == "none"
    assert state.parameters["split_skipped"] is True
    assert state.parameters["split_skipped_reason"] == scantailor.SINGLE_PAGE_SPLIT_REASON
    assert state.parameters["capture_layout"] == "single_page"
    assert state.parameters["output_width"] == state.parameters["input_width"] == 420
    assert state.output_sha256
    assert "resolved_split_x" not in state.parameters
    job_row = ProcessingJob.objects.get(job_id=state.worker_job_id)
    assert job_row.state == ProcessingJob.JobState.SUCCESS
    assert scantailor.is_auto_skipped_split(state)
    assert _stage_preview_size(client, str(job.id), str(page.id), "page_split") == (420, 280)

    stages = client.get(f"/scan/jobs/{job.id}/pages/{page.id}/stages/").json()["stages"]
    by_stage = {item["stage"]: item for item in stages}
    assert by_stage["page_split"]["auto_skipped"] is True
    assert by_stage["deskew"]["auto_skipped"] is False

    html = client.get(f"/scan/jobs/{job.id}/workspace/", HTTP_ACCEPT="text/html").content
    assert b"skipped \xc2\xb7 single page" in html

    # Downstream stages consume the passthrough output like any other split output.
    deskew = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 0}}),
        content_type="application/json",
    )
    assert deskew.status_code == 202
    deskew_state = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    assert deskew_state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert deskew_state.input_stage_state_id == state.id


@pytest.mark.django_db(transaction=True)
def test_manual_override_splits_invalidates_downstream_and_survives_rescan() -> None:
    client, job = _create_scan_job("layout-override")
    page = _upload(client, job, side="left", payload=_facing_page_bytes(split=230))
    client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 0}}),
        content_type="application/json",
    )

    override = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "manual", "split_x": 140}}),
        content_type="application/json",
    )
    assert override.status_code == 202
    state = _split_state(job, page)
    assert state.algorithm == "manual_split"
    assert state.parameters["resolved_split_x"] == 140
    assert state.parameters["resolved_split_side"] == "left"
    assert "split_skipped" not in state.parameters
    assert not scantailor.is_auto_skipped_split(state)
    deskew = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    assert deskew.status == ScanTailorStageState.StageStatus.INVALIDATED
    assert deskew.invalidated_reason == "page_split updated"

    # A replacement capture does not silently undo the per-page override.
    revision = state.revision
    assert _put_scan_page(client, page.id, _facing_page_bytes(split=232)).status_code == 201
    state.refresh_from_db()
    assert state.parameters["split_mode"] == "manual"
    assert state.status == ScanTailorStageState.StageStatus.INVALIDATED
    assert state.revision == revision

    # Switching back to "none" restores the recorded passthrough.
    back = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "none"}}),
        content_type="application/json",
    )
    assert back.status_code == 202
    state.refresh_from_db()
    assert state.algorithm == scantailor.SINGLE_PAGE_SPLIT_ALGORITHM
    assert scantailor.is_auto_skipped_split(state)
    assert "resolved_split_x" not in state.parameters
    assert state.parameters["output_width"] == 420


@pytest.mark.django_db(transaction=True)
def test_rescan_reruns_the_automatic_passthrough() -> None:
    client, job = _create_scan_job("layout-rescan")
    page = _upload(client, job, payload=_facing_page_bytes())
    first = _split_state(job, page)
    first_revision, first_input = first.revision, first.input_scan_page_version_id

    assert _put_scan_page(client, page.id, _facing_page_bytes(split=200)).status_code == 201
    state = _split_state(job, page)
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.revision > first_revision
    assert state.input_scan_page_version_id != first_input
    assert scantailor.is_auto_skipped_split(state)


@pytest.mark.django_db(transaction=True)
def test_spread_job_keeps_gutter_detection_and_is_not_auto_run() -> None:
    client, job = _create_scan_job("layout-spread", capture_layout="spread")
    page = _upload(client, job, side="left", payload=_facing_page_bytes())
    assert not ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.STAGE).exists()

    response = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 202
    state = _split_state(job, page)
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.parameters["resolved_split_side"] in {"left", "left-heuristic"}
    assert state.parameters["output_width"] < 240
    assert not scantailor.is_auto_skipped_split(state)


@pytest.mark.django_db(transaction=True)
def test_failed_dispatch_does_not_fail_the_upload(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_dispatch(job_id: str) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(scantailor, "dispatch_stage_job", broken_dispatch)
    client, job = _create_scan_job("layout-dispatch")
    page = _upload(client, job, payload=_facing_page_bytes())

    state = _split_state(job, page)
    assert state.status == ScanTailorStageState.StageStatus.FAILED
    assert "could not be queued" in state.last_error
    assert ProcessingJob.objects.get(job_id=state.worker_job_id).state == (
        ProcessingJob.JobState.FAILED
    )


def _manual_split_from_another_session(job: ScanJob, page: ScanPage) -> int:
    """POST a manual split as a second user, on its own thread and DB connection."""
    status: list[int] = []

    def run() -> None:
        try:
            other = Client()
            other.force_login(get_or_create_profile("layout-race-editor").user)
            response = other.post(
                _stage_url(str(job.id), str(page.id), "page_split"),
                data=json.dumps({"parameters": {"split_mode": "manual", "split_x": 140}}),
                content_type="application/json",
            )
            status.append(response.status_code)
        finally:
            connection.close()

    thread = threading.Thread(target=run)
    thread.start()
    thread.join(timeout=60)
    assert not thread.is_alive()
    assert status, "manual split request did not complete"
    return status[0]


def _assert_manual_split_kept(job: ScanJob, page: ScanPage, manual_revision: int) -> None:
    state = _split_state(job, page)
    assert state.revision == manual_revision
    assert state.parameters["split_mode"] == "manual"
    assert state.algorithm == "manual_split"
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.parameters["resolved_split_x"] == 140
    assert state.parameters["output_width"] < 420
    assert not scantailor.is_auto_skipped_split(state)
    assert f"/v{manual_revision}/" in state.output_storage_identifier
    owner = ProcessingJob.objects.get(job_id=state.worker_job_id)
    assert owner.details["automatic"] is False
    assert owner.details["stage_revision"] == manual_revision
    assert owner.state == ProcessingJob.JobState.SUCCESS
    assert not owner.details.get("superseded")


def run_reviewer_interleaving(monkeypatch: pytest.MonkeyPatch, user: str) -> None:
    """Reviewer interleaving on PR #102, shared with the PostgreSQL suite.

    During a rescan PUT the automatic passthrough reads the Page Split stage (mode
    "none", revision N); a manual split POST then lands from another session
    (revision N+1, cropped output). Automation must neither reset the mode nor reuse
    revision N+1 nor replace the manual artifact.
    """
    client, job = _create_scan_job(user)
    page = _upload(client, job, side="left", payload=_facing_page_bytes(split=230))
    observed_revision = _split_state(job, page).revision

    real_get_or_create = scantailor.get_or_create_stage_state
    armed = [True]
    manual_status: list[int] = []

    def read_then_interleave(**kwargs: str) -> ScanTailorStageState:
        state = real_get_or_create(**kwargs)
        if armed[0] and kwargs.get("stage") == "page_split":
            armed[0] = False
            assert scantailor.effective_split_mode(state) == "none"
            manual_status.append(_manual_split_from_another_session(job, page))
        return state

    monkeypatch.setattr(scantailor, "get_or_create_stage_state", read_then_interleave)
    stage_jobs = ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.STAGE)
    jobs_before = set(stage_jobs.values_list("job_id", flat=True))

    assert _put_scan_page(client, page.id, _facing_page_bytes(split=232)).status_code == 201

    assert manual_status == [202]
    _assert_manual_split_kept(job, page, observed_revision + 1)
    # Only the manual run was queued; automation queued nothing.
    new_jobs = stage_jobs.exclude(job_id__in=jobs_before)
    assert [row.details["automatic"] for row in new_jobs] == [False]


@pytest.mark.django_db(transaction=True)
def test_manual_split_interleaved_after_passthrough_read_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_reviewer_interleaving(monkeypatch, "layout-race-read")


@pytest.mark.django_db(transaction=True)
def test_late_automatic_job_is_a_noop_after_a_manual_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A passthrough job overtaken by a manual edit must not replace its artifact."""
    client, job = _create_scan_job("layout-race-late")
    page = _upload(client, job, side="left", payload=_facing_page_bytes(split=230))

    real_dispatch = scantailor.dispatch_stage_job
    held: list[str] = []

    def hold_automatic(job_id: str) -> None:
        row = ProcessingJob.objects.get(job_id=job_id)
        if row.details.get("automatic"):
            held.append(job_id)
            return
        real_dispatch(job_id)

    monkeypatch.setattr(scantailor, "dispatch_stage_job", hold_automatic)
    assert _put_scan_page(client, page.id, _facing_page_bytes(split=232)).status_code == 201
    assert len(held) == 1
    auto_revision = _split_state(job, page).revision

    assert _manual_split_from_another_session(job, page) == 202
    _assert_manual_split_kept(job, page, auto_revision + 1)

    scantailor.process_stage_job(held[0])  # the queued passthrough finally runs

    late = ProcessingJob.objects.get(job_id=held[0])
    assert late.details["superseded"] is True
    assert late.details["superseded_reason"] == "stage revision changed after dispatch"
    _assert_manual_split_kept(job, page, auto_revision + 1)


@pytest.mark.django_db(transaction=True)
def test_dispatch_for_a_stale_revision_is_superseded_without_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job queued for an old revision never takes over the stage's worker slot."""
    client, job = _create_scan_job("layout-race-dispatch")
    page = _upload(client, job, side="left", payload=_facing_page_bytes(split=230))
    assert _manual_split_from_another_session(job, page) == 202
    state = _split_state(job, page)
    stale = ScanTailorStageState.objects.get(pk=state.pk)
    stale.revision -= 1  # a copy read before the manual edit

    ran: list[str] = []
    monkeypatch.setattr(scantailor, "dispatch_stage_job", ran.append)
    queued = scantailor.request_stage_job(state=stale, automatic=True)

    assert ran == []
    assert queued.details["superseded"] is True
    assert queued.details["superseded_reason"] == "stage revision changed before dispatch"
    _assert_manual_split_kept(job, page, state.revision)
