from __future__ import annotations

import io
import json
from collections.abc import AsyncGenerator, Iterable
from pathlib import Path
from typing import cast

import numpy as np
import pytest
from asgiref.sync import async_to_sync
from django.http import StreamingHttpResponse
from django.test import Client, override_settings
from PIL import Image, ImageDraw

from ebook_server.core import deskew, scantailor, views
from ebook_server.core.models import (
    ProcessingJob,
    ScanPage,
    ScanPageImage,
    ScanTailorStageState,
)
from ebook_server.core.storage_registry import backend_for_location
from ebook_server.storage import LocalFilesystemStorage
from tests.stage_helpers import run_stage
from tests.stage_helpers import stage_input as make_stage_input
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page


def _stage_url(job_id: str, page_id: str, stage: str) -> str:
    return f"/scan/jobs/{job_id}/pages/{page_id}/stages/{stage}/"


def _batch_stage_url(job_id: str, stage: str) -> str:
    return f"/scan/jobs/{job_id}/stages/{stage}/batch/"


def _stages_url(job_id: str, page_id: str) -> str:
    return f"/scan/jobs/{job_id}/pages/{page_id}/stages/"


def test_margin_results_do_not_become_fixed_canvas_requests() -> None:
    source = Image.new("RGB", (100, 100), "black")
    first = run_stage(
        scantailor._run_margins, ScanTailorStageState(parameters={"margin": 20}), source
    )
    assert first.parameters["resolved_canvas_width"] == 140
    assert first.parameters["resolved_canvas_height"] == 140
    assert first.full_size == (140, 140) == first.image.size
    assert "canvas_width" not in first.parameters and "canvas_height" not in first.parameters
    second = run_stage(
        scantailor._run_margins,
        ScanTailorStageState(parameters={**first.parameters, "margin": 0}),
        source,
    )
    assert second.parameters["transform"] == {"op": "identity"}
    assert second.image.size == (100, 100)
    assert second.image.tobytes() == source.tobytes()
    assert second.parameters["resolved_canvas_width"] == 100
    assert second.parameters["resolved_canvas_height"] == 100
    fixed = run_stage(
        scantailor._run_margins,
        ScanTailorStageState(parameters={"margin": 0, "canvas_width": 200}),
        source,
    )
    assert fixed.parameters["canvas_width"] == fixed.parameters["resolved_canvas_width"] == 200
    assert fixed.full_size == (200, 100)


