"""The editor runs the active stage by itself; newer settings supersede a run (#209)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from django.conf import settings
from django.test import Client
from PIL import Image

from ebook_server.core import cylindrical, scantailor
from ebook_server.core.models import ProcessingJob, ScanJob, ScanPage, ScanTailorStageState
from ebook_server.core.stage_cancel import StageSuperseded, cancellable, checkpoint
from tests.stage_helpers import stage_input
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page
from tests.test_scantailor import _stage_url, _stages_url, _text_page


def _ensure_url(job_id: str, page_id: str, stage: str) -> str:
    return f"{_stage_url(job_id, page_id, stage)}ensure/"


def _page(user: str) -> tuple[Client, ScanJob, ScanPage]:
    client, job = _create_scan_job(user)
    buffer = io.BytesIO()
    _text_page(300, 400).save(buffer, format="PNG")
    _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=buffer.getvalue(), filename="p.png"
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    client.get(_stages_url(str(job.id), str(page.id)))  # the editor's workspace load
    return client, job, page


def _stage_jobs(stage: str) -> list[ProcessingJob]:
    return [
        job
        for job in ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.STAGE).order_by("id")
        if (job.details or {}).get("stage") == stage
    ]


def _state(page: ScanPage, stage: str) -> ScanTailorStageState:
    return ScanTailorStageState.objects.get(scan_page=page, stage=stage)


@pytest.fixture
def held_jobs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Queue stage jobs without running them; the test runs them when it wants to."""
    held: list[str] = []

    def hold(job_id: str, *, at_front: bool = True) -> None:
        del at_front
        held.append(job_id)

    monkeypatch.setattr(scantailor, "dispatch_stage_job", hold)
    return held


@pytest.mark.django_db(transaction=True)
def test_opening_a_page_queues_one_job_and_reopening_it_none(held_jobs: list[str]) -> None:
    client, job, page = _page("auto-run-open")
    url = _ensure_url(str(job.id), str(page.id), "deskew")

    first = client.post(url)
    assert first.status_code == 200
    assert first.json()["outcome"] == "queued"
    # Opening it again (or a second editor) while it is queued adds nothing.
    again = client.post(url)
    assert again.json()["outcome"] == "queued"
    assert again.json()["job"]["job_id"] == first.json()["job"]["job_id"]
    assert len(_stage_jobs("deskew")) == 1

    scantailor.process_stage_job(held_jobs[0])
    assert _state(page, "deskew").status == ScanTailorStageState.StageStatus.SUCCESS

    reopened = client.post(url)
    assert reopened.json()["outcome"] == "current"
    assert reopened.json()["job"] is None
    assert len(_stage_jobs("deskew")) == 1


@pytest.mark.django_db(transaction=True)
def test_a_changed_setting_queues_a_run_and_identical_settings_do_not(
    held_jobs: list[str],
) -> None:
    client, job, page = _page("auto-run-edit")
    url = _stage_url(str(job.id), str(page.id), "deskew")

    changed = client.post(
        url, json.dumps({"parameters": {"manual_angle": 1.5}}), content_type="application/json"
    )
    assert changed.status_code == 202
    assert changed.json()["outcome"] == "queued"
    scantailor.process_stage_job(held_jobs[-1])
    state = _state(page, "deskew")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    revision = state.revision

    same = client.post(
        url, json.dumps({"parameters": {"manual_angle": 1.5}}), content_type="application/json"
    )
    assert same.status_code == 200
    assert same.json()["unchanged"] is True
    assert same.json()["outcome"] == "current"
    assert _state(page, "deskew").revision == revision
    assert len(_stage_jobs("deskew")) == 1

    other = client.post(
        url, json.dumps({"parameters": {"manual_angle": -2.0}}), content_type="application/json"
    )
    assert other.status_code == 202
    assert _state(page, "deskew").revision == revision + 1
    assert len(_stage_jobs("deskew")) == 2


@pytest.mark.django_db(transaction=True)
def test_a_newer_run_drops_the_older_queued_job(held_jobs: list[str]) -> None:
    client, job, page = _page("auto-run-dedupe")
    url = _stage_url(str(job.id), str(page.id), "deskew")
    for angle in (1.0, 2.0):
        parameters = {"parameters": {"manual_angle": angle}}
        response = client.post(url, json.dumps(parameters), content_type="application/json")
        assert response.status_code == 202
    older, newer = _stage_jobs("deskew")
    older.refresh_from_db()
    assert older.details["superseded"] is True
    assert newer.state == ProcessingJob.JobState.QUEUED

    scantailor.process_stage_job(str(older.job_id))  # dequeued late: a no-op
    assert _state(page, "deskew").status == ScanTailorStageState.StageStatus.QUEUED
    scantailor.process_stage_job(str(newer.job_id))
    state = _state(page, "deskew")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.parameters["angle"] == 2.0


