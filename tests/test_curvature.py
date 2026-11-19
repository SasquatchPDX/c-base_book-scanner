"""Actual remapped pixels, geometric improvement, replay, and memory bounds (#171)."""

from typing import Any

import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from ebook_server.core import curvature
from tests.curvature_fixtures import curled_page, straightness


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


def test_isolated_crossing_auto_trace_does_not_discard_ordered_text_rows() -> None:
    good = [[[0.0, y], [500.0, y], [999.0, y]] for y in (100.0, 200.0, 300.0)]
    bad = [[0.0, 250.0], [500.0, 180.0], [999.0, 250.0]]
    candidates = [(0.9, curve) for curve in good] + [(0.2, bad)]
    selected = curvature._ordered_candidates(candidates)
    assert selected == good
    assert curvature.normalize_curves(selected, (1000, 500)) == good
    # The automatic filter must not make the same contradictory manual edit valid.
    with pytest.raises(ValueError, match="cross"):
        curvature.normalize_curves([*good, bad], (1000, 500))