def _facing_page_bytes(
    *,
    width: int = 420,
    height: int = 280,
    split: int = 210,
    gutter: int = 10,
) -> bytes:
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    left_content_end = max(8, split - gutter // 2)
    right_content_start = min(width - 8, split + gutter // 2)

    draw.rectangle((8, 24, left_content_end, height - 24), fill=0)
    draw.rectangle((right_content_start, 24, width - 8, height - 24), fill=0)
    draw.rectangle((8, 0, width - 8, 4), fill=40)
    draw.rectangle((8, height - 4, width - 8, height), fill=40)

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _deskew_fixture_bytes(
    *,
    width: int = 360,
    height: int = 280,
    angle: float = 3.0,
) -> bytes:
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    for y in range(20, height - 20, 20):
        draw.line((12, y, width - 12, y), fill=0, width=2)
    rotated = image.rotate(angle, expand=True, fillcolor=255)
    buffer = io.BytesIO()
    rotated.save(buffer, format="PNG")
    return buffer.getvalue()


def _text_heavy_bytes(
    *,
    width: int = 420,
    height: int = 560,
) -> bytes:
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    content_left = 64
    content_top = 72
    content_right = width - 64
    content_bottom = height - 72
    draw.rectangle((content_left, content_top, content_right, content_bottom), outline=0, width=5)
    for y in range(content_top + 16, content_bottom - 16, 20):
        draw.line((content_left + 8, y, content_right - 8, y), fill=0, width=2)
    for x in range(content_left + 16, content_right - 16, 40):
        draw.line((x, content_top + 8, x, content_bottom - 8), fill=0, width=1)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _trapezoid_bytes(
    *,
    width: int = 420,
    height: int = 300,
) -> bytes:
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    draw.polygon(
        [(18, 14), (width - 28, 28), (width - 20, height - 18), (10, height - 30)],
        outline=0,
        fill=0,
    )
    for y in range(22, height - 22, 24):
        draw.line((22, y, width - 24, y), fill=255, width=1)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _mixed_content_bytes(
    *,
    width: int = 420,
    height: int = 320,
) -> bytes:
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    draw.rectangle((30, 32, width - 30, height - 32), outline=0, width=4)
    for y in range(50, 250, 24):
        draw.line((40, y, width - 40, y), fill=0, width=1)
    draw.rectangle((42, 48, width // 2 - 6, 198), fill=120, outline=0, width=1)
    draw.line((42, 180, width // 2 - 6, 180), fill=255, width=2)
    for y in range(90, 170, 16):
        draw.line((width // 2 + 10, y, width - 40, y + 2), fill=0, width=2)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _stage_preview_size(
    client: Client,
    job_id: str,
    page_id: str,
    stage: str,
) -> tuple[int, int]:
    response = client.get(
        f"/scan/jobs/{job_id}/pages/{page_id}/stages/{stage}/preview/",
    )
    assert response.status_code == 200
    streaming_response = cast(StreamingHttpResponse, response)
    payload = b"".join(cast(Iterable[bytes], streaming_response.streaming_content))
    preview = Image.open(io.BytesIO(payload))
    with preview:
        return preview.size


def _first_async_stream_chunk(response: StreamingHttpResponse) -> bytes:
    async def consume() -> bytes:
        stream = cast(AsyncGenerator[bytes], response.streaming_content)
        try:
            return await anext(stream)
        finally:
            await stream.aclose()

    return async_to_sync(consume)()


@pytest.mark.parametrize(
    ("stage", "parameters"),
    [
        ("deskew", {"manual_angle": 1.25}),
    ],
)
def test_batch_exception_detection_includes_manual_early_stages(
    stage: str,
    parameters: dict[str, object],
) -> None:
    assert views._contains_stage_manual_override(stage, parameters)


@pytest.mark.django_db(transaction=True)
def test_two_users_can_edit_different_scan_jobs() -> None:
    client_a, job_a = _create_scan_job("scan-user-a")
    _upload_scan_page(
        client_a,
        str(job_a.id),
        sequence=1,
        side="single",
        payload=b"job-a",
        filename="a.png",
    )
    page_a = ScanPage.objects.get(scan_job=job_a, logical_sequence_number=1, side="single")

    client_b, job_b = _create_scan_job("scan-user-b")
    _upload_scan_page(
        client_b,
        str(job_b.id),
        sequence=1,
        side="single",
        payload=b"job-b",
        filename="b.png",
    )
    page_b = ScanPage.objects.get(scan_job=job_b, logical_sequence_number=1, side="single")

    response_a = client_a.post(
        _stage_url(str(job_a.id), str(page_a.id), "deskew"),
        data=json.dumps({"parameters": {"pass": "a"}}),
        content_type="application/json",
    )
    response_b = client_b.post(
        _stage_url(str(job_b.id), str(page_b.id), "deskew"),
        data=json.dumps({"parameters": {"pass": "b"}}),
        content_type="application/json",
    )

    assert response_a.status_code == 202
    assert response_b.status_code == 202
    assert response_a.json()["stage"]["scan_job_id"] == str(job_a.id)
    assert response_b.json()["stage"]["scan_job_id"] == str(job_b.id)


@pytest.mark.django_db(transaction=True)
@override_settings(
    SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS=0.1,
    SCANTAILOR_EVENT_STREAM_MAX_EVENTS=1,
)
def test_workspace_event_stream_sends_persistent_stage_state() -> None:
    client, job = _create_scan_job("scan-user-events")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_text_heavy_bytes(),
        filename="events.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    client.get(_stages_url(str(job.id), str(page.id)))

    response = client.get(f"/scan/jobs/{job.id}/workspace/events/")

    assert response.status_code == 200
    streaming_response = cast(StreamingHttpResponse, response)
    assert streaming_response["Content-Type"].startswith("text/event-stream")
    assert streaming_response["X-Accel-Buffering"] == "no"
    chunk = _first_async_stream_chunk(streaming_response).decode("utf-8")
    assert chunk.startswith("event: workspace\ndata: ")
    payload = json.loads(chunk.split("data: ", maxsplit=1)[1])
    assert payload["job_id"] == str(job.id)
    assert {state["stage"] for state in payload["stages"]} == set(scantailor.stage_order())


@pytest.mark.django_db(transaction=True)
def test_stage_input_preview_and_dimensions_support_geometry_editor() -> None:
    client, job = _create_scan_job("scan-user-input-preview")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_text_heavy_bytes(width=1200, height=1600),
        filename="geometry.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    client.get(_stages_url(str(job.id), str(page.id)))

    input_response = client.get(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/preview/?input=true",
    )
    assert input_response.status_code == 200
    input_stream = cast(StreamingHttpResponse, input_response)
    source = Image.open(
        io.BytesIO(b"".join(cast(Iterable[bytes], input_stream.streaming_content))),
    )
    with source:
        assert source.size == (1200, 1600)

    processed = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 0}}),
        content_type="application/json",
    )
    assert processed.status_code == 202
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error
    assert state.parameters["input_width"] == 1200
    assert state.parameters["input_height"] == 1600
    assert state.parameters["output_width"] == 1200
    assert state.parameters["output_height"] == 1600

    predecessor_preview = client.get(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/select_content/preview/?input=true",
    )
    assert predecessor_preview.status_code == 200
    preview_stream = cast(StreamingHttpResponse, predecessor_preview)
    preview = Image.open(
        io.BytesIO(b"".join(cast(Iterable[bytes], preview_stream.streaming_content))),
    )
    with preview:
        # Previews are the working image (#195): pages up to 1600 px are not reduced.
        assert preview.size == (1200, 1600)


@pytest.mark.django_db(transaction=True)
def test_concurrent_edit_conflict_is_reported() -> None:
    client, job = _create_scan_job("scan-user-conflict")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"conflict-content",
        filename="scan.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    initial = client.get(_stages_url(str(job.id), str(page.id))).json()
    deskew_state = next(item for item in initial["stages"] if item["stage"] == "deskew")
    expected_revision = deskew_state["revision"]

    first = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"expected_revision": expected_revision, "parameters": {"pass": "first"}}),
        content_type="application/json",
    )
    assert first.status_code == 202

    second = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"expected_revision": expected_revision, "parameters": {"pass": "second"}}),
        content_type="application/json",
    )
    assert second.status_code == 409
    payload = second.json()
    assert payload["error"] == "conflict"
    assert payload["current_revision"] == expected_revision + 1
    assert payload["expected_revision"] == expected_revision


@pytest.mark.django_db(transaction=True)
def test_upstream_change_invalidates_downstream_stages() -> None:
    client, job = _create_scan_job("scan-user-invalidation")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"invalidate-content",
        filename="scan.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    workspace = client.get(_stages_url(str(job.id), str(page.id))).json()
    assert len(workspace["stages"]) == len(list(scantailor.stage_order()))

    update = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"pass": "changed"}}),
        content_type="application/json",
    )
    assert update.status_code == 202

    latest = client.get(_stages_url(str(job.id), str(page.id))).json()["stages"]
    status_by_stage = {entry["stage"]: entry["status"] for entry in latest}
    by_stage = {entry["stage"]: entry for entry in latest}
    for stage_name in ("select_content", "dewarping", "margins", "output"):
        assert status_by_stage[stage_name] == ScanTailorStageState.StageStatus.INVALIDATED
        # Routine invalidation is not an error (#53).
        assert by_stage[stage_name]["last_error"] == ""
        assert by_stage[stage_name]["invalidated_reason"] == "deskew updated"

    # Re-running a stage clears its invalidation note.
    rerun = client.post(
        _stage_url(str(job.id), str(page.id), "select_content"),
        data=json.dumps({"parameters": {}}),
        content_type="application/json",
    )
    assert rerun.status_code == 202
    content = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="select_content")
    assert content.invalidated_reason == ""