def _scan_files() -> set[str]:
    root = Path(settings.APP_DATA_DIR) / "scan-artifacts" / scantailor.STAGE_NAMESPACE
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


@pytest.mark.django_db(transaction=True)
def test_an_edit_during_a_run_supersedes_it_before_encoding(
    held_jobs: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    client, job, page = _page("auto-run-cancel")
    url = _stage_url(str(job.id), str(page.id), "deskew")
    response = client.post(
        url, json.dumps({"parameters": {"manual_angle": 1.0}}), content_type="application/json"
    )
    first_job = held_jobs[-1]

    runner = scantailor._run_deskew

    def edited_while_running(state: Any, given: scantailor.StageInput) -> Any:
        if state.revision == response.json()["stage"]["revision"]:
            # The member changes the angle while the worker computes the old one.
            newer = client.post(
                url,
                json.dumps({"parameters": {"manual_angle": 3.0}}),
                content_type="application/json",
            )
            assert newer.status_code == 202
        return runner(state, given)

    encoded: list[int] = []
    write = scantailor._write_stage_artifacts

    def counting_write(result: Any, given: Any, *paths: Any) -> None:
        encoded.append(1)
        write(result, given, *paths)

    monkeypatch.setitem(scantailor._STAGE_RUNNERS["deskew"], "skew_finder", edited_while_running)
    monkeypatch.setattr(scantailor, "_write_stage_artifacts", counting_write)
    before = _scan_files()

    scantailor.process_stage_job(first_job)
    superseded = ProcessingJob.objects.get(job_id=first_job)
    assert superseded.details["superseded"] is True
    assert encoded == []  # stopped before its encode step
    assert _scan_files() == before  # and left no files behind
    state = _state(page, "deskew")
    assert state.status == ScanTailorStageState.StageStatus.QUEUED
    assert not state.output_storage_identifier

    scantailor.process_stage_job(held_jobs[-1])
    state = _state(page, "deskew")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.parameters["angle"] == 3.0  # only the last revision's result
    assert encoded == [1]
    namespace = f"{job.id}/{page.id}/deskew/"
    assert {name for name in _scan_files() if name.startswith(namespace)} == {
        f"{namespace}v{state.revision}/{name}"
        for name in ("work.png", "preview.jpg", scantailor.STAGE_THUMBNAIL_NAME)
    }


@pytest.mark.django_db(transaction=True)
def test_a_stage_waits_for_its_predecessor_and_runs_after_it() -> None:
    client, job, page = _page("auto-run-waiting")
    content = client.post(
        _stage_url(str(job.id), str(page.id), "select_content"),
        json.dumps({"parameters": {"content_mode": "disabled"}}),
        content_type="application/json",
    )
    assert content.status_code == 202
    assert content.json()["outcome"] == "waiting"
    assert content.json()["waiting_for"] == "deskew"
    assert content.json()["job"] is None
    assert _stage_jobs("select_content") == []
    ensure = client.post(_ensure_url(str(job.id), str(page.id), "select_content")).json()
    assert (ensure["outcome"], ensure["waiting_for"]) == ("waiting", "deskew")
    assert _stage_jobs("select_content") == []
    assert _state(page, "select_content").status == ScanTailorStageState.StageStatus.UNKNOWN

    # Deskew finishes: Select Content runs without another request, then the rest.
    deskew = client.post(_ensure_url(str(job.id), str(page.id), "deskew")).json()
    assert deskew["outcome"] == "queued"
    selected = _state(page, "select_content")
    assert selected.status == ScanTailorStageState.StageStatus.SUCCESS, selected.last_error
    assert selected.parameters["content_mode"] == "disabled"
    assert len(_stage_jobs("select_content")) == 1
    for stage in scantailor.stage_order():
        assert _state(page, stage).status == ScanTailorStageState.StageStatus.SUCCESS
    assert not ProcessingJob.objects.filter(state=ProcessingJob.JobState.FAILED).exists()


def test_long_steps_stop_between_tiles_and_column_blocks() -> None:
    page = _text_page(300, 400)
    with cancellable(lambda: False), pytest.raises(StageSuperseded):
        scantailor._threshold_tiles(page, "sauvola", {})
    # Output's copy fallback does not swallow the cancellation.
    state = ScanTailorStageState(stage="output", parameters={"output_mode": "sauvola"})
    with cancellable(lambda: False), pytest.raises(StageSuperseded):
        scantailor._run_output(state, stage_input(page))
    top, bottom = cylindrical.trivial_curves(300, 400)
    dewarper = cylindrical.CylindricalSurfaceDewarper(top, bottom, 2.0)
    with cancellable(lambda: False), pytest.raises(StageSuperseded):
        cylindrical.dewarp_image(
            np.asarray(Image.new("L", (300, 400), 255)), (300, 400), dewarper, (0, 0, 300, 400), 255
        )
    # Outside a worker run the checks do nothing.
    checkpoint(force=True)
    assert scantailor._threshold_tiles(page, "sauvola", {}).size == page.size
