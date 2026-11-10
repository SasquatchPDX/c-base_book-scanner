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

from ebook_server.core import scantailor, views
from ebook_server.core.models import ProcessingJob, ScanPage, ScanTailorStageState
from ebook_server.core.storage_registry import backend_for_location
from ebook_server.storage import LocalFilesystemStorage
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page


def _stage_url(job_id: str, page_id: str, stage: str) -> str:
    return f"/scan/jobs/{job_id}/pages/{page_id}/stages/{stage}/"


def _batch_stage_url(job_id: str, stage: str) -> str:
    return f"/scan/jobs/{job_id}/stages/{stage}/batch/"


def _stages_url(job_id: str, page_id: str) -> str:
    return f"/scan/jobs/{job_id}/pages/{page_id}/stages/"


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
        ("page_split", {"split_mode": "manual", "split_x": 200}),
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
        _stage_url(str(job_a.id), str(page_a.id), "page_split"),
        data=json.dumps({"parameters": {"pass": "a"}}),
        content_type="application/json",
    )
    response_b = client_b.post(
        _stage_url(str(job_b.id), str(page_b.id), "page_split"),
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
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/page_split/preview/?input=true",
    )
    assert input_response.status_code == 200
    input_stream = cast(StreamingHttpResponse, input_response)
    source = Image.open(
        io.BytesIO(b"".join(cast(Iterable[bytes], input_stream.streaming_content))),
    )
    with source:
        assert source.size == (1200, 1600)

    processed = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "single"}}),
        content_type="application/json",
    )
    assert processed.status_code == 202
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error
    assert state.parameters["input_width"] == 1200
    assert state.parameters["input_height"] == 1600
    assert state.parameters["output_width"] == 1200
    assert state.parameters["output_height"] == 1600

    predecessor_preview = client.get(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/preview/?input=true",
    )
    assert predecessor_preview.status_code == 200
    preview_stream = cast(StreamingHttpResponse, predecessor_preview)
    preview = Image.open(
        io.BytesIO(b"".join(cast(Iterable[bytes], preview_stream.streaming_content))),
    )
    with preview:
        assert preview.size == (720, 960)


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
    page_split_state = next(item for item in initial["stages"] if item["stage"] == "page_split")
    expected_revision = page_split_state["revision"]

    first = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"expected_revision": expected_revision, "parameters": {"pass": "first"}}),
        content_type="application/json",
    )
    assert first.status_code == 202

    second = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
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
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"pass": "changed"}}),
        content_type="application/json",
    )
    assert update.status_code == 202

    latest = client.get(_stages_url(str(job.id), str(page.id))).json()["stages"]
    status_by_stage = {entry["stage"]: entry["status"] for entry in latest}
    for stage_name in ("deskew", "select_content", "margins", "dewarping", "output"):
        assert status_by_stage[stage_name] == ScanTailorStageState.StageStatus.INVALIDATED


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

    stage_url = _stage_url(str(job.id), str(page.id), "page_split")
    first_run = client.post(
        stage_url,
        data=json.dumps({"parameters": {"mode": "first"}}),
        content_type="application/json",
    )
    assert first_run.status_code == 202

    stage = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")
    assert stage.output_storage_identifier
    previous_output = stage.output_storage_identifier
    previous_size = stage.output_size_bytes
    previous_hash = stage.output_sha256

    stage.input_stage_state = None
    stage.input_scan_page_version = None
    stage.input_storage_identifier = "missing://artifact"
    stage.status = ScanTailorStageState.StageStatus.QUEUED
    stage.parameters = {"mode": "forced-fail"}
    stage.revision += 1
    stage.last_error = ""
    stage.save(
        update_fields=[
            "input_stage_state",
            "input_scan_page_version",
            "input_storage_identifier",
            "status",
            "parameters",
            "revision",
            "last_error",
            "updated_at",
        ],
    )

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
def test_page_split_auto_detects_facing_gutter() -> None:
    client, job = _create_scan_job("scan-user-auto-split")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="left",
        payload=_facing_page_bytes(),
        filename="facing-left.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="left")

    response = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "auto"}}),
        content_type="application/json",
    )
    assert response.status_code == 202

    stage = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")
    assert stage.output_storage_identifier
    assert stage.parameters["resolved_split_mode"] in {"auto", "heuristic-left"}
    assert stage.parameters["resolved_split_side"] in {"left", "left-heuristic"}
    preview_width, preview_height = _stage_preview_size(
        client,
        str(job.id),
        str(page.id),
        "page_split",
    )
    assert preview_width < 240
    assert preview_height == 280


