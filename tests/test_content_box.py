"""The ContentBoxFinder port (#195 step 5): primitives against brute force, fixtures."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest
from PIL import Image, ImageDraw

from ebook_server.core import binary_image, content_box, despeckle, scan_transform, scantailor
from ebook_server.core.max_whitespace import (
    AUTO_OBSTACLES,
    MaxWhitespaceFinder,
    area_quality,
)
from ebook_server.core.models import ScanTailorStageState

# --- references ---------------------------------------------------------------------


def _upstream_wolf(gray: np.ndarray, window: tuple[int, int], lower: int, upper: int) -> np.ndarray:
    """imageproc/Binarize.cpp binarizeWolf, loop for loop."""
    h, w = gray.shape
    win_w, win_h = window
    lower_half, upper_half = win_h >> 1, win_h - (win_h >> 1)
    left_half, right_half = win_w >> 1, win_w - (win_w >> 1)
    means = np.zeros((h, w))
    deviations = np.zeros((h, w))
    for y in range(h):
        top, bottom = max(0, y - lower_half), min(h, y + upper_half)
        for x in range(w):
            left, right = max(0, x - left_half), min(w, x + right_half)
            block = gray[top:bottom, left:right].astype(np.float64)
            mean = block.sum() / block.size
            variance = (block * block).sum() / block.size - mean * mean
            means[y, x] = np.float32(mean)
            deviations[y, x] = np.float32(math.sqrt(abs(variance)))
    max_dev = deviations.max()
    min_gray = gray.min()
    out = np.zeros((h, w), dtype=bool)
    for y in range(h):
        for x in range(w):
            a = 1.0 - deviations[y, x] / max_dev
            threshold = means[y, x] - 0.3 * a * (means[y, x] - min_gray)
            g = int(gray[y, x])
            out[y, x] = g < lower or (g <= upper and g < threshold)
    return out


def _brute_sedm(image: np.ndarray, lr: bool, tb: bool) -> np.ndarray:
    h, w = image.shape
    points = [(y, x) for y in range(h) for x in range(w) if image[y, x]]
    out = np.zeros((h, w))
    for y in range(h):
        for x in range(w):
            best = binary_image.SEDM_INF
            for py, px in points:
                best = min(best, (py - y) ** 2 + (px - x) ** 2)
            if lr:
                best = min(best, (x + 1) ** 2, (w - x) ** 2)
            if tb:
                best = min(best, (y + 1) ** 2, (h - y) ** 2)
            out[y, x] = best
    return out


def _brute_max_empty_rect(image: np.ndarray) -> int:
    h, w = image.shape
    best = 0
    for top, bottom in itertools.combinations_with_replacement(range(h), 2):
        for left, right in itertools.combinations_with_replacement(range(w), 2):
            if not image[top : bottom + 1, left : right + 1].any():
                best = max(best, (bottom - top + 1) * (right - left + 1))
    return best


# --- primitives ---------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(3))
def test_wolf_binarisation_matches_upstream(seed: int) -> None:
    rng = np.random.default_rng(seed)
    gray = np.clip(rng.normal(180, 40, (23, 31)), 0, 255).astype(np.uint8)
    gray[5:9, 4:20] = 30  # some text-like ink
    expected = _upstream_wolf(gray, (7, 5), 50, 254)
    assert np.array_equal(binary_image.binarize_wolf(gray, (7, 5), lower_bound=50), expected)


@pytest.mark.parametrize("borders", [(False, False), (True, False), (False, True)])
def test_sedm_matches_brute_force(borders: tuple[bool, bool]) -> None:
    rng = np.random.default_rng(11)
    image = rng.random((13, 17)) < 0.05
    expected = _brute_sedm(image, *borders)
    got = binary_image.sedm(image, left_right_borders=borders[0], top_bottom_borders=borders[1])
    assert np.array_equal(got, expected)
    assert np.all(binary_image.sedm(np.zeros((4, 4), bool)) == binary_image.SEDM_INF)


def test_sedm_peaks_are_regional_maxima() -> None:
    distances = np.array(
        [
            [0, 1, 1, 1, 0],
            [1, 4, 4, 2, 1],
            [1, 2, 2, 5, 1],
            [0, 1, 1, 1, 0],
        ],
        dtype=np.float64,
    )
    peaks = binary_image.sedm_peaks(distances, padding=0)
    # The 4-plateau neighbours the 5, so only the 5 is a peak.
    assert np.argwhere(peaks).tolist() == [[2, 3]]
    flat = np.array([[0, 0, 0], [0, 3, 0], [0, 0, 0]], dtype=np.float64)
    assert binary_image.sedm_peaks(flat, padding=0).tolist() == [
        [False, False, False],
        [False, True, False],
        [False, False, False],
    ]


@pytest.mark.parametrize("seed", range(4))
def test_max_whitespace_finder_returns_the_largest_empty_rectangle_first(seed: int) -> None:
    rng = np.random.default_rng(seed)
    image = rng.random((9, 11)) < 0.12
    finder = MaxWhitespaceFinder(image, area_quality)
    rect = finder.next(AUTO_OBSTACLES)
    assert rect is not None
    left, top, right, bottom = rect
    assert not image[top : bottom + 1, left : right + 1].any()
    assert (right - left + 1) * (bottom - top + 1) == _brute_max_empty_rect(image)
    # AUTO_OBSTACLES: the next rectangle does not overlap the first.
    second = finder.next(AUTO_OBSTACLES)
    if second is not None:
        overlap_w = min(right, second[2]) - max(left, second[0]) + 1
        overlap_h = min(bottom, second[3]) - max(top, second[1]) + 1
        assert overlap_w <= 0 or overlap_h <= 0


def test_despeckle_keeps_letters_and_their_dots_but_drops_isolated_specks() -> None:
    image = np.zeros((200, 300), dtype=bool)
    image[50:70, 40:46] = True  # a letter stem, 20 px tall: big at 150 dpi (threshold 6)
    image[44:46, 41:44] = True  # its dot, 2 px above
    image[150, 250] = True  # an isolated speck far away
    cleaned = despeckle.despeckle(image, (150, 150), "normal")
    assert cleaned[50:70, 40:46].all()
    assert cleaned[44:46, 41:44].all()
    assert not cleaned[150, 250]
    # Nothing big on the page: upstream removes everything.
    assert not despeckle.despeckle(image[140:160, 240:260], (150, 150)).any()
    assert despeckle.settings_for("normal", (150, 150)).big_object_threshold == 6


def test_trim_and_border_removal() -> None:
    blocks = np.zeros((20, 20), dtype=bool)
    blocks[2:12, 2:12] = True
    content = np.zeros_like(blocks)
    content[4:6, 5:9] = True
    content_box.trim_content_blocks_in_place(content, blocks)
    assert np.argwhere(blocks).min(axis=0).tolist() == [4, 5]
    assert np.argwhere(blocks).max(axis=0).tolist() == [5, 8]

    border = np.zeros((40, 40), dtype=bool)
    border[0:30, 0:3] = True  # a block on the left edge
    border[15:25, 15:25] = True  # one in the middle
    content_box.remove_areas_touching_borders(border)
    assert not border[:, :3].any()
    assert border[15:25, 15:25].all()


# --- fixtures with a known content box ------------------------------------------------


def _page(
    width: int = 1240,
    height: int = 1754,
    *,
    shadow: bool = True,
    speck: bool = False,
    seed: int = 3,
) -> np.ndarray:
    """A 150 dpi page: glyph-like text from (180, 240) to (1060, ~1500), a page number."""
    rng = np.random.default_rng(seed)
    image = Image.new("L", (width, height), 232)
    draw = ImageDraw.Draw(image)
    for y in range(240, 1500, 26):
        x = 180
        while x < 1060:
            word = int(rng.integers(20, 90))
            glyph_x = x
            while glyph_x < min(x + word, 1060):
                draw.rectangle((glyph_x, y, min(glyph_x + 6, 1060), y + 11), fill=30)
                glyph_x += 9
            x += word + 14
    draw.rectangle((610, 1600, 630, 1612), fill=30)  # page number
    if shadow:
        draw.rectangle((0, 0, 40, height), fill=40)  # binding shadow
        draw.rectangle((0, height - 30, width, height), fill=50)  # edge shadow
    if speck:
        draw.rectangle((1180, 80, 1182, 82), fill=20)
    return np.asarray(image)


def test_finds_the_text_and_page_number_but_not_the_shadows() -> None:
    box = content_box.find_content_box(_page())
    assert box is not None
    left, top, right, bottom = box
    assert abs(left - 180) <= 2 and abs(top - 240) <= 2
    assert abs(right - 1060) <= 2 and abs(bottom - 1612) <= 2


def test_dust_speck_in_the_margin_is_ignored() -> None:
    box = content_box.find_content_box(_page(shadow=False, speck=True))
    assert box is not None
    assert box[2] <= 1062 and box[1] >= 238


def test_blank_page_has_no_content() -> None:
    assert content_box.find_content_box(np.full((1754, 1240), 232, np.uint8)) is None


def test_area_outside_the_page_outline_is_not_content() -> None:
    page = _page(shadow=False)
    mask = np.ones(page.shape, dtype=bool)
    mask[:, 1150:] = False  # added by a rotation upstream, e.g. Deskew's canvas
    gray = page.copy()
    gray[:, 1150:] = 232
    gray[600:700, 1160:1230] = 20  # something drawn there must not count
    box = content_box.find_content_box(gray, mask)
    assert box is not None and box[2] <= 1062


def test_scaling_to_150_dpi() -> None:
    gray = np.zeros((3508, 2480), dtype=np.uint8)
    scaled, sx, sy = content_box.to_working(gray, (300, 300))
    assert scaled.shape == (1754, 1240)
    assert sx == pytest.approx(0.5) and sy == pytest.approx(0.5)
    same, sx, sy = content_box.to_working(gray[:10, :10], (150, 150))
    assert same.shape == (10, 10) and sx == sy == 1.0


# --- the Select Content stage ---------------------------------------------------------


def _stage_input(gray: np.ndarray, **extra: object) -> scantailor.StageInput:
    image = Image.fromarray(gray)
    return scantailor.StageInput(
        image=image,
        full_size=image.size,
        render_full=lambda: image.copy(),
        dpi=(150, 150),
        **extra,  # type: ignore[arg-type]
    )


def _state(parameters: dict[str, object] | None = None) -> ScanTailorStageState:
    return ScanTailorStageState(stage="select_content", parameters=parameters or {})


def test_auto_records_the_box_its_physical_size_and_crops_to_it() -> None:
    result = scantailor._run_select_content(_state(), _stage_input(_page()))
    params = result.parameters
    assert params["content_mode"] == "auto" and params["content_found"] is True
    box = params["content_box"]
    assert abs(box["x1"] - 180) <= 2 and abs(box["y2"] - 1613) <= 2
    # PhysSizeCalc: pixels at 150 dpi in millimetres.
    assert params["content_size_mm"]["width"] == pytest.approx(box["width"] * 25.4 / 150, abs=0.1)
    assert params["transform"]["op"] == "crop"
    assert result.full_size == (box["width"], box["height"])


def test_manual_disabled_and_empty_pages() -> None:
    page = _page(shadow=False)
    manual = scantailor._run_select_content(
        _state({"content_mode": "manual", "content_box": [100, 120, 900, 1300]}),
        _stage_input(page),
    )
    assert manual.parameters["content_mode"] == "manual"
    assert manual.full_size == (800, 1180)

    disabled = scantailor._run_select_content(
        _state({"content_mode": "disabled"}), _stage_input(page)
    )
    assert disabled.parameters["content_mode"] == "disabled"
    assert disabled.parameters["transform"] == {"op": "identity"}
    assert disabled.full_size == (1240, 1754)

    blank = scantailor._run_select_content(
        _state(), _stage_input(np.full((1754, 1240), 232, np.uint8))
    )
    assert blank.parameters["content_found"] is False
    assert blank.parameters["transform"] == {"op": "identity"}


def test_page_outline_follows_the_deskew_rotation() -> None:
    op = scan_transform.rotate(-3.0)  # Deskew turned the page 3 degrees clockwise
    canvas = scan_transform.output_size(op, (1000, 1400))
    stage = _stage_input(np.full((canvas[1], canvas[0]), 232, np.uint8))
    stage = scantailor.StageInput(
        image=stage.image.resize(canvas),
        full_size=canvas,
        render_full=stage.render_full,
        dpi=(150, 150),
        ops=(op,),
        original_size=(1000, 1400),
    )
    outline = scantailor._page_outline(stage)
    assert outline is not None
    xs = [x for x, _ in outline]
    ys = [y for _, y in outline]
    assert min(xs) == pytest.approx(0, abs=1) and max(xs) == pytest.approx(canvas[0], abs=1)
    assert min(ys) == pytest.approx(0, abs=1) and max(ys) == pytest.approx(canvas[1], abs=1)
    # Clockwise: the top-left corner moved right along the top edge.
    assert outline[0][1] == pytest.approx(0, abs=1) and outline[0][0] > 50
