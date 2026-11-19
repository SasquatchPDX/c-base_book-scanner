"""Cylindrical-surface dewarping (#195 step 6a): the model, the raster dewarper, the stage."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from ebook_server.core import cylindrical, scan_transform, scantailor
from ebook_server.core.models import ScanTailorStageState

FIXTURE = Path(__file__).parent / "fixtures" / "dewarp_model_cases.json"

TOP = [(20.0, 60.0), (100.0, 80.0), (200.0, 92.0), (300.0, 80.0), (380.0, 60.0)]
BOTTOM = [(20.0, 540.0), (100.0, 548.0), (200.0, 552.0), (300.0, 548.0), (380.0, 540.0)]


def _curved_model(depth: float = 2.0) -> cylindrical.CylindricalSurfaceDewarper:
    top = cylindrical.XSpline(TOP).to_polyline()
    bottom = cylindrical.XSpline(BOTTOM).to_polyline()
    return cylindrical.CylindricalSurfaceDewarper(top, bottom, depth)


# --- XSpline ----------------------------------------------------------------------------


def test_xspline_ends_and_straight_lines() -> None:
    spline = cylindrical.XSpline(TOP)
    assert spline.point_at(0.0) == pytest.approx(TOP[0])
    assert spline.point_at(1.0) == pytest.approx(TOP[-1])
    straight = cylindrical.XSpline.editor_default((0.0, 10.0), (100.0, 10.0))
    assert straight.tensions == [0.0, 1.0, 1.0, 1.0, 0.0]
    for x, y in straight.to_polyline():
        assert y == pytest.approx(10.0)
        assert 0.0 <= x <= 100.0


def test_xspline_polyline_stays_within_a_fifth_of_a_pixel() -> None:
    spline = cylindrical.XSpline(TOP)
    polyline = np.array(spline.to_polyline())
    for t in np.linspace(0, 1, 97):
        x, y = spline.point_at(float(t))
        distances = []
        for (ax, ay), (bx, by) in zip(polyline[:-1], polyline[1:], strict=True):
            vx, vy = bx - ax, by - ay
            s = max(0.0, min(1.0, ((x - ax) * vx + (y - ay) * vy) / (vx * vx + vy * vy)))
            distances.append(math.hypot(x - (ax + s * vx), y - (ay + s * vy)))
        assert min(distances) <= 0.21


# --- the model --------------------------------------------------------------------------


def test_rectangular_model_is_the_identity() -> None:
    top, bottom = cylindrical.trivial_curves(200, 300)
    dewarper = cylindrical.CylindricalSurfaceDewarper(top, bottom, 2.0)
    domain = cylindrical.model_domain(top, bottom, dewarper, (0, 0, 200, 300))
    assert domain == pytest.approx((0, 0, 200, 300))
    image = (np.random.default_rng(1).random((300, 200)) * 255).astype(np.uint8)
    out = cylindrical.dewarp_image(image, (200, 300), dewarper, domain, 255)
    assert np.array_equal(out, image)


def test_mapping_round_trips_and_curves_become_straight() -> None:
    dewarper = _curved_model()
    for point in [(50.0, 100.0), (200.0, 300.0), (350.0, 500.0), (120.0, 90.0)]:
        crv = dewarper.map_to_dewarped(point)
        assert dewarper.map_to_warped(crv) == pytest.approx(point, abs=1e-6)
    # Every point of the top curve maps to crv y = 0, of the bottom curve to 1.
    for t in np.linspace(0.05, 0.95, 7):
        assert dewarper.map_to_dewarped(cylindrical.XSpline(TOP).point_at(float(t)))[1] == (
            pytest.approx(0.0, abs=2e-3)
        )
        assert dewarper.map_to_dewarped(cylindrical.XSpline(BOTTOM).point_at(float(t)))[1] == (
            pytest.approx(1.0, abs=2e-3)
        )


def test_depth_perception_scales_the_unrolled_length() -> None:
    # The curves sag by different amounts: upstream reads that as surface elevation.
    # Straight curves have no elevation, so depth perception does not matter there.
    flat = _curved_model(1.0)
    deep = _curved_model(3.0)
    assert deep.directrix_arc_length > flat.directrix_arc_length > 1.0
    straight = [(20.0, 60.0), (380.0, 100.0)], [(20.0, 540.0), (380.0, 500.0)]
    assert cylindrical.CylindricalSurfaceDewarper(*straight, 3.0).directrix_arc_length == (
        pytest.approx(1.0)
    )


def test_dewarping_straightens_lines_drawn_along_the_model() -> None:
    dewarper = _curved_model()
    size = (400, 600)
    image = Image.new("L", size, 235)
    draw = ImageDraw.Draw(image)
    rows = [0.1 * k for k in range(1, 10)]
    for crv_y in rows:
        points = [dewarper.map_to_warped((x, crv_y)) for x in np.linspace(0.02, 0.98, 120)]
        draw.line(points, fill=20, width=3)
    top = cylindrical.XSpline(TOP).to_polyline()
    bottom = cylindrical.XSpline(BOTTOM).to_polyline()
    domain = cylindrical.model_domain(top, bottom, dewarper, (0, 0, *size))
    out = cylindrical.dewarp_image(np.asarray(image), size, dewarper, domain, 235)
    left, top_y, right, bottom_y = domain
    for crv_y in rows:
        expected = top_y + crv_y * (bottom_y - top_y)
        for x in np.linspace(left + 30, right - 30, 9).astype(int):
            column = out[:, x]
            window = column[int(expected) - 8 : int(expected) + 9]
            darkest = int(expected) - 8 + int(np.argmin(window))
            assert abs(darkest - expected) <= 2.0, (crv_y, x, darkest, expected)


def test_model_validity_is_upstreams_convexity_check() -> None:
    assert cylindrical.model_is_valid(TOP, BOTTOM)
    # Crossed: the bottom curve runs right to left.
    assert not cylindrical.model_is_valid(TOP, list(reversed(BOTTOM)))
    # Degenerate: a curve that starts where it ends.
    assert not cylindrical.model_is_valid([(1.0, 1.0), (1.0, 1.0)], BOTTOM)


def test_shared_fixture_matches_the_python_model() -> None:
    """frontend/dewarp-view.ts draws the grid with a TypeScript port; both check this."""
    cases = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert len(cases) >= 3
    for case in cases:
        top = cylindrical.XSpline([tuple(p) for p in case["top"]]).to_polyline()
        bottom = cylindrical.XSpline([tuple(p) for p in case["bottom"]]).to_polyline()
        dewarper = cylindrical.CylindricalSurfaceDewarper(top, bottom, case["depth"])
        for crv, expected in zip(case["crv"], case["warped"], strict=True):
            assert dewarper.map_to_warped(tuple(crv)) == pytest.approx(expected, abs=1e-6)


# --- transform op -----------------------------------------------------------------------


def test_cylindrical_op_sizes_maps_and_renders_at_any_scale() -> None:
    top = cylindrical.XSpline(TOP).to_polyline()
    bottom = cylindrical.XSpline(BOTTOM).to_polyline()
    op = scan_transform.cylindrical_op(top, bottom, 2.0, (400, 600))
    assert scan_transform.output_size(op, (400, 600)) == (400, 600)
    full = Image.new("L", (400, 600), 235)
    ImageDraw.Draw(full).rectangle((100, 200, 300, 400), fill=20)
    out_full = scan_transform.apply(full, op, (1.0, 1.0))
    half = full.resize((200, 300))
    out_half = scan_transform.apply(half, op, (0.5, 0.5))
    assert out_full.size == (400, 600) and out_half.size == (200, 300)
    reduced = np.asarray(out_full.resize((200, 300)), dtype=np.float64)
    assert np.mean(np.abs(reduced - np.asarray(out_half, dtype=np.float64))) < 6
    mapped = scan_transform.map_point(op, (199.5, 299.5), (400, 600))
    assert 150 < mapped[0] < 250 and 250 < mapped[1] < 350


# --- the stage --------------------------------------------------------------------------


def _stage(image: Image.Image) -> scantailor.StageInput:
    return scantailor.StageInput(
        image=image, full_size=image.size, render_full=lambda: image.copy(), dpi=(150, 150)
    )


def _state(parameters: dict[str, object]) -> ScanTailorStageState:
    return ScanTailorStageState(stage="dewarping", parameters=parameters)


def test_stage_modes() -> None:
    page = Image.new("L", (400, 600), 235)
    off = scantailor._run_dewarping(_state({"dewarp_mode": "bypass"}), _stage(page))
    assert off.parameters["dewarp_mode"] == "off"
    assert off.parameters["transform"] == {"op": "identity"}

    manual = scantailor._run_dewarping(
        _state(
            {
                "dewarp_mode": "manual",
                "top_curve": [list(p) for p in TOP],
                "bottom_curve": [list(p) for p in BOTTOM],
                "depth_perception": 9,
            }
        ),
        _stage(page),
    )
    params = manual.parameters
    assert params["dewarp_mode"] == "manual" and params["dewarp_applied"] is True
    assert params["depth_perception"] == cylindrical.MAX_DEPTH_PERCEPTION
    assert params["transform"]["op"] == "cylindrical"
    assert params["top_curve"] == [list(p) for p in TOP]

    crossed = scantailor._run_dewarping(
        _state(
            {
                "dewarp_mode": "manual",
                "top_curve": [list(p) for p in TOP],
                "bottom_curve": [list(p) for p in reversed(BOTTOM)],
            }
        ),
        _stage(page),
    )
    assert crossed.parameters["transform"] == {"op": "identity"}
    assert "convex" in crossed.parameters["dewarp_warning"]

    blank = scantailor._run_dewarping(_state({}), _stage(page))
    assert blank.parameters["dewarp_mode"] == "auto"
    assert "No text lines" in blank.parameters["dewarp_warning"]
    # The trivial model is still recorded, so the editor can start from it.
    assert len(blank.parameters["top_curve"]) == 5


# --- end to end ------------------------------------------------------------------------


@pytest.mark.django_db
def test_manual_model_replays_through_the_worker_and_auto_returns() -> None:
    """Run on SQLite here and on PostgreSQL by tests/integration/test_curvature_postgres.py."""
    import io

    from ebook_server.core.models import ScanPage
    from tests.test_scan_jobs import _create_scan_job, _upload_scan_page
    from tests.test_scantailor import _stage_url

    client, job = _create_scan_job("scan-user-cylindrical")
    page_image = Image.new("L", (400, 600), 235)
    payload = io.BytesIO()
    page_image.save(payload, format="PNG")
    _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=payload.getvalue(), filename="p.png"
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1)
    url = _stage_url(str(job.id), str(page.id), "dewarping")
    manual = {
        "dewarp_mode": "manual",
        "top_curve": [list(p) for p in TOP],
        "bottom_curve": [list(p) for p in BOTTOM],
        "depth_perception": 2.5,
    }
    transforms = []
    for _ in range(2):  # the same manual model replays to the same result
        response = client.post(
            url, data=json.dumps({"parameters": manual}), content_type="application/json"
        )
        assert response.status_code == 202, response.content
        state = ScanTailorStageState.objects.get(scan_page=page, stage="dewarping")
        assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error
        assert state.algorithm == "cylindrical_dewarp"
        assert state.algorithm_version == scantailor.DEFAULT_STAGE_ALGORITHM_VERSION["dewarping"]
        assert state.parameters["dewarp_mode"] == "manual"
        assert state.parameters["top_curve"] == [list(p) for p in TOP]
        transforms.append(state.parameters["transform"])
    assert transforms[0] == transforms[1]
    assert transforms[0]["op"] == "cylindrical"
    assert transforms[0]["depth"] == 2.5

    response = client.post(
        url,
        data=json.dumps({"parameters": {"dewarp_mode": "auto"}}),
        content_type="application/json",
    )
    assert response.status_code == 202
    state = ScanTailorStageState.objects.get(scan_page=page, stage="dewarping")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error
    assert state.parameters["dewarp_mode"] == "auto"
    assert "No text lines" in state.parameters["dewarp_warning"]
    assert state.parameters["transform"] == {"op": "identity"}
