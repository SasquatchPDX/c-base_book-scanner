"""Actual remapped pixels, geometric improvement, replay, and memory bounds (#171)."""

import io
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from ebook_server.core import curvature, scantailor
from ebook_server.core.models import ScanPage, ScanTailorStageState
from ebook_server.core.storage_registry import backend_for_location
from tests.curvature_fixtures import curled_page, straightness
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page


@pytest.mark.parametrize("amplitude", [24, 64], ids=["mild-synthetic", "strong-synthetic"])
def test_synthetic_curvature_improves_pixel_straightness_and_replays(amplitude: int) -> None:
    text = curled_page(amplitude)
    curves = curvature.detect_curves(text)
    output, _ = curvature.render(text, curves)
    assert not np.array_equal(np.asarray(text), np.asarray(output)[: text.height])
    calibration = curled_page(amplitude, calibration=True)
    corrected, geometry = curvature.render(calibration, curves)
    before = straightness(calibration)
    after = straightness(corrected, padding=geometry["curvature_padding"])
    assert after < before * 0.3, (before, after)
    assert after < 1.5, after
    replay, repeated = curvature.render(text, curves)
    assert output.tobytes() == replay.tobytes()
    assert geometry == repeated
    print(f"Synthetic amplitude={amplitude}: baseline RMS {before:.3f}px -> {after:.3f}px")