@pytest.mark.django_db(transaction=True)
def test_failed_stage_that_is_invalidated_clears_stale_error() -> None:
    client, job = _create_scan_job("scan-user-invalidation-failed")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"invalidate-failed",
        filename="scan.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    client.get(_stages_url(str(job.id), str(page.id)))
    ScanTailorStageState.objects.filter(scan_page=page, stage="output").update(
        status=ScanTailorStageState.StageStatus.FAILED,
        last_error="cannot read image",
    )
    scantailor.invalidate_downstream_states(
        page, from_stage="dewarping", reason="dewarping updated"
    )
    output = ScanTailorStageState.objects.get(scan_page=page, stage="output")
    assert output.status == ScanTailorStageState.StageStatus.INVALIDATED
    assert output.last_error == ""
    assert output.invalidated_reason == "dewarping updated"


@pytest.mark.django_db(transaction=True)
def test_stage_worker_failure_preserves_previous_outputs() -> None:
    client, job = _create_scan_job("scan-user-failure")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_facing_page_bytes(),
        filename="scan.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    stage_url = _stage_url(str(job.id), str(page.id), "deskew")
    first_run = client.post(
        stage_url,
        data=json.dumps({"parameters": {"mode": "first"}}),
        content_type="application/json",
    )
    assert first_run.status_code == 202

    stage = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert stage.output_storage_identifier
    previous_output = stage.output_storage_identifier
    previous_size = stage.output_size_bytes
    previous_hash = stage.output_sha256

    _corrupt_page_source(page)
    stage.status = ScanTailorStageState.StageStatus.QUEUED
    stage.parameters = {"mode": "forced-fail"}
    stage.revision += 1
    stage.last_error = ""
    stage.save(update_fields=["status", "parameters", "revision", "last_error", "updated_at"])

    processing_job = scantailor.request_stage_job(state=stage)
    processing_job.refresh_from_db()
    assert processing_job.state == ProcessingJob.JobState.FAILED
    stage.refresh_from_db()
    assert stage.status == ScanTailorStageState.StageStatus.FAILED
    assert stage.output_storage_identifier == previous_output
    assert stage.output_size_bytes == previous_size
    assert stage.output_sha256 == previous_hash
    assert stage.last_error


@pytest.mark.django_db(transaction=True)
def test_deskew_reproducible_with_manual_angle() -> None:
    client, job = _create_scan_job("scan-user-deskew-manual")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_deskew_fixture_bytes(),
        filename="deskew.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    deskew_url = _stage_url(str(job.id), str(page.id), "deskew")
    first = client.post(
        deskew_url,
        data=json.dumps({"parameters": {"manual_angle": 2.25}}),
        content_type="application/json",
    )
    assert first.status_code == 202

    deskew_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    first_hash = deskew_state.output_sha256
    first_angle = deskew_state.parameters.get("angle")
    assert first_angle == 2.25

    second = client.post(
        deskew_url,
        data=json.dumps({"parameters": {"manual_angle": 2.25}}),
        content_type="application/json",
    )
    assert second.status_code == 202

    deskew_state.refresh_from_db()
    assert deskew_state.output_sha256 == first_hash
    assert deskew_state.parameters.get("angle") == 2.25


@pytest.mark.django_db(transaction=True)
def test_select_content_detects_text_area() -> None:
    client, job = _create_scan_job("scan-user-select-content-auto")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_text_heavy_bytes(),
        filename="text-heavy.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    response = client.post(
        _stage_url(str(job.id), str(page.id), "select_content"),
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 202

    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="select_content")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error
    assert state.parameters["content_mode"] == "auto"
    preview_width, preview_height = _stage_preview_size(
        client,
        str(job.id),
        str(page.id),
        "select_content",
    )
    assert 0 < preview_width < 420
    assert 0 < preview_height < 560
    assert state.parameters["content_area_width"] < 420
    assert state.parameters["content_area_height"] < 560


@pytest.mark.django_db(transaction=True)
def test_select_content_manual_box_is_preserved() -> None:
    client, job = _create_scan_job("scan-user-select-content-manual")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_text_heavy_bytes(),
        filename="text-heavy.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    manual_box = [70, 120, 280, 420]
    response = client.post(
        _stage_url(str(job.id), str(page.id), "select_content"),
        data=json.dumps({"parameters": {"content_box": manual_box}}),
        content_type="application/json",
    )
    assert response.status_code == 202

    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="select_content")
    assert state.parameters["content_mode"] == "manual"
    assert state.parameters["content_box"]["x1"] == manual_box[0]
    assert state.parameters["content_box"]["y1"] == manual_box[1]
    assert state.parameters["content_box"]["x2"] == manual_box[2]
    assert state.parameters["content_box"]["y2"] == manual_box[3]

    preview_width, preview_height = _stage_preview_size(
        client,
        str(job.id),
        str(page.id),
        "select_content",
    )
    assert preview_width == manual_box[2] - manual_box[0]
    assert preview_height == manual_box[3] - manual_box[1]


@pytest.mark.django_db(transaction=True)
def test_margin_pipeline_reproducible_with_same_parameters() -> None:
    client, job = _create_scan_job("scan-user-margins-repeat")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"margins-variant",
        filename="plain.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    payload = {"parameters": {"margin": 24, "align_x": "center", "align_y": "center"}}
    first = client.post(
        _stage_url(str(job.id), str(page.id), "margins"),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert first.status_code == 202
    first_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="margins")
    first_hash = first_state.output_sha256

    second = client.post(
        _stage_url(str(job.id), str(page.id), "margins"),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert second.status_code == 202
    second_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="margins")
    assert second_state.output_sha256 == first_hash


