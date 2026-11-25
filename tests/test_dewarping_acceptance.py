"""#171 acceptance checks for the cylindrical Dewarping stage (#195 step 6a).

1. Straightness before/after on mild and strong synthetic curl (``cylinder_page``),
   measured by ``scripts/measure_dewarping.py`` (OCR: tests/integration/test_dewarping_ocr.py).
2. Manual geometry and parameters survive replay, batch apply and upstream edits; off
   (bypass) keeps the pixels; estimation or model failures leave the page unchanged.
3. Bounded worker memory when rendering a full-resolution (24 MP) page.
"""

from __future__ import annotations

import importlib.util
import io
import json
import tracemalloc
from pathlib import Path
from types import ModuleType
from typing import Any

import cv2
import numpy as np
import pytest
from PIL import Image

from ebook_server.core import curvature, cylindrical, scan_transform, scantailor
from ebook_server.core.models import ScanPage, ScanTailorStageState
from tests.curvature_fixtures import CALIBRATION_ROWS, cylinder_page

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "measure_dewarping.py"


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("measure_dewarping", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _stage(image: Image.Image) -> scantailor.StageInput:
    return scantailor.StageInput(
        image=image, full_size=image.size, render_full=lambda: image.copy(), dpi=(300, 300)
    )


def _run(parameters: dict[str, Any], image: Image.Image) -> scantailor.StageResult:
    state = ScanTailorStageState(stage="dewarping", parameters=parameters)
    return scantailor._run_dewarping(state, _stage(image))


# --- 1. straightness ---------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["auto", "manual"])
@pytest.mark.parametrize(("name", "lift"), [("mild", 140.0), ("strong", 340.0)])
def test_curl_is_straightened(name: str, lift: float, mode: str, tmp_path: Path) -> None:
    row = _script().measure_case(name, lift, mode, None, tmp_path)
    assert row["dewarp_applied"] is True and row["op"] == "cylindrical", row
    assert row["level_rms_before_px"] > 3.0, row  # the fixture really is curled
    assert row["level_rms_after_px"] < 0.5, row
    assert row["level_rms_after_px"] < 0.1 * row["level_rms_before_px"], row
    assert row["level_ptp_after_px"] < 1.5, row
    assert row["bow_rms_after_px"] < 0.5, row


@pytest.mark.parametrize("mode", ["auto", "manual"])
def test_flat_page_is_not_bent(mode: str, tmp_path: Path) -> None:
    row = _script().measure_case("flat", 0.0, mode, None, tmp_path)
    assert row["level_rms_after_px"] < 0.3, row
    assert row["level_ptp_after_px"] < 1.5, row


def test_straightness_measure_sees_the_curl() -> None:
    measure = _script().straightness
    flat = measure(cylinder_page(0.0, calibration=True), len(CALIBRATION_ROWS))
    curled = measure(cylinder_page(340.0, calibration=True), len(CALIBRATION_ROWS))
    assert flat["level_rms"] < 0.05 and flat["level_ptp"] < 0.5
    assert curled["level_ptp"] > 40


# --- 2. persistence, bypass and fallback -----------------------------------------------


def test_off_mode_keeps_the_pixels_and_drops_the_curves() -> None:
    page = cylinder_page(340.0)
    curves = _script()._known_curves(340.0)
    for mode in ("off", "bypass"):
        result = _run({"dewarp_mode": mode, **curves}, page)
        assert result.parameters["dewarp_mode"] == "off"
        assert result.parameters["dewarp_applied"] is False
        assert result.parameters["transform"] == {"op": "identity"}
        assert "top_curve" not in result.parameters
        assert result.image.tobytes() == page.tobytes()


def test_manual_model_replays_to_identical_pixels() -> None:
    page = cylinder_page(140.0)
    parameters = {
        "dewarp_mode": "manual",
        "depth_perception": 2.5,
        **_script()._known_curves(140.0),
    }
    first = _run(parameters, page)
    second = _run(dict(first.parameters), page)  # replay from what was recorded
    assert first.parameters["transform"] == second.parameters["transform"]
    assert first.parameters["top_curve"] == parameters["top_curve"]
    assert first.parameters["depth_perception"] == 2.5
    assert first.image.tobytes() == second.image.tobytes()


@pytest.mark.parametrize(
    ("top", "bottom", "message"),
    [
        ([[0, 0], [1e12, 5]], [[0, 300], [1e12, 290]], "too far outside"),
        ([[0, 1e300], [200, 0]], [[0, 300], [200, 300]], "too far outside"),
        ([[x * 0.02, 10.0] for x in range(10000)], [[0, 290], [200, 290]], "more than 64"),
    ],
    ids=["far-x", "far-y", "too-many-points"],
)
def test_unbounded_manual_geometry_is_refused_quickly(
    top: list[list[float]], bottom: list[list[float]], message: str
) -> None:
    """Sampling such a spline took minutes (a runaway worker) before it was bounded."""
    page = Image.new("L", (200, 300), 235)
    result = _run({"dewarp_mode": "manual", "top_curve": top, "bottom_curve": bottom}, page)
    params = result.parameters
    assert params["dewarp_mode"] == "manual"
    assert params["dewarp_applied"] is False
    assert params["transform"] == {"op": "identity"}
    assert message in params["dewarp_warning"]
    assert params["top_curve"] == [[float(x), float(y)] for x, y in top]  # kept for the editor
    assert result.image.tobytes() == page.tobytes()


def test_failed_estimation_leaves_the_page_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    page = cylinder_page(340.0)

    def no_lines(_image: Image.Image) -> list[list[list[float]]]:
        raise ValueError("no reliable text baselines found")

    monkeypatch.setattr(curvature, "detect_curves", no_lines)
    result = _run({"dewarp_mode": "auto"}, page)
    assert result.parameters["dewarp_applied"] is False
    assert result.parameters["transform"] == {"op": "identity"}
    assert "No text lines" in result.parameters["dewarp_warning"]
    assert result.image.tobytes() == page.tobytes()


def test_a_model_that_cannot_be_rendered_leaves_the_page_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = cylinder_page(140.0)

    def broken(*_args: Any, **_kwargs: Any) -> np.ndarray:
        raise cylindrical.DewarpError("empty model domain")

    monkeypatch.setattr(cylindrical, "dewarp_image", broken)
    result = _run({"dewarp_mode": "manual", **_script()._known_curves(140.0)}, page)
    assert result.parameters["dewarp_applied"] is False
    assert result.parameters["transform"] == {"op": "identity"}
    assert "could not be applied" in result.parameters["dewarp_warning"]
    assert result.image.tobytes() == page.tobytes()


@pytest.mark.django_db
def test_manual_curves_survive_worker_replay_batch_apply_and_upstream_edits() -> None:
    from tests.test_scan_jobs import _create_scan_job, _upload_scan_page
    from tests.test_scantailor import _batch_stage_url, _stage_url

    client, job = _create_scan_job("scan-user-dewarp-acceptance")
    for sequence in (1, 2):
        payload = io.BytesIO()
        cylinder_page(140.0).save(payload, format="PNG")
        response = _upload_scan_page(
            client,
            str(job.id),
            sequence=sequence,
            side="single",
            payload=payload.getvalue(),
            filename=f"p{sequence}.png",
        )
        assert response.status_code == 201
    manual_page, auto_page = ScanPage.objects.filter(scan_job=job).order_by(
        "logical_sequence_number"
    )

    def post(url: str, body: dict[str, Any]) -> None:
        response = client.post(url, data=json.dumps(body), content_type="application/json")
        assert response.status_code == 202, response.content

    def dewarping(page: ScanPage) -> ScanTailorStageState:
        state = ScanTailorStageState.objects.get(scan_page=page, stage="dewarping")
        assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error
        return state

    # Curves in the dewarping input's (full-resolution) coordinates, inside the page.
    url = _stage_url(str(job.id), str(manual_page.id), "dewarping")
    post(url, {"parameters": {"dewarp_mode": "auto"}})
    assert dewarping(manual_page).parameters["output_width"] >= 240
    top = [[10.0, 40.0], [60.0, 55.0], [120.0, 60.0], [180.0, 55.0], [230.0, 40.0]]
    bottom = [[10.0, 300.0], [60.0, 308.0], [120.0, 310.0], [180.0, 308.0], [230.0, 300.0]]
    manual: dict[str, Any] = {"dewarp_mode": "manual", "top_curve": top, "bottom_curve": bottom}
    manual["depth_perception"] = 1.5

    post(url, {"parameters": manual})
    first = dewarping(manual_page)
    post(url, {"parameters": manual})
    replay = dewarping(manual_page)
    assert replay.revision > first.revision
    assert replay.output_sha256 == first.output_sha256  # identical pixels on replay
    assert replay.parameters["transform"] == first.parameters["transform"]
    assert replay.parameters["transform"]["op"] == "cylindrical"

    # Apply auto to all pages, preserving manual exceptions: the manual page is skipped.
    post(_batch_stage_url(str(job.id), "dewarping"), {"parameters": {"dewarp_mode": "auto"}})
    kept = dewarping(manual_page)
    assert kept.revision == replay.revision
    assert kept.parameters["top_curve"] == top
    assert dewarping(auto_page).parameters["dewarp_mode"] == "auto"

    # An upstream edit invalidates the stage; re-running it keeps the manual model.
    post(
        _stage_url(str(job.id), str(manual_page.id), "deskew"), {"parameters": {"manual_angle": 0}}
    )
    post(url, {})
    rerun = dewarping(manual_page)
    assert rerun.parameters["dewarp_mode"] == "manual"
    assert rerun.parameters["top_curve"] == top
    assert rerun.parameters["bottom_curve"] == bottom
    assert rerun.parameters["depth_perception"] == 1.5
    assert rerun.parameters["transform"]["op"] == "cylindrical"


# --- 3. full-resolution memory ---------------------------------------------------------


def test_full_resolution_render_builds_maps_in_bounded_strips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 24 MP page: no full-size coordinate maps (2 x 92 MiB before #171) are built."""
    op = _script().full_resolution_op()
    dewarper, domain, canvas = scan_transform._cylindrical_model(op)
    page = np.full((canvas[1], canvas[0]), 255, dtype=np.uint8)
    page[::100] = 0
    shapes: list[tuple[int, ...]] = []
    original = cv2.remap

    def recording(image: Any, map_x: Any, map_y: Any, *args: Any, **kwargs: Any) -> Any:
        shapes.append(map_x.shape)
        return original(image, map_x, map_y, *args, **kwargs)

    monkeypatch.setattr(cv2, "remap", recording)
    tracemalloc.start()
    try:
        output = cylindrical.dewarp_image(page, canvas, dewarper, domain, 255)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert output.shape == page.shape
    assert all(rows <= cylindrical.MAP_STRIP_ROWS for rows, _ in shapes)
    assert sum(rows for rows, _ in shapes) == canvas[1]
    # Output plus a few strips of scratch; the unbounded version peaked above 200 MiB.
    assert peak < output.nbytes + 16 * 2**20, peak / 2**20


def _single_remap_reference(
    image: np.ndarray,
    out_size: tuple[int, int],
    dewarper: cylindrical.CylindricalSurfaceDewarper,
    domain: tuple[float, float, float, float],
    background: int | tuple[int, ...],
) -> np.ndarray:
    """``dewarp_image`` as it was before the strips: full-size maps, one remap."""
    out_w, out_h = out_size
    left, top, right, bottom = domain
    model_y = (np.arange(out_h, dtype=np.float64) + 0.5 - top) / (bottom - top)
    map_x = np.empty((out_h, out_w), dtype=np.float32)
    map_y = np.empty((out_h, out_w), dtype=np.float32)
    for dst_x in range(out_w):
        g = dewarper.map_generatrix((dst_x + 0.5 - left) / (right - left))
        a, b, c = g.pln2img
        t = (model_y * a + b) / (model_y * c + 1.0)
        map_x[:, dst_x] = g.p1[0] + (g.p2[0] - g.p1[0]) * t - 0.5
        map_y[:, dst_x] = g.p1[1] + (g.p2[1] - g.p1[1]) * t - 0.5
    border = background if isinstance(background, tuple) else (background,) * 4
    return np.asarray(
        cv2.remap(
            image,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=border,
        )
    )


@pytest.mark.parametrize("shape", [(601, 401), (1200, 800, 3)])
def test_strips_give_the_same_pixels_as_one_remap(shape: tuple[int, ...]) -> None:
    op = _script().full_resolution_op((shape[1], shape[0]))
    dewarper, domain, canvas = scan_transform._cylindrical_model(op)
    image = np.random.default_rng(171).integers(0, 256, shape, dtype=np.uint8)
    background = 255 if len(shape) == 2 else (255, 255, 255)
    strips = cylindrical.dewarp_image(image, canvas, dewarper, domain, background)
    reference = _single_remap_reference(image, canvas, dewarper, domain, background)
    assert np.array_equal(strips, reference)