@pytest.mark.django_db(transaction=True)
def test_page_split_manual_x_override() -> None:
    client, job = _create_scan_job("scan-user-manual-split")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="right",
        payload=_facing_page_bytes(split=230),
        filename="facing-right.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="right")

    response = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "manual", "split_x": 140}}),
        content_type="application/json",
    )
    assert response.status_code == 202

    preview_width, _ = _stage_preview_size(client, str(job.id), str(page.id), "page_split")
    assert 270 <= preview_width <= 290

    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")
    assert state.parameters["resolved_split_x"] == 140
    assert state.parameters["resolved_split_side"] == "right"


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

    split_payload = {"parameters": {"split_mode": "single"}}
    split_response = client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps(split_payload),
        content_type="application/json",
    )
    assert split_response.status_code == 202

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
def test_dewarping_manual_points_are_recorded_and_reproducible() -> None:
    client, job = _create_scan_job("scan-user-dewarp-manual")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_trapezoid_bytes(),
        filename="dewarp.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    manual_payload = {
        "parameters": {
            "dewarp_mode": "manual",
            "dewarp_points": [
                [18, 14],
                [392, 28],
                [412, 286],
                [10, 270],
            ],
        },
    }

    first = client.post(
        _stage_url(str(job.id), str(page.id), "dewarping"),
        data=json.dumps(manual_payload),
        content_type="application/json",
    )
    assert first.status_code == 202
    first_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="dewarping")
    first_hash = first_state.output_sha256
    assert first_state.parameters["dewarp_mode"] == "manual"
    assert first_state.parameters["dewarp_source_points"]

    second = client.post(
        _stage_url(str(job.id), str(page.id), "dewarping"),
        data=json.dumps(manual_payload),
        content_type="application/json",
    )
    assert second.status_code == 202
    second_state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="dewarping")
    assert second_state.output_sha256 == first_hash


@pytest.mark.django_db(transaction=True)
def test_dewarping_bypass_keeps_source_as_output() -> None:
    client, job = _create_scan_job("scan-user-dewarp-bypass")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"plain-dewarp",
        filename="dewarp-bypass.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    response = client.post(
        _stage_url(str(job.id), str(page.id), "dewarping"),
        data=json.dumps({"parameters": {"dewarp_mode": "bypass"}}),
        content_type="application/json",
    )
    assert response.status_code == 202

    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="dewarping")
    assert state.parameters["dewarp_mode"] == "bypass"


@pytest.mark.django_db(transaction=True)
def test_dewarping_without_library_falls_back_safely(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scantailor, "cv2", None)
    monkeypatch.setattr(scantailor, "np", None)
    client, job = _create_scan_job("scan-user-dewarp-fallback")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_trapezoid_bytes(),
        filename="fallback-dewarp.png",
        content_type="image/png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    response = client.post(
        _stage_url(str(job.id), str(page.id), "dewarping"),
        data=json.dumps({}),
        content_type="application/json",
    )
    assert response.status_code == 202

    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="dewarping")
    assert state.parameters["dewarp_mode"] in {
        "missing_library_fallback",
        "auto_fallback",
        "processing_error_fallback",
        "fallback_copy",
    }
    assert state.output_storage_identifier


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
def test_auto_deskew_straightens_skewed_text(skew: float, tmp_path: Path) -> None:
    page = _text_page().rotate(skew, expand=True, fillcolor=235)
    source = _save(page, tmp_path / "in.png")
    output = tmp_path / "out.png"
    result = scantailor._run_deskew(_state(), source, output, tmp_path / "prev.png")

    # applied rotation is the correction (opposite sign of the skew)
    assert result["angle"] == pytest.approx(-skew, abs=0.6)
    with Image.open(output) as straightened:
        residual = scantailor._estimate_deskew_angle(straightened.convert("L"), {})
    assert abs(residual) <= 0.5


def test_manual_angle_matches_auto_angle_semantics(tmp_path: Path) -> None:
    page = _text_page().rotate(3.0, expand=True, fillcolor=235)
    source = _save(page, tmp_path / "in.png")
    auto = scantailor._run_deskew(_state(), source, tmp_path / "a.png", tmp_path / "ap.png")
    manual = scantailor._run_deskew(
        _state({"manual_angle": auto["angle"]}),
        source,
        tmp_path / "m.png",
        tmp_path / "mp.png",
    )
    assert manual["deskew_mode"] == "manual"
    with Image.open(tmp_path / "a.png") as left, Image.open(tmp_path / "m.png") as right:
        assert left.size == right.size
        assert np.array_equal(np.asarray(left), np.asarray(right))