@pytest.mark.django_db(transaction=True)
def test_batch_stage_avoids_manual_exceptions_by_default() -> None:
    client, job = _create_scan_job("scan-user-batch-manual")
    page1_data = _text_heavy_bytes(width=360, height=240)
    page2_data = _text_heavy_bytes(width=340, height=240)
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=page1_data,
        filename="page1.png",
        content_type="image/png",
    )
    _upload_scan_page(
        client,
        str(job.id),
        sequence=2,
        side="single",
        payload=page2_data,
        filename="page2.png",
        content_type="image/png",
    )
    page1 = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    page2 = ScanPage.objects.get(scan_job=job, logical_sequence_number=2, side="single")

    manual_box = [20, 20, 180, 120]
    manual_response = client.post(
        _stage_url(str(job.id), str(page1.id), "select_content"),
        data=json.dumps({"parameters": {"content_box": manual_box}}),
        content_type="application/json",
    )
    assert manual_response.status_code == 202
    page1_state_before = ScanTailorStageState.objects.get(
        scan_job=job,
        scan_page=page1,
        stage="select_content",
    )
    assert page1_state_before.parameters["content_mode"] == "manual"

    batch_response = client.post(
        _batch_stage_url(str(job.id), "select_content"),
        data=json.dumps({"parameters": {"content_mode": "auto"}}),
        content_type="application/json",
    )
    assert batch_response.status_code == 202
    batch_payload = batch_response.json()
    assert batch_payload["applied_count"] == 1
    assert batch_payload["skipped_count"] == 1
    assert page1_state_before.id is not None
    assert str(page1.id) in batch_payload["skipped_page_ids"]

    page1_state_after = ScanTailorStageState.objects.get(
        scan_job=job,
        scan_page=page1,
        stage="select_content",
    )
    page2_state_after = ScanTailorStageState.objects.get(
        scan_job=job,
        scan_page=page2,
        stage="select_content",
    )
    assert page1_state_after.revision == page1_state_before.revision
    assert page1_state_after.parameters["content_mode"] == "manual"
    assert page2_state_after.parameters["content_mode"] in {"auto", "manual"}


@pytest.mark.django_db(transaction=True)
def test_content_change_invalidates_downstream_artifacts() -> None:
    client, job = _create_scan_job("scan-user-content-invalidation")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_text_heavy_bytes(),
        filename="text-heavy.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    client.post(
        _stage_url(str(job.id), str(page.id), "select_content"),
        data=json.dumps({}),
        content_type="application/json",
    )
    client.post(
        _stage_url(str(job.id), str(page.id), "margins"),
        data=json.dumps({"parameters": {"margin": 16}}),
        content_type="application/json",
    )
    client.post(
        _stage_url(str(job.id), str(page.id), "dewarping"),
        data=json.dumps({}),
        content_type="application/json",
    )
    client.post(
        _stage_url(str(job.id), str(page.id), "output"),
        data=json.dumps({}),
        content_type="application/json",
    )

    selected = ScanTailorStageState.objects.get(
        scan_job=job,
        scan_page=page,
        stage="select_content",
    )
    assert selected.status == ScanTailorStageState.StageStatus.SUCCESS

    update = client.post(
        _stage_url(str(job.id), str(page.id), "select_content"),
        data=json.dumps({"parameters": {"content_box": [40, 40, 280, 460]}}),
        content_type="application/json",
    )
    assert update.status_code == 202

    for stage in ("margins", "dewarping", "output"):
        state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage=stage)
        assert state.status == ScanTailorStageState.StageStatus.INVALIDATED


@pytest.mark.django_db(transaction=True)
def test_output_default_otsu_is_reproducible() -> None:
    client, job = _create_scan_job("scan-user-output-repro")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_mixed_content_bytes(),
        filename="mixed.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    first = client.post(
        _stage_url(str(job.id), str(page.id), "output"),
        data=json.dumps({"parameters": {"output_mode": "otsu", "keep_color": False}}),
        content_type="application/json",
    )
    assert first.status_code == 202
    first_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="output")
    first_hash = first_state.output_sha256
    assert first_state.parameters["output_mode"] == "otsu"
    assert first_state.parameters["zone_mode"] == "auto"
    assert first_state.parameters["output_width"] > 0
    assert first_state.parameters["output_height"] > 0

    second = client.post(
        _stage_url(str(job.id), str(page.id), "output"),
        data=json.dumps({"parameters": {"output_mode": "otsu", "keep_color": False}}),
        content_type="application/json",
    )
    assert second.status_code == 202
    second_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="output")
    assert second_state.output_sha256 == first_hash


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("output_mode", ["otsu", "sauvola", "wolf", "fox", "window", "grad"])
def test_each_documented_output_algorithm_produces_an_artifact(output_mode: str) -> None:
    client, job = _create_scan_job(f"scan-user-output-{output_mode}")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_mixed_content_bytes(),
        filename=f"{output_mode}.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    response = client.post(
        _stage_url(str(job.id), str(page.id), "output"),
        data=json.dumps({"parameters": {"output_mode": output_mode}}),
        content_type="application/json",
    )

    assert response.status_code == 202
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="output")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.parameters["output_mode"] == output_mode
    assert state.algorithm == f"binarization_{output_mode}"
    assert state.output_sha256


