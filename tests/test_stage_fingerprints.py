"""Stages are edited in any order; staleness comes from input fingerprints (#205)."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pytest
from django.test import Client

from ebook_server.core import scan_pipeline, scantailor
from ebook_server.core.models import ProcessingJob, ScanPage, ScanTailorStageState
from tests.test_scan_jobs import _create_scan_job, _put_scan_page, _upload_scan_page
from tests.test_scantailor import _text_heavy_bytes

Status = ScanTailorStageState.StageStatus


class _HeldQueue:
    """An RQ stand-in that holds jobs until the test runs them, in queue order."""

    def __init__(self) -> None:
        self.calls: list[tuple[Callable[..., Any], tuple[Any, ...], bool]] = []

    def enqueue(self, func: Callable[..., Any], *args: Any, at_front: bool = False) -> None:
        call = (func, args, at_front)
        if at_front:
            self.calls.insert(0, call)
        else:
            self.calls.append(call)

    def run_next(self) -> None:
        func, args, _ = self.calls.pop(0)
        func(*args)

    def drain(self) -> None:
        while self.calls:
            self.run_next()


@pytest.fixture
def held_queue(monkeypatch: pytest.MonkeyPatch) -> _HeldQueue:
    queue = _HeldQueue()
    monkeypatch.setattr(scantailor, "get_queue", lambda: queue)
    return queue


def _url(job_id: str, page_id: str, stage: str) -> str:
    return f"/scan/jobs/{job_id}/pages/{page_id}/stages/{stage}/"


def _save(client: Client, page: ScanPage, stage: str, parameters: dict[str, Any]) -> Any:
    response = client.post(
        _url(str(page.scan_job_id), str(page.id), stage),
        data=json.dumps({"parameters": parameters}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content
    return response.json()


def _page(client: Client, job_id: str) -> ScanPage:
    _upload_scan_page(
        client,
        job_id,
        sequence=1,
        side="single",
        payload=_text_heavy_bytes(),
        filename="page.png",
    )
    page = ScanPage.objects.get(scan_job_id=job_id, logical_sequence_number=1)
    client.get(f"/scan/jobs/{job_id}/pages/{page.id}/stages/")  # every stage row
    return page


def _states(page: ScanPage) -> dict[str, ScanTailorStageState]:
    states = {state.stage: state for state in ScanTailorStageState.objects.filter(scan_page=page)}
    scantailor.link_page_states(states, page.active_version())
    return states


def _served(client: Client, page: ScanPage) -> dict[str, dict[str, Any]]:
    response = client.get(f"/scan/jobs/{page.scan_job_id}/pages/{page.id}/stages/")
    return {item["stage"]: item for item in response.json()["stages"]}


def _no_failures() -> None:
    assert not ScanTailorStageState.objects.filter(status=Status.FAILED).exists()
    assert not ProcessingJob.objects.filter(
        job_type=ProcessingJob.JobType.STAGE, state=ProcessingJob.JobState.FAILED
    ).exists()


def _stage_jobs() -> int:
    return ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.STAGE).count()


@pytest.mark.django_db(transaction=True)
def test_margins_edited_before_deskew_waits_then_runs_without_another_click() -> None:
    client, job = _create_scan_job("fingerprint-out-of-order")
    page = _page(client, str(job.id))

    saved = _save(client, page, "margins", {"margins_mode": "manual", "margin": 12})
    assert saved["job"] is None
    assert saved["waiting_for"] == "deskew"
    assert saved["stage"]["waiting_for"] == "deskew"
    assert saved["stage"]["current"] is False
    assert _stage_jobs() == 0
    margins = _states(page)["margins"]
    assert margins.parameters == {"margins_mode": "manual", "margin": 12}
    assert margins.status == Status.UNKNOWN
    assert _served(client, page)["margins"]["waiting_for"] == "deskew"
    _no_failures()

    # Deskew runs; Select Content and Dewarping follow with their defaults, then Margins.
    _save(client, page, "deskew", {})
    states = _states(page)
    for stage in scantailor.stage_order():
        assert states[stage].status == Status.SUCCESS, (stage, states[stage].last_error)
        assert scantailor.stage_result_is_current(states[stage]), stage
        assert scantailor.stage_waiting_for(states[stage]) is None
    margins = states["margins"]
    assert margins.parameters["margin"] == 12
    assert margins.applied_input_sha256 == states["dewarping"].output_sha256
    assert margins.parameters["output_width"] == margins.parameters["input_width"] + 24
    _no_failures()


@pytest.mark.django_db(transaction=True)
def test_deskew_edit_after_margins_marks_margins_stale_and_reruns_it(
    held_queue: _HeldQueue,
) -> None:
    client, job = _create_scan_job("fingerprint-upstream-edit")
    page = _page(client, str(job.id))
    _save(client, page, "margins", {"margins_mode": "manual", "margin": 12})
    _save(client, page, "deskew", {})
    held_queue.drain()
    before = _states(page)
    assert all(scantailor.stage_result_is_current(state) for state in before.values())

    _save(client, page, "deskew", {"manual_angle": 2.0})
    states = _states(page)
    margins = states["margins"]
    # Margins keeps its settings and its old result, which is now stale.
    assert margins.parameters["margin"] == 12
    assert margins.status == Status.SUCCESS
    assert margins.output_sha256 == before["margins"].output_sha256
    assert not scantailor.stage_result_is_current(margins)
    assert scantailor.stage_waiting_for(margins) == "deskew"
    assert scantailor.stage_stale_reason(margins) == "input changed"
    served = _served(client, page)
    assert served["margins"]["stale_reason"] == "input changed"
    assert served["deskew"]["status"] == Status.QUEUED

    # Deskew finishes: Select Content's input fingerprint differs from what it ran on.
    held_queue.run_next()
    states = _states(page)
    content = states["select_content"]
    assert scantailor.stage_result_is_current(states["deskew"])
    assert scantailor.stage_input_fingerprint(content) == states["deskew"].output_sha256
    assert content.applied_input_sha256 != scantailor.stage_input_fingerprint(content)
    assert content.status == Status.QUEUED  # queued by Deskew's success

    held_queue.drain()
    states = _states(page)
    for stage in scantailor.stage_order():
        assert states[stage].status == Status.SUCCESS, (stage, states[stage].last_error)
        assert scantailor.stage_result_is_current(states[stage]), stage
    margins = states["margins"]
    assert margins.parameters["margin"] == 12
    assert margins.applied_input_sha256 == states["dewarping"].output_sha256
    assert margins.output_sha256 != before["margins"].output_sha256
    _no_failures()


@pytest.mark.django_db(transaction=True)
def test_settings_edited_back_to_the_applied_values_are_current_without_a_job(
    held_queue: _HeldQueue,
) -> None:
    client, job = _create_scan_job("fingerprint-edit-back")
    page = _page(client, str(job.id))
    _save(client, page, "deskew", {})
    held_queue.drain()
    margins = _states(page)["margins"]
    applied = scantailor.stage_settings("margins", margins.applied_parameters)
    result_sha = margins.output_sha256

    changed = _save(client, page, "margins", {**applied, "margin": 30})
    assert changed["job"] is not None
    assert changed["stage"]["status"] == Status.QUEUED
    assert changed["stage"]["settings_applied"] is False
    jobs = _stage_jobs()

    # What the editor sends for the settings of the existing result.
    back = _save(client, page, "margins", applied)
    assert back["job"] is None
    assert back["waiting_for"] is None
    assert back["stage"]["current"] is True
    assert _stage_jobs() == jobs
    margins = _states(page)["margins"]
    assert scantailor.stage_result_is_current(margins)
    assert margins.status == Status.SUCCESS
    # The recorded result parameters are back too (sizes, transform op).
    assert margins.parameters == margins.applied_parameters

    # The job queued for the intermediate settings is superseded, not run.
    held_queue.drain()
    margins = _states(page)["margins"]
    assert margins.output_sha256 == result_sha
    assert scantailor.stage_result_is_current(margins)
    _no_failures()


@pytest.mark.django_db(transaction=True)
def test_queued_job_whose_predecessor_changed_waits_instead_of_failing(
    held_queue: _HeldQueue,
) -> None:
    client, job = _create_scan_job("fingerprint-park")
    page = _page(client, str(job.id))
    _save(client, page, "deskew", {})
    held_queue.drain()

    _save(client, page, "margins", {"margins_mode": "manual", "margin": 8})  # queued
    _save(client, page, "select_content", {"content_mode": "disabled"})  # queued first
    held_queue.calls.reverse()  # the Margins job runs before Select Content's
    held_queue.run_next()
    margins = _states(page)["margins"]
    assert margins.status == Status.SUCCESS  # its old result, now stale
    assert not scantailor.stage_result_is_current(margins)
    margins_job = ProcessingJob.objects.get(job_id=margins.worker_job_id)
    assert margins_job.state == ProcessingJob.JobState.SUCCESS
    assert margins_job.details["superseded"] is True
    assert margins_job.details["waiting_for"] == "select_content"

    held_queue.drain()
    states = _states(page)
    assert all(scantailor.stage_result_is_current(state) for state in states.values())
    assert states["margins"].parameters["margin"] == 8
    _no_failures()


@pytest.mark.django_db(transaction=True)
def test_batch_apply_follows_the_same_rules_per_page() -> None:
    client, job = _create_scan_job("fingerprint-batch")
    page = _page(client, str(job.id))
    response = client.post(
        f"/scan/jobs/{job.id}/stages/margins/batch/",
        data=json.dumps({"parameters": {"margin": 5}, "preserve_exceptions": False}),
        content_type="application/json",
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["applied_count"] == 1
    assert payload["applied"][0]["waiting_for"] == "deskew"
    assert payload["jobs"] == []
    assert _stage_jobs() == 0

    _save(client, page, "deskew", {})
    margins = _states(page)["margins"]
    assert scantailor.stage_result_is_current(margins)
    assert margins.parameters["margin"] == 5
    _no_failures()


@pytest.mark.django_db(transaction=True)
def test_replacement_scan_makes_every_stage_stale_and_unused() -> None:
    client, job = _create_scan_job("fingerprint-rescan")
    page = _page(client, str(job.id))
    _save(client, page, "deskew", {})
    assert scan_pipeline._active_output_state(page) is not None

    replaced = _put_scan_page(client, page.id, _text_heavy_bytes(width=400, height=560))
    assert replaced.status_code == 201, replaced.content
    states = _states(page)
    for stage in scantailor.stage_order():
        assert states[stage].status == Status.SUCCESS
        assert not scantailor.stage_result_is_current(states[stage]), stage
    assert scantailor.stage_stale_reason(states["deskew"]) == "input changed"
    assert scantailor.stage_waiting_for(states["output"]) == "deskew"
    # Finishing never picks up a stale result.
    assert scan_pipeline._active_output_state(page) is None
    _no_failures()


def test_no_stored_invalidated_status_remains() -> None:
    assert "invalidated" not in ScanTailorStageState.StageStatus.values
    field_names = {field.name for field in ScanTailorStageState._meta.get_fields()}
    assert "invalidated_reason" not in field_names
    assert {"applied_input_sha256", "applied_parameters", "applied_revision"} <= field_names
    assert not hasattr(scantailor, "invalidate_downstream_states")


def test_settings_comparison_ignores_worker_results() -> None:
    applied = {
        "content_mode": "auto",
        "content_box": {"x1": 1, "y1": 2, "x2": 3, "y2": 4},
        "content_found": True,
        "transform": {"op": "crop"},
        "output_width": 10,
    }
    state = ScanTailorStageState(
        stage="select_content",
        revision=3,
        applied_revision=2,
        applied_parameters=applied,
        parameters={"content_mode": "auto", "content_box": {"x1": 1, "y1": 2, "x2": 3, "y2": 4}},
    )
    assert scantailor.stage_settings_applied(state)
    state.parameters = {"content_mode": "disabled"}
    assert not scantailor.stage_settings_applied(state)
    # Detected output zones are a result, a manual list is a setting.
    assert scantailor.stage_settings("output", {"zone_mode": "auto", "zones": [1]}) == {
        "zone_mode": "auto"
    }
    assert scantailor.stage_settings("output", {"zone_mode": "manual", "zones": [1]}) == {
        "zone_mode": "manual",
        "zones": [1],
    }
