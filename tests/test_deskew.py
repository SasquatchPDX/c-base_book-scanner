"""The SkewFinder port (#195 step 4): equivalence with upstream and known-angle fixtures."""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from ebook_server.core import binary_image, deskew, scan_transform

ROTATION_CASES = Path(__file__).parent / "fixtures" / "deskew_rotation_cases.json"


# --- literal transcriptions of the upstream C++ (reference implementations) -------


def _upstream_otsu(gray: np.ndarray) -> int:
    """imageproc/BinaryThreshold.cpp otsuThreshold, loop for loop."""
    counts = np.bincount(gray.ravel(), minlength=256)
    pixels_by_threshold = [0] * 256
    moment_by_threshold = [0] * 256
    pixels_by_threshold[0] = int(counts[0])
    for i in range(1, 256):
        pixels_by_threshold[i] = pixels_by_threshold[i - 1] + int(counts[i])
        moment_by_threshold[i] = moment_by_threshold[i - 1] + int(counts[i]) * i
    total_pixels = pixels_by_threshold[255]
    total_moment = moment_by_threshold[255]
    max_variance = 0.0
    first_best = last_best = -1
    for i in range(256):
        below = pixels_by_threshold[i]
        above = total_pixels - below
        if below > 0 and above > 0:
            moment_below = moment_by_threshold[i]
            mean_diff = moment_below / below - (total_moment - moment_below) / above
            variance = mean_diff * mean_diff * below * above
            if variance > max_variance:
                max_variance = variance
                first_best = last_best = i
            elif variance == max_variance:
                last_best = i
    return (first_best + 1 + last_best + 1) >> 1


def _upstream_vshear_score(image: np.ndarray, shear: float) -> float:
    """imageproc/Shear.cpp vShearFromTo + SkewFinder::calcScore, column by column."""
    height, width = image.shape
    x_origin = 0.5 * width
    dst = np.zeros_like(image)
    shift = 0.5 + shear * (0.5 - x_origin)
    shift_end = 0.5 + shear * (width - 0.5 - x_origin)
    if math.floor(shift) == math.floor(shift_end):
        dst = image.copy()
    else:
        for x in range(width):
            s = math.floor(0.5 + shear * (x + 0.5 - x_origin))
            if abs(s) >= height:
                continue
            if s < 0:
                dst[: height + s, x] = image[-s:, x]
            elif s > 0:
                dst[s:, x] = image[: height - s, x]
            else:
                dst[:, x] = image[:, x]
    counts = dst.sum(axis=1).astype(np.int64)
    score = 0.0
    for y in range(1, height):
        diff = counts[y] - counts[y - 1]
        score += diff * diff
    return score


def _text_page(width: int = 1240, height: int = 1754, seed: int = 7) -> Image.Image:
    """A synthetic 150 dpi A4 page: lines of word-like bars on light paper."""
    rng = np.random.default_rng(seed)
    image = Image.new("L", (width, height), 236)
    draw = ImageDraw.Draw(image)
    for y in range(int(height * 0.1), int(height * 0.9), 30):
        x = int(width * 0.1)
        while x < width * 0.88:
            word = int(rng.integers(18, 90))
            draw.rectangle((x, y, min(x + word, int(width * 0.9)), y + 10), fill=24)
            x += word + 12
    return image


# --- primitives -------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(6))
def test_otsu_threshold_matches_upstream(seed: int) -> None:
    rng = np.random.default_rng(seed)
    gray = np.concatenate(
        [rng.normal(40, 12, 3000), rng.normal(210, 20, 9000), rng.integers(0, 256, 500)]
    )
    gray = np.clip(gray, 0, 255).astype(np.uint8).reshape(100, 125)
    assert binary_image.otsu_threshold(gray) == _upstream_otsu(gray)


def test_otsu_threshold_on_a_two_level_image_and_a_blank_one() -> None:
    gray = np.array([[10, 10, 200, 200]], dtype=np.uint8)
    threshold = binary_image.otsu_threshold(gray)
    assert threshold == _upstream_otsu(gray)
    assert binary_image.binarize(gray, threshold).tolist() == [[True, True, False, False]]
    assert binary_image.otsu_threshold(np.full((5, 5), 235, np.uint8)) == 0