@pytest.mark.django_db(transaction=True)
def test_output_manual_zone_controls_are_preserved() -> None:
    client, job = _create_scan_job("scan-user-output-zones")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_mixed_content_bytes(),
        filename="mixed-zones.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    payload = {
        "parameters": {
            "zone_mode": "manual",
            "output_mode": "grad",
            "text_box": [32, 40, 220, 210],
            "picture_box": [230, 40, 392, 292],
            "text_algorithm": "sauvola",
            "picture_algorithm": "window",
            "k": 0.35,
        },
    }

    response = client.post(
        _stage_url(str(job.id), str(page.id), "output"),
        data=json.dumps(payload),
        content_type="application/json",
    )
    assert response.status_code == 202

    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="output")
    assert state.parameters["zone_mode"] == "manual"
    assert state.parameters["text_box"] == [32, 40, 220, 210]
    assert state.parameters["picture_box"] == [230, 40, 392, 292]
    assert state.parameters["text_algorithm"] == "sauvola"
    assert state.parameters["picture_algorithm"] == "window"
    assert state.parameters["output_mode"] == "grad"
    assert state.output_storage_identifier


@pytest.mark.django_db(transaction=True)
def test_output_batch_skips_manual_zone_overrides() -> None:
    client, job = _create_scan_job("scan-user-output-batch")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_mixed_content_bytes(),
        filename="page1.png",
        content_type="image/png",
    )
    _upload_scan_page(
        client,
        str(job.id),
        sequence=2,
        side="single",
        payload=_mixed_content_bytes(),
        filename="page2.png",
        content_type="image/png",
    )
    page1 = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    page2 = ScanPage.objects.get(scan_job=job, logical_sequence_number=2, side="single")

    manual = client.post(
        _stage_url(str(job.id), str(page1.id), "output"),
        data=json.dumps(
            {
                "parameters": {
                    "zone_mode": "manual",
                    "text_box": [20, 20, 90, 150],
                    "output_mode": "otsu",
                },
            },
        ),
        content_type="application/json",
    )
    assert manual.status_code == 202
    manual_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page1, stage="output")
    manual_revision = manual_state.revision

    batch_response = client.post(
        _batch_stage_url(str(job.id), "output"),
        data=json.dumps({"parameters": {"output_mode": "window"}}),
        content_type="application/json",
    )
    assert batch_response.status_code == 202
    batch_payload = batch_response.json()
    assert batch_payload["skipped_count"] == 1
    assert str(page1.id) in batch_payload["skipped_page_ids"]
    assert batch_payload["applied_count"] == 1

    manual_state.refresh_from_db()
    assert manual_state.revision == manual_revision
    assert manual_state.parameters["zone_mode"] == "manual"

    page2_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page2, stage="output")
    assert page2_state.parameters["output_mode"] == "window"


# ---------------------------------------------------------------------------
# Image-processing regression tests (real-camera robustness)
# ---------------------------------------------------------------------------


def _text_page(
    width: int = 900,
    height: int = 1200,
    *,
    paper: int = 235,
    ink: int = 30,
    seed: int = 7,
) -> Image.Image:
    """Synthetic text page: rows of word-like dashes on paper."""
    rng = np.random.default_rng(seed)
    image = Image.new("L", (width, height), paper)
    draw = ImageDraw.Draw(image)
    for y in range(int(height * 0.12), int(height * 0.88), 28):
        x = int(width * 0.12)
        while x < width * 0.85:
            word = int(rng.integers(20, 70))
            draw.rectangle((x, y, min(x + word, int(width * 0.88)), y + 9), fill=ink)
            x += word + 14
    return image


def _state(parameters: dict[str, object] | None = None) -> ScanTailorStageState:
    return ScanTailorStageState(parameters=parameters or {})


def _save(image: Image.Image, path: Path, **kwargs: object) -> Path:
    image.save(path, **kwargs)  # type: ignore[arg-type]
    return path


@pytest.mark.parametrize("skew", [3.0, -4.0])
def test_auto_deskew_straightens_skewed_text(skew: float) -> None:
    # Pillow turns counter-clockwise by ``skew``; the correction turns clockwise by
    # the same amount, and positive deskew angles are clockwise (#195 step 4).
    page = _text_page().rotate(skew, expand=True, fillcolor=235)
    result = run_stage(scantailor._run_deskew, _state(), page)

    assert result.parameters["angle"] == pytest.approx(skew, abs=0.15)
    assert result.parameters["deskew_mode"] == "auto"
    assert result.parameters["confidence_good"] is True
    assert result.parameters["transform"] == {"op": "rotate", "angle": -result.parameters["angle"]}
    residual = deskew.detect(np.asarray(result.image.convert("L")), (150, 150))
    assert abs(residual.detected_angle) <= 0.15


def test_manual_angle_matches_auto_angle_semantics() -> None:
    page = _text_page().rotate(3.0, expand=True, fillcolor=235)
    auto = run_stage(scantailor._run_deskew, _state(), page)
    manual = run_stage(
        scantailor._run_deskew, _state({"manual_angle": auto.parameters["angle"]}), page
    )
    assert manual.parameters["deskew_mode"] == "manual"
    assert manual.parameters["transform"] == auto.parameters["transform"]
    assert auto.image.size == manual.image.size == auto.full_size
    assert np.array_equal(np.asarray(auto.image), np.asarray(manual.image))


def test_deskew_prefers_zero_on_blank_and_speck_pages() -> None:
    dpi = (150.0, 150.0)
    assert deskew.detect(np.full((1000, 800), 235, np.uint8), dpi).angle == 0.0
    speck = Image.new("L", (800, 1000), 235)
    ImageDraw.Draw(speck).rectangle((400, 500, 404, 504), fill=20)
    found = deskew.detect(np.asarray(speck), dpi)
    assert found.angle == 0.0 and not found.applied
    # A dim but straight page is read like any other (Otsu adapts to the contrast).
    assert deskew.detect(np.asarray(_text_page(paper=70, ink=10)), dpi).angle == 0.0


def test_deskew_corners_use_paper_colour_not_red() -> None:
    page = _text_page().convert("RGB")
    result = run_stage(scantailor._run_deskew, _state({"manual_angle": 4.0}), page)
    rgb = result.image.convert("RGB")
    for corner in ((0, 0), (rgb.width - 1, 0), (0, rgb.height - 1)):
        red, green, blue = rgb.getpixel(corner)
        assert abs(red - green) < 8
        assert abs(green - blue) < 8
        assert red > 200