def test_deskew_prefers_zero_on_blank_and_dim_pages() -> None:
    assert scantailor._estimate_deskew_angle(Image.new("L", (800, 1000), 235), {}) == 0.0
    speck = Image.new("L", (800, 1000), 235)
    ImageDraw.Draw(speck).rectangle((400, 500, 404, 504), fill=20)
    assert scantailor._estimate_deskew_angle(speck, {}) == 0.0
    dim = _text_page(paper=70, ink=10)
    assert scantailor._estimate_deskew_angle(dim, {}) == 0.0


def test_deskew_corners_use_paper_colour_not_red(tmp_path: Path) -> None:
    page = _text_page().convert("RGB")
    source = _save(page, tmp_path / "in.png")
    output = tmp_path / "out.png"
    scantailor._run_deskew(_state({"manual_angle": 4.0}), source, output, tmp_path / "p.png")
    with Image.open(output) as rotated:
        rgb = rotated.convert("RGB")
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


def test_exif_rotated_page_is_not_split_as_spread(tmp_path: Path) -> None:
    portrait = _text_page(800, 1100)
    sideways = portrait.rotate(90, expand=True, fillcolor=235)  # stored landscape
    exif = Image.Exif()
    exif[0x0112] = 8  # orientation 8 undoes a 90 degree CCW rotation
    source = _save(sideways, tmp_path / "s.jpg", exif=exif)
    output = tmp_path / "out.png"
    state = ScanTailorStageState(parameters={})
    state.scan_page = ScanPage(side=ScanPage.Side.SINGLE)
    result = scantailor._run_page_split(state, source, output, tmp_path / "p.png")
    assert result["resolved_split_mode"] == "single"
    with Image.open(output) as out:
        assert out.size == (800, 1100)


def _spread(
    width: int = 1500,
    height: int = 1000,
    *,
    gutter_x: int,
    shadow: bool = True,
    paper: int = 225,
) -> Image.Image:
    rng = np.random.default_rng(3)
    array = np.full((height, width), paper, dtype=np.float32)
    for y in range(int(height * 0.1), int(height * 0.9), 26):
        x = int(width * 0.06)
        while x < width * 0.94:
            word = int(rng.integers(20, 60))
            if abs((x + word / 2) - gutter_x) > width * 0.02:
                array[y : y + 8, x : x + word] = paper * 0.15
            x += word + 12
    if shadow:
        columns = np.arange(width)
        ridge = np.clip(1 - np.abs(columns - gutter_x) / (width * 0.03), 0, 1)
        array *= (1 - 0.55 * ridge)[None, :]
    array += rng.normal(0, 3, array.shape)
    return Image.fromarray(np.clip(array, 0, 255).astype(np.uint8))


@pytest.mark.parametrize("gutter_ratio", [0.5, 0.44, 0.56])
def test_auto_split_finds_dark_gutter(gutter_ratio: float) -> None:
    spread = _spread(gutter_x=int(1500 * gutter_ratio))
    found = scantailor._find_gutter(spread)
    assert found is not None
    assert abs(found.x - 1500 * gutter_ratio) <= 1500 * 0.015
    assert found.confidence >= scantailor.MIN_SPLIT_CONFIDENCE


def test_auto_split_works_on_dim_exposure() -> None:
    dim = _spread(gutter_x=700, paper=60)
    found = scantailor._find_gutter(dim)
    assert found is not None
    assert abs(found.x - 700) <= 1500 * 0.015


def test_auto_split_finds_bright_gap_between_text_blocks() -> None:
    payload = _facing_page_bytes(width=900, height=600, split=430, gutter=24)
    with Image.open(io.BytesIO(payload)) as image:
        found = scantailor._find_gutter(image.copy())
    assert found is not None
    assert abs(found.x - 430) <= 900 * 0.015


def test_auto_split_rejects_featureless_spread() -> None:
    blank = Image.new("L", (1500, 1000), 230)
    assert scantailor._auto_split_position(blank) is None
    noise_pixels = np.random.default_rng(5).integers(200, 240, (1000, 1500), dtype=np.uint8)
    assert scantailor._auto_split_position(Image.fromarray(noise_pixels)) is None


def test_auto_split_does_not_pick_brightest_paper() -> None:
    # A bright paper patch off-centre must not beat the real (shadow) gutter.
    spread = _spread(gutter_x=760)
    array = np.asarray(spread).copy()
    array[:, 600:640] = 255
    found = scantailor._find_gutter(Image.fromarray(array))
    assert found is not None
    assert abs(found.x - 760) <= 1500 * 0.02


def test_content_box_ignores_dust_speck_and_scales_with_dim_images() -> None:
    page = _text_page(900, 1200, paper=235)
    ImageDraw.Draw(page).rectangle((860, 1170, 863, 1173), fill=15)
    left, top, right, bottom = scantailor._detect_content_box(page)
    assert left > 60
    assert right < 880
    assert top > 80
    assert bottom < 1100

    dim = _text_page(900, 1200, paper=90, ink=12)
    left, top, right, bottom = scantailor._detect_content_box(dim)
    assert (right - left) < 900 * 0.9
    assert (bottom - top) < 1200 * 0.9