@pytest.mark.parametrize("threshold", [1, 2, 3, 4])
@pytest.mark.parametrize("shape", [(7, 9), (8, 8), (1, 9), (9, 1), (1, 1)])
def test_reduce_threshold_counts_black_pixels_per_block(
    threshold: int, shape: tuple[int, int]
) -> None:
    rng = np.random.default_rng(threshold * 31 + shape[0])
    image = rng.random(shape) < 0.5
    reduced = binary_image.reduce_threshold(image, threshold)
    height, width = shape
    if shape == (1, 1):
        assert reduced.tolist() == image.tolist()
        return
    if height == 1 or width == 1:
        line = image.ravel()
        pairs = [
            int(line[2 * i]) + int(line[2 * i + 1]) >= (1 if threshold <= 2 else 2)
            for i in range(line.size // 2)
        ]
        assert reduced.ravel().tolist() == pairs
        return
    blocks = [
        [
            bool(image[2 * y : 2 * y + 2, 2 * x : 2 * x + 2].sum() >= threshold)
            for x in range(width // 2)
        ]
        for y in range(height // 2)
    ]
    assert reduced.tolist() == blocks


@pytest.mark.parametrize("angle", [-7.0, -3.0, -0.5, 0.0, 0.25, 1.0, 4.0, 6.5])
def test_block_scorer_equals_upstream_shear_and_score(angle: float) -> None:
    rng = np.random.default_rng(int(angle * 100) + 1000)
    image = rng.random((61, 97)) < 0.2
    scorer = deskew._Scorer(image, 1.0)
    shear = math.tan(math.radians(angle))
    assert scorer(angle) == pytest.approx(_upstream_vshear_score(image, shear))


def test_reductions_reach_upstream_target_resolutions() -> None:
    # (coarse, fine, reductions already done)
    assert deskew.reductions_for(300) == (2, 1, 0)  # upstream defaults
    assert deskew.reductions_for(150) == (1, 0, 1)  # working image: 75 / 150 dpi
    assert deskew.reductions_for(600) == (3, 2, 0)


def test_horizontal_shadow_is_removed_text_is_kept() -> None:
    page = np.asarray(_text_page(800, 1000)) < 128
    shadowed = page.copy()
    shadowed[:40, :] = True  # a dark band along the top edge
    cleaned = deskew.remove_horizontal_shadows(shadowed, (150.0, 150.0))
    assert not cleaned[:40, :].any()
    assert np.array_equal(cleaned[40:], page[40:])


# --- known-angle fixtures ---------------------------------------------------------


@pytest.mark.parametrize("skew", [-6.5, -3.2, -1.0, -0.3, 0.0, 0.4, 1.7, 4.9, 6.8])
def test_detect_finds_known_skew_within_upstream_accuracy(skew: float) -> None:
    # Pillow rotates counter-clockwise; the correcting deskew angle is clockwise.
    page = _text_page().rotate(skew, expand=True, fillcolor=236, resample=Image.Resampling.BICUBIC)
    found = deskew.detect(np.asarray(page), (150.0, 150.0))
    assert found.applied
    assert found.confidence >= deskew.GOOD_CONFIDENCE
    assert found.angle == pytest.approx(skew, abs=deskew.DEFAULT_ACCURACY + 0.02)


def test_horizontal_shadow_does_not_bias_detection() -> None:
    page = _text_page().rotate(2.0, expand=True, fillcolor=236, resample=Image.Resampling.BICUBIC)
    pixels = np.asarray(page).copy()
    pixels[:60, :] = 20  # a straight dark scanner-lid shadow
    found = deskew.detect(pixels, (150.0, 150.0))
    assert found.angle == pytest.approx(2.0, abs=0.12)


def test_poor_confidence_applies_no_rotation() -> None:
    # A nearly empty page (a page number and a smudge) scores at or below upstream's
    # LOW_SCORE: zero confidence, so no rotation, whatever angle scored best.
    page = Image.new("L", (900, 1200), 236)
    draw = ImageDraw.Draw(page)
    draw.rectangle((440, 1120, 460, 1132), fill=30)
    draw.line((100, 300, 160, 340), fill=60, width=3)
    found = deskew.detect(np.asarray(page), (150.0, 150.0))
    assert not found.applied
    assert found.angle == 0.0
    assert found.confidence < deskew.GOOD_CONFIDENCE


def test_find_skew_rejects_invalid_settings() -> None:
    image = np.zeros((10, 10), dtype=bool)
    with pytest.raises(ValueError):
        deskew.find_skew(image, max_angle=50)
    with pytest.raises(ValueError):
        deskew.find_skew(image, resolution_ratio=0)


# --- the editor's live preview turns the page exactly as the server does ----------


def _css_rotation(
    point: tuple[float, float], size: tuple[int, int], angle: float
) -> tuple[float, float]:
    """What the editor shows: CSS ``rotate(angle)`` (clockwise) about the image centre,
    placed on the canvas that holds the whole rotated page."""
    width, height = size
    radians = math.radians(angle)
    cos, sin = math.cos(radians), math.sin(radians)
    canvas_w = abs(width * cos) + abs(height * sin)
    canvas_h = abs(width * sin) + abs(height * cos)
    dx, dy = point[0] - width / 2, point[1] - height / 2
    return canvas_w / 2 + dx * cos - dy * sin, canvas_h / 2 + dx * sin + dy * cos


def test_server_rotation_matches_the_editor_preview() -> None:
    """Shared with frontend/editor-shell.test.ts, which checks ``deskewMapPoint``."""
    cases = json.loads(ROTATION_CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 6
    for case in cases:
        op = scan_transform.rotate(-case["angle"])  # the op the Deskew stage records
        size = tuple(case["size"])
        for point, expected in zip(case["points"], case["expected"], strict=True):
            # map_point works on pixel indices; the editor on continuous coordinates.
            mapped = scan_transform.map_point(op, (point[0] - 0.5, point[1] - 0.5), size)
            assert (mapped[0] + 0.5, mapped[1] + 0.5) == pytest.approx(expected, abs=1e-6)
            assert _css_rotation(point, size, case["angle"]) == pytest.approx(expected, abs=1e-6)
        canvas = scan_transform.output_size(op, size)
        assert canvas == (
            math.ceil(case["canvas"][0] - 1e-6),
            math.ceil(case["canvas"][1] - 1e-6),
        )
