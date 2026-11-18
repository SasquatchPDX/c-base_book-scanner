"""Working images made once per page version, and stages that never decode the scan (#195)."""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from PIL import Image, ImageDraw

from ebook_server.core import scan_images, scantailor
from ebook_server.core.models import ScanPage, ScanPageImage, ScanTailorStageState
from ebook_server.core.storage_registry import filesystem_backend_for_location
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page


@pytest.mark.parametrize(
    ("size", "dpi", "expected"),
    [
        ((2480, 3508), (300, 300), 0.5),  # A4 at 300 dpi -> 150 dpi
        ((4960, 7016), (600, 600), 0.25),  # A4 at 600 dpi -> 150 dpi
        ((6000, 9000), (100, 100), 2400 / 9000),  # capped by the maximum edge
        ((1200, 1600), (300, 300), 1.0),  # never below the minimum edge, never upscaled
        ((2000, 2800), (300, 300), 1600 / 2800),
    ],
)
def test_work_scale(size: tuple[int, int], dpi: tuple[int, int], expected: float) -> None:
    assert scan_images.work_scale(size, dpi) == pytest.approx(expected)


def _scan_bytes(width: int, height: int) -> bytes:
    image = Image.new("L", (width, height), 236)
    draw = ImageDraw.Draw(image)
    for y in range(300, height - 300, 70):
        draw.rectangle((250, y, width - 250, y + 22), fill=25)
    image = image.rotate(2.0, expand=False, fillcolor=236)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(300, 300))
    return buffer.getvalue()


def _run(client: Any, job_id: str, page_id: str, stage: str, parameters: dict[str, Any]) -> None:
    response = client.post(
        f"/scan/jobs/{job_id}/pages/{page_id}/stages/{stage}/",
        data=json.dumps({"parameters": parameters}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content
    state = ScanTailorStageState.objects.get(scan_page_id=page_id, stage=stage)
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error


@pytest.mark.django_db(transaction=True)
def test_stages_work_on_the_working_image_and_output_renders_the_original_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job = _create_scan_job("scan-images-pipeline")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_scan_bytes(2000, 2800),
        filename="page.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1)
    version = page.active_version()
    assert version is not None

    # Generated once, at upload, by the post-commit job (inline without a queue).
    page_image = ScanPageImage.objects.get(version=version)
    assert (page_image.full_width, page_image.full_height) == (2000, 2800)
    assert (page_image.work_width, page_image.work_height) == (1143, 1600)
    storage = scantailor._scan_storage()
    with Image.open(storage.resolve_location(page_image.thumbnail_storage_identifier)) as thumb:
        assert max(thumb.size) <= scan_images.THUMBNAIL_EDGE

    original = Path(
        filesystem_backend_for_location(version.storage_location).resolve_location(
            version.storage_identifier
        )
    )
    loaded: list[Path] = []
    real_load = scantailor._load_image

    def recording_load(path: Path) -> Image.Image:
        loaded.append(Path(path))
        return real_load(path)

    monkeypatch.setattr(scantailor, "_load_image", recording_load)

    for stage, parameters in (
        ("deskew", {}),
        ("select_content", {}),
        ("dewarping", {"dewarp_mode": "bypass"}),
        ("margins", {"margin": 40}),
    ):
        _run(client, str(job.id), str(page.id), stage, parameters)
    assert original not in loaded, "stages 1-4 must not decode the full-resolution scan"

    states = {state.stage: state for state in ScanTailorStageState.objects.filter(scan_page=page)}
    deskew = states["deskew"]
    # Parameters are in full-resolution coordinates; artifacts are working-resolution.
    assert (deskew.parameters["input_width"], deskew.parameters["input_height"]) == (2000, 2800)
    assert deskew.parameters["transform"]["op"] == "rotate"
    assert abs(deskew.parameters["angle"]) > 0.5
    with Image.open(storage.resolve_location(deskew.output_storage_identifier)) as work:
        assert max(work.size) < 1800
    assert deskew.preview_storage_identifier.endswith(".jpg")
    margins = states["margins"]
    expected_full = (margins.parameters["output_width"], margins.parameters["output_height"])
    assert expected_full[0] == states["select_content"].parameters["output_width"] + 80

    _run(client, str(job.id), str(page.id), "output", {"output_mode": "otsu", "zone_mode": "off"})
    assert loaded.count(original) == 1, "Output renders from the original exactly once"
    output = ScanTailorStageState.objects.get(scan_page=page, stage="output")
    with Image.open(storage.resolve_location(output.output_storage_identifier)) as rendered:
        assert rendered.size == expected_full
        assert rendered.info.get("dpi") is not None
        assert round(rendered.info["dpi"][0]) == 300
    with Image.open(storage.resolve_location(output.preview_storage_identifier)) as preview:
        assert max(preview.size) < max(expected_full)


@pytest.mark.django_db(transaction=True)
def test_missing_working_image_is_rebuilt_and_decode_errors_fail_the_stage() -> None:
    client, job = _create_scan_job("scan-images-rebuild")
    payload = _scan_bytes(900, 1200)
    _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=payload, filename="p.png"
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1)
    version = page.active_version()
    assert version is not None
    ScanPageImage.objects.filter(version=version).delete()
    _run(client, str(job.id), str(page.id), "deskew", {})
    assert ScanPageImage.objects.filter(version=version).exists()

    # The stored scan is damaged later and the working image is gone: a clear error.
    ScanPageImage.objects.filter(version=version).delete()
    original = filesystem_backend_for_location(version.storage_location).resolve_location(
        version.storage_identifier
    )
    Path(original).write_bytes(payload[: len(payload) // 3])
    response = client.post(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/",
        data=json.dumps({"parameters": {"manual_angle": 1}}),
        content_type="application/json",
    )
    assert response.status_code == 202
    state = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    assert state.status == ScanTailorStageState.StageStatus.FAILED
    assert "cannot read image" in state.last_error