def test_content_box_blank_page_is_full_frame() -> None:
    blank = Image.new("L", (600, 800), 220)
    ImageDraw.Draw(blank).rectangle((10, 10, 12, 12), fill=190)
    assert scantailor._detect_content_box(blank) == (0, 0, 600, 800)


def test_record_image_dimensions_overwrites_stale_values(tmp_path: Path) -> None:
    source = _save(Image.new("L", (120, 80), 255), tmp_path / "s.png")
    output = _save(Image.new("L", (60, 40), 255), tmp_path / "o.png")
    stale = {"input_width": 999, "input_height": 999, "output_width": 1, "output_height": 1}
    recorded = scantailor._record_image_dimensions(source, output, stale)
    assert (recorded["input_width"], recorded["input_height"]) == (120, 80)
    assert (recorded["output_width"], recorded["output_height"]) == (60, 40)


def test_stage_preserves_source_dpi_and_defaults_to_300(tmp_path: Path) -> None:
    source = _save(_text_page(400, 500), tmp_path / "d.png", dpi=(200, 200))
    state = _state({"manual_angle": 1.0})
    scantailor._run_deskew(state, source, tmp_path / "o.png", tmp_path / "p.png")
    with Image.open(tmp_path / "o.png") as out:
        assert out.info["dpi"][0] == pytest.approx(200, abs=1)
    nodpi = _save(_text_page(400, 500), tmp_path / "n.png")
    scantailor._run_deskew(state, nodpi, tmp_path / "o2.png", tmp_path / "p2.png")
    with Image.open(tmp_path / "o2.png") as out2:
        assert out2.info["dpi"][0] == pytest.approx(300, abs=1)


def test_unreadable_image_raises_clear_error(tmp_path: Path) -> None:
    good = io.BytesIO()
    _text_page(400, 500).save(good, format="PNG")
    truncated = tmp_path / "t.png"
    truncated.write_bytes(good.getvalue()[: len(good.getvalue()) // 2])
    junk = tmp_path / "j.png"
    junk.write_bytes(b"not an image")
    for path in (truncated, junk):
        with pytest.raises(scantailor.ImageLoadError, match="cannot read image"):
            scantailor._run_deskew(_state(), path, tmp_path / "o.png", tmp_path / "p.png")


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
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "single"}}),
        content_type="application/json",
    )
    assert response.status_code == 202
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.output_storage_identifier.endswith(".png")
    assert state.preview_storage_identifier.endswith(".png")
    preview = client.get(_stage_url(str(job.id), str(page.id), "page_split") + "preview/")
    assert preview["Content-Type"] == "image/png"


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
    version = page.active_version()
    assert version is not None
    backend = backend_for_location(version.storage_location)
    assert isinstance(backend, LocalFilesystemStorage)
    stored = backend.resolve_location(version.storage_identifier)
    Path(stored).write_bytes(good[: len(good) // 2])
    client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "single"}}),
        content_type="application/json",
    )
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")
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
    original = scantailor._STAGE_RUNNERS["page_split"]["auto_split"]

    def racing_runner(
        state: ScanTailorStageState,
        source: Path,
        output: Path,
        preview: Path,
    ) -> dict[str, object]:
        # a user saves new parameters (revision bump, re-queued) while the job runs
        ScanTailorStageState.objects.filter(id=state.id).update(
            parameters={"split_mode": "manual", "split_x": 77},
            revision=state.revision + 1,
            status=ScanTailorStageState.StageStatus.QUEUED,
        )
        return original(state, source, output, preview)

    monkeypatch.setitem(scantailor._STAGE_RUNNERS["page_split"], "auto_split", racing_runner)
    client.post(
        _stage_url(str(job.id), str(page.id), "page_split"),
        data=json.dumps({"parameters": {"split_mode": "single"}}),
        content_type="application/json",
    )
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="page_split")
    assert state.parameters == {"split_mode": "manual", "split_x": 77}
    assert state.status == ScanTailorStageState.StageStatus.QUEUED
    assert not state.output_storage_identifier


def test_content_box_on_dark_surroundings_finds_text_not_frame() -> None:
    canvas = Image.new("L", (1000, 1300), 25)
    page = _text_page(800, 1100, paper=225)
    canvas.paste(page, (100, 100))
    left, top, right, bottom = scantailor._detect_content_box(canvas)
    assert left > 150
    assert right < 950
    assert top > 150
    assert bottom < 1250