def test_loader_scales_16bit_grayscale(tmp_path: Path) -> None:
    gradient = np.tile(np.linspace(0, 65535, 400, dtype=np.uint16), (100, 1))
    path = tmp_path / "g16.png"
    Image.fromarray(gradient).save(path)
    loaded = scantailor._load_image(path)
    assert loaded.mode == "L"
    values = np.asarray(loaded)
    assert values[:, 0].max() <= 2
    assert 120 <= int(values[0, 200]) <= 135
    assert values[:, -1].min() >= 253


def test_loader_converts_cmyk_alpha_and_palette(tmp_path: Path) -> None:
    base = Image.new("RGB", (64, 48), (40, 90, 200))
    cmyk = _save(base.convert("CMYK"), tmp_path / "c.jpg")
    assert scantailor._load_image(cmyk).mode == "RGB"

    rgba = Image.new("RGBA", (32, 32), (0, 0, 0, 0))
    ImageDraw.Draw(rgba).rectangle((8, 8, 23, 23), fill=(0, 0, 0, 255))
    flattened = scantailor._load_image(_save(rgba, tmp_path / "a.png"))
    assert flattened.mode == "RGB"
    assert flattened.getpixel((1, 1)) == (255, 255, 255)
    assert flattened.getpixel((16, 16)) == (0, 0, 0)

    luma_alpha = Image.new("LA", (16, 16), (0, 0))
    flat_l = scantailor._load_image(_save(luma_alpha, tmp_path / "la.png"))
    assert flat_l.mode == "L"
    assert flat_l.getpixel((3, 3)) == 255

    palette = base.convert("P")
    assert scantailor._load_image(_save(palette, tmp_path / "p.png")).mode == "RGB"
    assert scantailor._load_image(_save(base.convert("1"), tmp_path / "b.png")).mode == "L"


def test_loader_applies_exif_orientation(tmp_path: Path) -> None:
    landscape = Image.new("RGB", (300, 200), (255, 255, 255))
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated: displays as portrait
    path = _save(landscape, tmp_path / "o.jpg", exif=exif)
    loaded = scantailor._load_image(path)
    assert loaded.size == (200, 300)


def test_zero_angle_deskew_records_identity_and_keeps_identical_pixels() -> None:
    page = _text_page(300, 400)
    stage_input = make_stage_input(page)
    result = scantailor._run_deskew(_state({"manual_angle": 0}), stage_input)
    assert result.parameters["transform"] == {"op": "identity"}
    assert result.full_size == page.size
    assert result.image.tobytes() == page.tobytes()


def test_exif_rotated_input_defines_the_stage_geometry(tmp_path: Path) -> None:
    landscape = _text_page(500, 300).convert("RGB")
    exif = Image.Exif()
    exif[0x0112] = 6  # stored rotated: displays as portrait
    path = _save(landscape, tmp_path / "o.jpg", exif=exif)
    stage_input = make_stage_input(path)
    assert stage_input.full_size == (300, 500)
    result = scantailor._run_deskew(_state({"manual_angle": 0}), stage_input)
    assert result.full_size == (300, 500)


def test_stage_preserves_source_dpi_and_defaults_to_300(tmp_path: Path) -> None:
    """The full-resolution Output file carries the page DPI (missing DPI means 300)."""
    for saved, expected in (({"dpi": (200, 200)}, 200), ({}, 300)):
        source = _save(_text_page(400, 500), tmp_path / f"d{expected}.png", **saved)
        stage_input = make_stage_input(source)
        assert stage_input.dpi[0] == expected
        result = scantailor._run_output(_state({"output_mode": "otsu"}), stage_input)
        output = tmp_path / f"o{expected}.png"
        scantailor._write_stage_artifacts(result, stage_input, output, tmp_path / "p.png")
        with Image.open(output) as out:
            assert out.info["dpi"][0] == pytest.approx(expected, abs=1)


def test_unreadable_image_raises_clear_error(tmp_path: Path) -> None:
    good = io.BytesIO()
    _text_page(400, 500).save(good, format="PNG")
    truncated = tmp_path / "t.png"
    truncated.write_bytes(good.getvalue()[: len(good.getvalue()) // 2])
    junk = tmp_path / "j.png"
    junk.write_bytes(b"not an image")
    for path in (truncated, junk):
        with pytest.raises(scantailor.ImageLoadError, match="cannot read image"):
            run_stage(scantailor._run_deskew, _state(), path)


@pytest.mark.parametrize("mode", ["otsu", "sauvola", "wolf", "fox", "window"])
def test_binarization_keeps_noisy_paper_white_and_ink_black(mode: str) -> None:
    rng = np.random.default_rng(11)
    gray = rng.normal(225, 7, (400, 600)).clip(0, 255)
    ink_mask = np.zeros((400, 600), dtype=bool)
    for y in range(40, 360, 40):
        ink_mask[y : y + 10, 50:550] = True
    gray[ink_mask] = rng.normal(35, 8, int(ink_mask.sum())).clip(0, 255)
    gray_u8 = gray.astype(np.uint8)
    result = scantailor._binarize_with_cv2(
        gray_u8,
        Image.fromarray(gray_u8),
        mode,
        window_size=35,
        k=0.33,
        r=128.0,
        invert=False,
    )
    binary = np.asarray(result) > 0
    assert (~binary[~ink_mask]).mean() < 0.02  # paper stays white
    assert (~binary[ink_mask]).mean() > 0.9  # ink stays black


# --- pipeline-level behaviour ------------------------------------------------


def _jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    _text_page(500, 700).convert("RGB").save(buffer, format="JPEG")
    return buffer.getvalue()


@pytest.mark.django_db(transaction=True)
def test_stage_outputs_are_named_for_the_png_they_contain() -> None:
    client, job = _create_scan_job("scan-user-output-ext")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_jpeg_bytes(),
        filename="scan.jpg",
        content_type="image/jpeg",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    response = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 0}}),
        content_type="application/json",
    )
    assert response.status_code == 202
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    # Stages 1-4: lossless working image plus a JPEG preview (#195 step 2).
    assert state.output_storage_identifier.endswith("/work.png")
    assert state.preview_storage_identifier.endswith("/preview.jpg")
    preview = client.get(_stage_url(str(job.id), str(page.id), "deskew") + "preview/")
    assert preview["Content-Type"] == "image/jpeg"