def test_bypass_and_failed_estimation_keep_pixels(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    Image.new("RGB", (300, 500), (245, 240, 235)).save(source)
    for mode in ("auto", "bypass", "manual"):
        state = cast(
            ScanTailorStageState,
            SimpleNamespace(parameters={"dewarp_model": "curvature", "dewarp_mode": mode}),
        )
        metadata = scantailor._run_dewarping(
            state, source, tmp_path / "out.png", tmp_path / "preview.png"
        )
        assert metadata["dewarp_applied"] == "passthrough"
        assert metadata["algorithm"] == "copy"
        assert Image.open(source).tobytes() == Image.open(tmp_path / "out.png").tobytes()
        if mode != "bypass":
            assert metadata["dewarp_warning"]


def test_flat_text_page_needs_no_correction() -> None:
    source = curled_page(0)
    output, geometry = curvature.render(source, curvature.detect_curves(source))
    assert geometry["dewarp_correction"] == "none_needed"
    assert source.tobytes() == output.tobytes()


def test_full_resolution_remap_scratch_is_tiled(monkeypatch: pytest.MonkeyPatch) -> None:
    source = Image.new("L", (4000, 6000), 255)
    curves = [[[0, y + 100], [2000, y], [3999, y + 100]] for y in (600, 2400, 4800)]
    sizes: list[tuple[tuple[int, ...], tuple[int, ...]]] = []
    original = cv2.remap

    def recording(region: Any, map_x: Any, map_y: Any, *args: Any, **kwargs: Any) -> Any:
        sizes.append((region.shape, map_x.shape))
        return original(region, map_x, map_y, *args, **kwargs)

    monkeypatch.setattr(cv2, "remap", recording)
    output, metadata = curvature.render(source, curves)
    assert output.width == 4000 and output.height > 6000
    assert metadata["curvature_max_displacement"] <= curvature.MAX_DISPLACEMENT
    assert len(sizes) > 50
    assert all(max(mapping) <= curvature.TILE_EDGE for _region, mapping in sizes)
    assert all(
        region[0] <= curvature.TILE_EDGE + 2 * curvature.MAX_DISPLACEMENT + 5
        for region, _mapping in sizes
    )


def test_padding_keeps_all_four_input_corners() -> None:
    source = Image.new("RGB", (1000, 1400), "white")
    draw = ImageDraw.Draw(source)
    colours = [(255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 0, 255)]
    for box, colour in zip(
        ((0, 0, 12, 12), (987, 0, 999, 12), (0, 1387, 12, 1399), (987, 1387, 999, 1399)),
        colours,
        strict=True,
    ):
        draw.rectangle(box, fill=colour)
    curves = [[[0, y + 100], [500, y], [999, y + 100]] for y in (200, 650, 1100)]
    output, _metadata = curvature.render(source, curves, (255, 255, 255))
    pixels = np.asarray(output)
    assert all(bool((pixels == colour).all(axis=2).any()) for colour in colours)


@pytest.mark.parametrize("mode", ["L", "RGB"])
def test_full_resolution_stage_decodes_writes_and_bounds_preview(
    tmp_path: Path,
    mode: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = Image.new(mode, (4000, 6000), 255 if mode == "L" else (255, 255, 255))
    draw = ImageDraw.Draw(source)
    for y in range(100, 5900, 100):
        draw.line((50, y, 3950, y), fill=0, width=2)
    path = tmp_path / "full-resolution.png"
    source.save(path)
    del draw, source
    before = path.read_bytes()
    preview_sizes = []
    original_preview = scantailor._save_preview

    def record_preview(image: Image.Image, destination: Path) -> None:
        preview_sizes.append(image.size)
        original_preview(image, destination)

    monkeypatch.setattr(scantailor, "_save_preview", record_preview)
    curves = [[[0, y + 100], [2000, y], [3999, y + 100]] for y in (600, 2400, 4800)]
    state = cast(
        ScanTailorStageState,
        SimpleNamespace(parameters={"dewarp_mode": "manual", "dewarp_curves": curves}),
    )
    metadata = scantailor._run_curvature_dewarp(
        state, path, tmp_path / "out.png", tmp_path / "preview.png"
    )
    assert metadata["dewarp_applied"] == "curvature", metadata
    with Image.open(tmp_path / "out.png") as output:
        assert output.size == (4000, 6104)
    assert preview_sizes and all(max(size) <= 960 for size in preview_sizes)
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "curves",
    [
        [[[0, 10], [50, 20], [99, 10]], [[0, 40], [50, 10], [99, 40]]],
        [[[0, 10], [0, 10], [99, 10]], [[0, 40], [50, 40], [99, 40]]],
        [[[0, float("nan")], [50, 10], [99, 10]], [[0, 40], [50, 40], [99, 40]]],
    ],
)
def test_unsafe_manual_geometry_is_rejected(curves: Any) -> None:
    with pytest.raises(ValueError):
        curvature.normalize_curves(curves, (100, 100))


@pytest.mark.django_db(transaction=True)
def test_opt_in_worker_provenance_manual_replay_and_switch_back() -> None:
    client, job = _create_scan_job("curvature-worker")
    payload = io.BytesIO()
    curled_page(64).save(payload, format="PNG")
    uploaded = _upload_scan_page(
        client, str(job.pk), 1, "single", payload.getvalue(), filename="synthetic-curl.png"
    )
    assert uploaded.status_code == 201
    page = ScanPage.objects.get(scan_job=job)
    url = f"/scan/jobs/{job.pk}/pages/{page.pk}/stages/dewarping/"

    def run(parameters: dict[str, Any]) -> ScanTailorStageState:
        response = client.post(
            url, json.dumps({"parameters": parameters}), content_type="application/json"
        )
        assert response.status_code == 202
        state = ScanTailorStageState.objects.get(scan_page=page, stage="dewarping")
        assert state.status == "success", state.last_error
        return state

    default = run({})
    assert default.algorithm == "projective_dewarp"
    automatic = run({"dewarp_model": "curvature", "dewarp_mode": "auto"})
    assert automatic.algorithm == "baseline_curvature"
    assert automatic.algorithm_version == curvature.VERSION
    assert automatic.parameters["dewarp_applied"] == "curvature"
    curves = automatic.parameters["curvature_curves"]
    first_hash = automatic.output_sha256
    replay = run({"dewarp_model": "curvature", "dewarp_mode": "manual", "dewarp_curves": curves})
    assert replay.output_sha256 == first_hash
    assert replay.parameters["dewarp_curves"] == curves
    bypass = run({"dewarp_model": "curvature", "dewarp_mode": "bypass"})
    assert bypass.parameters["curvature_curves"] is None
    assert bypass.parameters["dewarp_applied"] == "passthrough"
    restored = run({"dewarp_model": "perspective", "dewarp_mode": "auto"})
    assert restored.algorithm == "projective_dewarp"
    assert restored.algorithm_version == "opencv-v3"
    assert restored.parameters["curvature_curves"] is None
    assert restored.output_storage_location is not None
    storage = backend_for_location(restored.output_storage_location)
    assert storage.exists(restored.output_storage_identifier)
