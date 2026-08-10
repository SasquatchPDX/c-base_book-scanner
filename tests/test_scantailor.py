from __future__ import annotations

import io
import json
from collections.abc import AsyncGenerator, Iterable
from typing import cast

import pytest
from asgiref.sync import async_to_sync
from django.http import StreamingHttpResponse
from django.test import Client, override_settings
from PIL import Image, ImageDraw

from ebook_server.core import scantailor, views
from ebook_server.core.models import ProcessingJob, ScanPage, ScanTailorStageState
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
        payload=b"failure-content",
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