def _corrupt_page_source(page: ScanPage) -> None:
    """Make the page unreadable for the worker: no working image, truncated original."""
    version = page.active_version()
    assert version is not None
    ScanPageImage.objects.filter(version=version).delete()
    backend = backend_for_location(version.storage_location)
    assert isinstance(backend, LocalFilesystemStorage)
    stored = Path(backend.resolve_location(version.storage_identifier))
    stored.write_bytes(stored.read_bytes()[: max(1, stored.stat().st_size // 2)])


@pytest.mark.django_db(transaction=True)
def test_unreadable_input_fails_the_stage_with_message() -> None:
    client, job = _create_scan_job("scan-user-unreadable")
    good = _facing_page_bytes()
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=good,
        filename="scan.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    # Uploads are validated, so simulate later on-disk corruption of the stored original.
    _corrupt_page_source(page)
    client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 0}}),
        content_type="application/json",
    )
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert state.status == ScanTailorStageState.StageStatus.FAILED
    assert "cannot read image" in state.last_error
    assert not state.output_storage_identifier
    failed = ProcessingJob.objects.get(job_id=state.worker_job_id)
    assert failed.state == ProcessingJob.JobState.FAILED


@pytest.mark.django_db(transaction=True)
def test_edit_during_job_is_not_overwritten(monkeypatch: pytest.MonkeyPatch) -> None:
    client, job = _create_scan_job("scan-user-race")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_facing_page_bytes(),
        filename="scan.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    original = scantailor._STAGE_RUNNERS["deskew"]["skew_finder"]

    def racing_runner(
        state: ScanTailorStageState, stage_input: scantailor.StageInput
    ) -> scantailor.StageResult:
        # a user saves new parameters (revision bump, re-queued) while the job runs
        ScanTailorStageState.objects.filter(id=state.id).update(
            parameters={"manual_angle": 1.5},
            revision=state.revision + 1,
            status=ScanTailorStageState.StageStatus.QUEUED,
        )
        return original(state, stage_input)

    monkeypatch.setitem(scantailor._STAGE_RUNNERS["deskew"], "skew_finder", racing_runner)
    client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 0}}),
        content_type="application/json",
    )
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert state.parameters == {"manual_angle": 1.5}
    assert state.status == ScanTailorStageState.StageStatus.QUEUED
    assert not state.output_storage_identifier


def test_oversized_image_is_an_image_load_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _save(Image.new("L", (600, 800), 200), tmp_path / "in.png")
    # Pillow raises DecompressionBombError above 2x MAX_IMAGE_PIXELS.
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)
    with pytest.raises(scantailor.ImageLoadError):
        scantailor._load_image(source)


@pytest.mark.django_db(transaction=True)
def test_stage_run_records_current_algorithm_version() -> None:
    client, job = _create_scan_job("scan-user-dewarp-version")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_trapezoid_bytes(),
        filename="dewarp-version.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    url = _stage_url(str(job.id), str(page.id), "dewarping")
    points = [[18, 14], [392, 28], [412, 286], [10, 270]]
    payload = json.dumps({"parameters": {"dewarp_mode": "manual", "dewarp_points": points}})
    assert client.post(url, data=payload, content_type="application/json").status_code == 202
    # a row created by an older release still carries the old implementation version
    ScanTailorStageState.objects.filter(scan_page=page, stage="dewarping").update(
        algorithm_version="opencv-v1"
    )
    assert client.post(url, data=payload, content_type="application/json").status_code == 202
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="dewarping")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.algorithm_version == scantailor.DEFAULT_STAGE_ALGORITHM_VERSION["dewarping"]
    assert state.algorithm_version != "opencv-v1"


def _thumbnail_size(client: Client, page_id: str) -> tuple[tuple[int, int], str]:
    response = client.get(f"/scan/pages/{page_id}/image/")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    streaming = cast(StreamingHttpResponse, response)
    body = b"".join(cast(Iterable[bytes], streaming.streaming_content))
    with Image.open(io.BytesIO(body)) as thumb:
        return thumb.size, response["ETag"]


@pytest.mark.django_db(transaction=True)
def test_page_thumbnail_follows_latest_completed_stage() -> None:
    """#58: the overview thumbnail shows the latest completed stage, not the raw scan."""
    client, job = _create_scan_job("scan-user-thumbnail-stage")
    portrait = io.BytesIO()
    _text_page(300, 400).save(portrait, format="PNG")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=portrait.getvalue(),
        filename="portrait.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    raw_size, raw_etag = _thumbnail_size(client, str(page.id))
    assert raw_size == (300, 400)

    client.get(_stages_url(str(job.id), str(page.id)))
    response = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 45}}),
        content_type="application/json",
    )
    assert response.status_code == 202
    deskew = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    assert deskew.status == ScanTailorStageState.StageStatus.SUCCESS, deskew.last_error

    # Deskew turned the 3:4 page by 45 degrees (the manual maximum): the rotated page
    # fills a square canvas, so the thumbnail is square now.
    stage_size, stage_etag = _thumbnail_size(client, str(page.id))
    assert stage_size[0] == stage_size[1] > 300
    assert stage_etag != raw_etag
    revalidated = client.get(f"/scan/pages/{page.id}/image/", HTTP_IF_NONE_MATCH=stage_etag)
    assert revalidated.status_code == 304
    assert "max-age=0" in revalidated["Cache-Control"]

    # A stale (invalidated) stage is never shown; fall back to the raw page.
    ScanTailorStageState.objects.filter(pk=deskew.pk).update(
        status=ScanTailorStageState.StageStatus.INVALIDATED,
    )
    fallback_size, fallback_etag = _thumbnail_size(client, str(page.id))
    assert fallback_size == (300, 400)
    assert fallback_etag == raw_etag

    # Downstream success after an upstream that is not complete does not count either.
    ScanTailorStageState.objects.filter(scan_page=page, stage="select_content").update(
        status=ScanTailorStageState.StageStatus.SUCCESS,
        output_storage_identifier=deskew.output_storage_identifier,
        preview_storage_identifier=deskew.preview_storage_identifier,
        preview_storage_location=deskew.preview_storage_location,
        output_storage_location=deskew.output_storage_location,
    )
    ScanTailorStageState.objects.filter(pk=deskew.pk).update(
        status=ScanTailorStageState.StageStatus.QUEUED,
    )
    assert _thumbnail_size(client, str(page.id))[1] == raw_etag


