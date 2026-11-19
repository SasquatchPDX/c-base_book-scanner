"""Real output pixels and bounded layout detection, beyond successful job state (#172)."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import cv2
import numpy as np
import pytest
from PIL import Image

from ebook_server.core import output_zones, scantailor, views
from ebook_server.core.models import ScanPage, ScanTailorStageState
from tests.output_zone_fixtures import PICTURE_BOXES, illustrated_page
from tests.stage_helpers import run_stage
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page


def run_output(tmp_path: Path, parameters: dict[str, Any]) -> tuple[Image.Image, dict[str, Any]]:
    source = tmp_path / "source.png"
    if not source.exists():
        illustrated_page().save(source)
    before = source.read_bytes()
    result = run_stage(
        scantailor._run_output,
        cast(ScanTailorStageState, SimpleNamespace(parameters=parameters)),
        source,
    )
    assert source.read_bytes() == before
    assert result.full_resolution
    return result.image, result.parameters


def test_auto_finds_multiple_pictures_and_text_regions_and_preserves_detail(tmp_path: Path) -> None:
    image = illustrated_page()
    zones = output_zones.detect_zones(image)
    assert sum(z["kind"] == "picture" for z in zones) >= 2
    assert sum(z["kind"] == "text" for z in zones) >= 2
    output, result = run_output(tmp_path, {})
    assert result["output_applied"] == "zoned"
    assert not result["output_warning"]
    assert all(z["source"] == "auto" for z in result["resolved_zones"])
    for left, top, right, bottom in PICTURE_BOXES:
        # Require protection of the continuous-tone interior, not merely a box label.
        region = (left + 12, top + 12, right - 12, bottom - 12)
        assert np.array_equal(np.asarray(output.crop(region)), np.asarray(image.crop(region)))
    assert len(output.crop(PICTURE_BOXES[0]).getcolors(maxcolors=1_000_000) or []) > 100


@pytest.mark.parametrize("keep_color", [False, True])
def test_manual_picture_copy_keeps_original_rgb_pixels(tmp_path: Path, keep_color: bool) -> None:
    zones = [{"kind": "picture", "box": list(box)} for box in PICTURE_BOXES]
    output, result = run_output(
        tmp_path,
        {
            "zone_mode": "manual",
            "zones": zones,
            "picture_algorithm": "copy",
            "keep_color": keep_color,
        },
    )
    assert output.mode == "RGB"
    for box in PICTURE_BOXES:
        assert np.array_equal(
            np.asarray(output.crop(box)), np.asarray(illustrated_page().crop(box))
        )
    assert all(z["source"] == "manual" for z in result["resolved_zones"])


def test_legacy_rectangles_replay_as_list_and_keep_pixels(tmp_path: Path) -> None:
    output, parameters = run_output(
        tmp_path,
        {
            "zone_mode": "manual",
            "text_box": [60, 60, 470, 260],
            "picture_box": list(PICTURE_BOXES[0]),
            "picture_algorithm": "copy",
        },
    )
    first = np.asarray(output)
    replay, again = run_output(tmp_path, parameters)
    assert np.array_equal(first, np.asarray(replay))
    assert parameters["zones"] == again["zones"]
    assert parameters["text_box"] == [60, 60, 470, 260]


def test_keep_color_protects_unclassified_and_picture_pixels(tmp_path: Path) -> None:
    output, _ = run_output(tmp_path, {"zone_mode": "off", "keep_color": True})
    assert np.array_equal(np.asarray(output), np.asarray(illustrated_page()))
    output, result = run_output(
        tmp_path,
        {
            "zone_mode": "manual",
            "keep_color": True,
            "picture_algorithm": "otsu",
            "zones": [
                {"kind": "picture", "box": list(PICTURE_BOXES[0])},
                {"kind": "text", "box": [60, 60, 490, 260]},
            ],
        },
    )
    assert result["picture_algorithm"] == "copy"
    assert np.array_equal(
        np.asarray(output.crop(PICTURE_BOXES[0])),
        np.asarray(illustrated_page().crop(PICTURE_BOXES[0])),
    )


def test_auto_replay_is_deterministic_and_manual_edits_are_batch_exceptions(tmp_path: Path) -> None:
    first, parameters = run_output(tmp_path, {})
    second, repeated = run_output(tmp_path, parameters)
    assert np.array_equal(np.asarray(first), np.asarray(second))
    assert parameters["resolved_zones"] == repeated["resolved_zones"]
    assert not views._contains_stage_manual_override("output", repeated)
    authored = {**parameters, "zone_mode": "manual", "zones": parameters["resolved_zones"]}
    assert views._contains_stage_manual_override("output", authored)


def test_copy_bypass_and_failed_detection_never_destroy_source_pixels(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bypass, _ = run_output(tmp_path, {"output_mode": "copy"})
    assert np.array_equal(np.asarray(bypass), np.asarray(illustrated_page()))

    def fail(_source: Image.Image) -> Any:
        raise RuntimeError("bounded detector unavailable")

    monkeypatch.setattr(output_zones, "detect_zones", fail)
    output, result = run_output(tmp_path, {})
    assert result["output_applied"] == "copy_fallback"
    assert "unavailable" in result["output_warning"]
    assert np.array_equal(np.asarray(output), np.asarray(illustrated_page()))


def test_detection_and_threshold_scratch_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[int, ...]] = []
    original = scantailor._binarize_with_cv2

    def recording(gray: Any, *args: Any, **kwargs: Any) -> Image.Image:
        calls.append(gray.shape)
        return original(gray, *args, **kwargs)

    monkeypatch.setattr(scantailor, "_binarize_with_cv2", recording)
    large = illustrated_page().resize((2200, 3080))
    masks: list[tuple[int, ...]] = []
    components = cv2.connectedComponentsWithStats

    def bounded_components(mask: Any, *args: Any, **kwargs: Any) -> Any:
        masks.append(mask.shape)
        return components(mask, *args, **kwargs)

    monkeypatch.setattr(cv2, "connectedComponentsWithStats", bounded_components)
    output_zones.detect_zones(large)
    assert masks and all(max(shape) <= output_zones.MAX_DETECTION_EDGE for shape in masks)
    large.save(tmp_path / "source.png")
    _output, result = run_output(
        tmp_path, {"output_mode": "sauvola", "zone_mode": "off", "window_size": 501}
    )
    assert not result["output_warning"]
    assert len(calls) > 4
    assert all(max(shape) <= scantailor.OUTPUT_TILE_EDGE + 506 for shape in calls)
    with pytest.raises(ValueError, match="at most"):
        scantailor._output_zones({"zones": [{}] * 65}, (100, 100))


@pytest.mark.django_db(transaction=True)
def test_list_zone_edit_survives_batch_and_only_replaces_output() -> None:
    client, job = _create_scan_job("zone-list-editor")
    encoded = io.BytesIO()
    illustrated_page().save(encoded, format="PNG")
    for sequence in (1, 2):
        response = _upload_scan_page(
            client, str(job.pk), sequence, "single", encoded.getvalue(), filename=f"{sequence}.png"
        )
        assert response.status_code == 201
    pages = list(ScanPage.objects.filter(scan_job=job).order_by("logical_sequence_number"))
    zones = [{"kind": "picture", "box": list(box)} for box in PICTURE_BOXES]
    url = f"/scan/jobs/{job.pk}/pages/{pages[0].pk}/stages/output/"
    response = client.post(
        url,
        json.dumps({"parameters": {"zone_mode": "manual", "zones": zones}}),
        content_type="application/json",
    )
    assert response.status_code == 202
    manual = ScanTailorStageState.objects.get(scan_page=pages[0], stage="output")
    original = manual.output_sha256
    upstream = list(
        ScanTailorStageState.objects.filter(scan_page=pages[0])
        .exclude(stage="output")
        .values("id", "revision", "status")
    )
    response = client.post(
        f"/scan/jobs/{job.pk}/stages/output/batch/",
        json.dumps(
            {
                "parameters": {"zone_mode": "auto", "output_mode": "wolf"},
                "preserve_exceptions": True,
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 202
    assert response.json()["skipped_count"] == 1
    manual.refresh_from_db()
    assert manual.output_sha256 == original
    replay = client.post(
        url, json.dumps({"parameters": manual.parameters}), content_type="application/json"
    )
    assert replay.status_code == 202
    manual.refresh_from_db()
    assert manual.output_sha256 == original
    assert manual.algorithm_version == "opencv-zones-v3"
    assert (
        list(
            ScanTailorStageState.objects.filter(scan_page=pages[0])
            .exclude(stage="output")
            .values("id", "revision", "status")
        )
        == upstream
    )