@pytest.mark.django_db(transaction=True)
def test_noop_stage_still_persists_own_output_artifact_with_provenance() -> None:
    client, job = _create_scan_job("scan-user-noop-stage")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_text_heavy_bytes(),
        filename="noop.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    client.get(_stages_url(str(job.id), str(page.id)))
    response = client.post(
        _stage_url(str(job.id), str(page.id), "deskew"),
        data=json.dumps({"parameters": {"manual_angle": 0}}),
        content_type="application/json",
    )
    assert response.status_code == 202

    active = page.active_version()
    assert active is not None
    deskew = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    assert deskew.status == ScanTailorStageState.StageStatus.SUCCESS, deskew.last_error
    # The no-op stage has its own artifact (and preview) with full provenance, separate
    # from the raw page version it read (byte copying is covered by the passthrough tests).
    assert deskew.output_storage_identifier
    assert deskew.output_storage_identifier != active.storage_identifier
    assert deskew.preview_storage_identifier
    assert deskew.input_stage_state_id is None
    assert deskew.input_scan_page_version_id == active.id
    assert deskew.algorithm and deskew.algorithm_version
    assert deskew.output_sha256 and deskew.output_size_bytes
    assert deskew.parameters["angle"] == 0
    assert deskew.parameters["output_width"] == deskew.parameters["input_width"]


def _workspace_queries(client: Client, job_id: str) -> list[str]:
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        response = client.get(f"/scan/jobs/{job_id}/workspace/")
    assert response.status_code == 200
    return [query["sql"] for query in captured.captured_queries]


@pytest.mark.django_db(transaction=True)
def test_workspace_query_count_does_not_grow_with_page_count() -> None:
    """Regression for #51: the workspace issued several queries per page *and* stage."""
    client, job = _create_scan_job("scan-user-workspace-queries")
    for sequence in (1, 2):
        _upload_scan_page(
            client,
            str(job.id),
            sequence=sequence,
            side="single",
            payload=f"page-{sequence}".encode(),
            filename=f"p{sequence}.png",
        )
    _workspace_queries(client, str(job.id))  # first load creates the stage rows
    small = _workspace_queries(client, str(job.id))

    for sequence in range(3, 9):
        _upload_scan_page(
            client,
            str(job.id),
            sequence=sequence,
            side="single",
            payload=f"page-{sequence}".encode(),
            filename=f"p{sequence}.png",
        )
    first_large = _workspace_queries(client, str(job.id))
    large = _workspace_queries(client, str(job.id))

    assert len(large) == len(small)
    # Creating the missing rows costs a bounded number of extra queries, not O(pages).
    assert len(first_large) <= len(small) + 2 * len(scantailor.stage_order()) + 2
    # A steady-state load must not write anything.
    assert not [sql for sql in large if sql.lstrip().upper().startswith(("UPDATE", "INSERT"))]
    assert ScanTailorStageState.objects.filter(scan_job=job).count() == 8 * len(
        scantailor.stage_order()
    )


@pytest.mark.django_db(transaction=True)
def test_workspace_bulk_stage_creation_links_inputs_like_single_path() -> None:
    client, job = _create_scan_job("scan-user-workspace-links")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"links",
        filename="links.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    response = client.get(f"/scan/jobs/{job.id}/workspace/")
    assert response.status_code == 200
    stages = response.json()["pages"][0]["stages"]
    assert [stage["stage"] for stage in stages] == list(scantailor.stage_order())

    states = {state.stage: state for state in ScanTailorStageState.objects.filter(scan_page=page)}
    active = page.active_version()
    assert active is not None
    assert states["deskew"].input_stage_state_id is None
    assert states["deskew"].input_scan_page_version_id == active.id
    assert states["deskew"].input_storage_identifier == active.storage_identifier
    for stage in scantailor.stage_order()[1:]:
        predecessor = scantailor.previous_stage(stage)
        assert predecessor is not None
        assert states[stage].input_stage_state_id == states[predecessor].id
        assert states[stage].algorithm == scantailor.DEFAULT_STAGE_ALGORITHM[stage]


@pytest.mark.django_db(transaction=True)
def test_get_or_create_stage_state_skips_noop_save() -> None:
    client, job = _create_scan_job("scan-user-noop-save")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"noop",
        filename="noop.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    first = scantailor.get_or_create_stage_state(
        scan_job_id=str(job.id), page_id=str(page.id), stage="deskew"
    )
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as captured:
        again = scantailor.get_or_create_stage_state(
            scan_job_id=str(job.id), page_id=str(page.id), stage="deskew"
        )
    assert again.id == first.id
    assert not [
        query
        for query in captured.captured_queries
        if query["sql"].lstrip().upper().startswith("UPDATE")
    ]
