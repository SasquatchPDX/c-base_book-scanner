# Derived from Scan Tailor filters/select_content/ContentBoxFinder.cpp,
# filters/select_content/ContentBoxFinder.h, imageproc/InfluenceMap.cpp and
# imageproc/SlicedHistogram.cpp
# Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
# Copyright (C) c-base_book-scanner contributors
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# See the LICENSE file at the repository root.
"""Automatic content box: a port of upstream ``ContentBoxFinder`` (#195 step 5).

The page is processed at 150 dpi, as upstream does:

1. Wolf binarisation (51x51 window); everything outside the page outline is black.
2. Shadows: black regions holding a long horizontal (200x14) or vertical (14x300)
   bar, grown through a 3x3 dilation; ``filterShadows`` gives back the parts that
   look like white text on a dark background. The rest is *garbage*.
3. ``content`` = black minus garbage, despeckled (``Despeckle`` NORMAL).
4. Content blocks: the page minus the largest whitespace rectangles (80 preferring
   wide ones, 40 preferring tall ones, then 10 more by area), each block trimmed to
   the content inside it; blocks reaching in from the page border are removed.
5. A text mask: blocks whose row profile splits into text lines with a plausible
   fill factor, aspect ratio and enough ultimate eroded points.
6. The bounding box of the blocks is trimmed side by side: a strip separated by
   whitespace is dropped when it lies closer to the garbage than to the remaining
   content, weighted by how much text it holds.

Divergences, with reasons (also in ``docs/third-party/scantailor-port.md``):

- ``InfluenceMap`` (used to split garbage into horizontal and vertical parts)
  propagates seed labels through the garbage with distance vectors; here each pixel
  of an 8-connected garbage region takes the nearest seed of that region by straight
  Euclidean distance. They differ only where a region bends around a corner.
- ``inPlaceRemoveAreasTouchingBorders`` seeds its limited flood from the first row,
  the first and last columns of rows 0..h-3 and row h-2 (an off-by-one in the
  pointer arithmetic). The port seeds all four borders, as its comment intends.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np

from . import despeckle as despeckle_module
from .binary_image import (
    SEDM_INF,
    Binary,
    binarize_wolf,
    bounding_box,
    dilate_brick,
    open_brick,
    sedm,
    sedm_peaks,
    seed_fill,
)
from .max_whitespace import (
    MANUAL_OBSTACLES,
    MaxWhitespaceFinder,
    Rect,
    area_quality,
    prefer_horizontal,
    prefer_vertical,
)

#: Upstream finds the content box on the page scaled to 150 dpi.
WORK_DPI: Final[float] = 150.0
MIN_TEXT_HEIGHT: Final[int] = 6


def _width(rect: Rect) -> int:
    return rect[2] - rect[0] + 1


def _height(rect: Rect) -> int:
    return rect[3] - rect[1] + 1


def _empty(rect: Rect) -> bool:
    return rect[2] < rect[0] or rect[3] < rect[1]


def _view(image: np.ndarray, rect: Rect) -> np.ndarray:
    return image[rect[1] : rect[3] + 1, rect[0] : rect[2] + 1]


# --- garbage -------------------------------------------------------------------------


class _Garbage:
    """``ContentBoxFinder::Garbage``: garbage pixels and their distance map, where the
    page borders across the garbage's direction count as garbage too."""

    def __init__(self, image: Binary, *, vertical: bool) -> None:
        self.image = image.copy()
        self._vertical = vertical
        self._sedm: np.ndarray | None = None

    def add(self, content: Binary, rect: Rect) -> None:
        _view(self.image, rect)[...] |= _view(content, rect)
        self._sedm = None

    def sedm(self) -> np.ndarray:
        if self._sedm is None:
            self._sedm = sedm(
                self.image,
                left_right_borders=self._vertical,
                top_bottom_borders=not self._vertical,
            )
        return self._sedm


# --- steps ---------------------------------------------------------------------------


def trim_content_blocks_in_place(content: Binary, blocks: Binary) -> None:
    """Shrink each 4-connected block to the bounding box of the content inside it."""
    from scipy import ndimage

    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        blocks.view(np.uint8), connectivity=4
    )
    if count <= 1:
        return
    masked = np.where(content, labels, 0)
    content_boxes = ndimage.find_objects(masked, max_label=count - 1)
    for label in range(1, count):
        x, y, w, h = (int(value) for value in stats[label, :4])
        region = (slice(y, y + h), slice(x, x + w))
        found = content_boxes[label - 1]
        if found is not None:
            rows, cols = found
            if (rows.start, rows.stop, cols.start, cols.stop) == (y, y + h, x, x + w):
                continue  # the content fills the block's bounding box
        own = labels[region] == label
        keep = np.zeros_like(own)
        if found is not None:
            rows, cols = found
            keep[rows.start - y : rows.stop - y, cols.start - x : cols.stop - x] = True
        blocks[region] &= ~own | keep


def remove_areas_touching_borders(blocks: Binary) -> None:
    """``inPlaceRemoveAreasTouchingBorders``: remove block pixels within a limited
    4-connected distance (a quarter of the shorter side) of a block pixel on the border."""
    from scipy import ndimage

    height, width = blocks.shape
    max_spread = min(width, height) // 4
    seeds = np.zeros_like(blocks)
    seeds[0, :] = blocks[0, :]
    seeds[-1, :] = blocks[-1, :]
    seeds[:, 0] |= blocks[:, 0]
    seeds[:, -1] |= blocks[:, -1]
    if max_spread < 1 or not seeds.any():
        return
    reached = seeds
    if max_spread > 1:
        reached = ndimage.binary_dilation(
            seeds,
            structure=ndimage.generate_binary_structure(2, 1),
            iterations=max_spread - 1,
            mask=blocks,
        )
    blocks &= ~reached


def segment_garbage(garbage: Binary) -> tuple[Binary, Binary]:
    """``segmentGarbage``: split garbage into horizontal and vertical garbage.

    Seeds are the long horizontal and vertical runs (plus the garbage on the page
    edges). Upstream's ``InfluenceMap`` then gives every garbage pixel the label of
    the seed it reaches first through the garbage; here every pixel of an 8-connected
    garbage region takes the label of the nearest seed inside that region (Euclidean).
    Regions without a seed belong to both kinds.
    """
    hor = open_brick(garbage, (200, 1))
    hor[0, :] |= garbage[0, :]
    hor[-1, :] |= garbage[-1, :]
    vert = open_brick(garbage, (1, 200))
    vert[:, 0] |= garbage[:, 0]
    vert[:, -1] |= garbage[:, -1]
    vert &= ~hor  # the horizontal seeds are added to the map last and win overlaps
    vert_garbage = np.zeros_like(garbage)
    hor_garbage = np.zeros_like(garbage)
    region_mask = garbage | hor | vert
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        region_mask.view(np.uint8), connectivity=8
    )
    for label in range(1, count):
        x, y, w, h = (int(value) for value in stats[label, :4])
        window = (slice(y, y + h), slice(x, x + w))
        own = labels[window] == label
        own_vert = vert[window] & own
        own_hor = hor[window] & own
        has_vert, has_hor = bool(own_vert.any()), bool(own_hor.any())
        if has_vert and not has_hor:
            vert_garbage[window] |= own
        elif has_hor and not has_vert:
            hor_garbage[window] |= own
        elif has_vert and has_hor:
            to_vert = cv2.distanceTransform(
                np.ascontiguousarray(~own_vert).view(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE
            )
            to_hor = cv2.distanceTransform(
                np.ascontiguousarray(~own_hor).view(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE
            )
            nearer_vert = to_vert < to_hor
            vert_garbage[window] |= own & nearer_vert
            hor_garbage[window] |= own & ~nearer_vert
    unconnected = garbage & ~hor_garbage & ~vert_garbage
    return hor_garbage | unconnected, vert_garbage | unconnected


def estimate_text_mask(content: Binary, blocks: Binary) -> Binary:
    """``estimateTextMask``: blocks whose rows look like lines of text."""
    peaks = sedm_peaks(sedm(content))
    text_mask = np.zeros_like(content)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        blocks.view(np.uint8), connectivity=4
    )
    for label in range(1, count):
        x, y, w, h = (int(value) for value in stats[label, :4])
        cc_img = labels[y : y + h, x : x + w] == label
        content_img = content[y : y + h, x : x + w] & cc_img
        hist = content_img.sum(axis=1).astype(np.int64)
        block_hist = cc_img.sum(axis=1).astype(np.int64)
        for first, last in _text_line_ranges(hist):
            line = _text_line(hist, block_hist, first, last)
            if line is None:
                continue
            top, bottom = line
            line_rect = (x, y + top, x + w - 1, y + bottom)
            ueps_todo = int(0.4 * _width(line_rect) / _height(line_rect))
            if ueps_todo:
                line_ueps = _view(blocks, line_rect) & _view(peaks, line_rect)
                found = cv2.connectedComponents(line_ueps.astype(np.uint8), connectivity=4)[0] - 1
                if found < ueps_todo:
                    continue  # not enough ultimate eroded points
            _view(text_mask, line_rect)[...] |= cc_img[top : bottom + 1]
    return text_mask


def _text_line_ranges(hist: np.ndarray) -> list[tuple[int, int]]:
    """Split a block's row histogram at deep valleys (candidate text lines)."""
    ranges: list[tuple[int, int]] = []
    splittable = [(0, hist.size - 1)]
    while splittable:
        first, last = splittable.pop()
        if last - first < MIN_TEXT_HEIGHT - 1:
            continue
        segment = hist[first : last + 1]
        max_forward = np.maximum.accumulate(segment)
        max_backward = np.maximum.accumulate(segment[::-1])
        positions = np.arange(first + 1, last)
        if positions.size == 0:
            ranges.append((first, last))
            continue
        values = hist[positions]
        peak1 = max_forward[positions - (first + 1)]
        peak2 = max_backward[(last - 1) - positions]
        shoulder1 = peak1 - values
        shoulder2 = peak2 - values
        valid = (
            ~(values * 3.5 > 0.5 * (peak1 + peak2))
            & (shoulder1 > 0)
            & (shoulder2 > 0)
            & ~(np.minimum(shoulder1, shoulder2) * 20 < np.maximum(shoulder1, shoulder2))
        )
        if not valid.any():
            ranges.append((first, last))
            continue
        magnitude = np.where(valid, shoulder1 + shoulder2, np.iinfo(np.int64).min)
        split = int(positions[int(np.argmax(magnitude))])
        splittable.append((first, split - 1))
        splittable.append((split + 1, last))
    return ranges


def _text_line(
    hist: np.ndarray, block_hist: np.ndarray, first: int, last: int
) -> tuple[int, int] | None:
    """The rows of a text line around a range's ink centroid, or None."""
    if last - first < MIN_TEXT_HEIGHT - 1:
        return None
    rows = np.arange(first, last + 1)
    weights = hist[first : last + 1]
    total_weight = int(weights.sum())
    if total_weight == 0:
        return None
    weighted_y = int((weights * rows).sum())
    min_fill, max_fill = 0.22, 0.65
    center_y = (weighted_y + total_weight // 2) // total_weight
    top = center_y - MIN_TEXT_HEIGHT // 2
    bottom = top + MIN_TEXT_HEIGHT - 1
    if top < first or bottom > last:
        return None
    num_black = int(hist[top : bottom + 1].sum())
    num_total = int(block_hist[top : bottom + 1].sum())
    max_width = int(block_hist[top : bottom + 1].max())
    if num_black < num_total * min_fill or num_black > num_total * max_fill:
        return None
    while (top > first or bottom < last) and abs((center_y - top) - (bottom - center_y)) <= 1:
        new_top = top - 1 if top > first else top
        new_bottom = bottom + 1 if bottom < last else bottom
        num_black += int(hist[new_top] + hist[new_bottom])
        num_total += int(block_hist[new_top] + block_hist[new_bottom])
        if num_black < num_total * min_fill:
            break
        max_width = max(max_width, int(block_hist[new_top]), int(block_hist[new_bottom]))
        top, bottom = new_top, new_bottom
    if num_black > num_total * max_fill:
        return None
    if max_width < (bottom - top + 1) * 0.6:
        return None
    return top, bottom


def filter_shadows(shadows: Binary) -> Binary:
    """``filterShadows``: give back shadow parts that look like white text."""
    borders = np.zeros_like(shadows)
    borders[0, :] = borders[-1, :] = True
    borders[:, 0] = borders[:, -1] = True
    touching = seed_fill(borders, shadows)
    inner = shadows ^ touching
    if inner.any():
        inverted = ~inner
        background = seed_fill(borders, inverted)
        filled = ~background | inner  # the inner shadows with their holes
        text_mask = estimate_text_mask(inverted, filled)
        misclassified = seed_fill(text_mask, inner)
        inner = inner ^ misclassified
    return inner | touching


# --- trimming ------------------------------------------------------------------------


@dataclass
class _Images:
    content: Binary
    blocks: Binary
    text: Binary


def _trim(
    images: _Images,
    area: Rect,
    new_area: Rect,
    removed_area: Rect,
    garbage: _Garbage,
) -> tuple[Rect, bool]:
    """``ContentBoxFinder::trim``: (resulting area, can retry grouped)."""
    # Don't trim too much, unless the strip is thin (nothing but garbage on the page).
    if _width(removed_area) * _height(removed_area) > 0.3 * (
        _width(new_area) * _height(new_area)
    ) and not (_width(removed_area) < 6 or _height(removed_area) < 6):
        return area, False

    content_pixels = int(_view(images.content, removed_area).sum())
    vertical_cut = new_area[1] == area[1] and new_area[3] == area[3]
    proximity_bias = 0.5 if vertical_cut else 0.65
    num_text_pixels = int(_view(images.text, removed_area).sum())
    if num_text_pixels == 0:
        proximity_bias = 0.4 if vertical_cut else 0.5
    else:
        total_pixels = content_pixels + int(_view(garbage.image, removed_area).sum()) + 1
        min_influence, max_influence, upper_threshold = 0.2, 1.0, 5000
        text_influence = max_influence
        if num_text_pixels < upper_threshold:
            text_influence = min_influence + (max_influence - min_influence) * math.log(
                num_text_pixels
            ) / math.log(upper_threshold)
        proximity_bias += (1.0 - proximity_bias) * text_influence * num_text_pixels / total_pixels
        proximity_bias = min(1.0, max(0.0, proximity_bias))

    # Distances to the content that stays, computed on ``area`` only: everything that
    # stays lies inside it, so the nearest such pixel is found there too.
    local = np.zeros((_height(area), _width(area)), dtype=bool)
    nl, nt = new_area[0] - area[0], new_area[1] - area[1]
    local[nt : nt + _height(new_area), nl : nl + _width(new_area)] = _view(
        images.content, new_area
    ) & _view(images.blocks, new_area)
    to_others = sedm(local)
    rl, rt = removed_area[0] - area[0], removed_area[1] - area[1]
    others = to_others[rt : rt + _height(removed_area), rl : rl + _width(removed_area)]
    to_garbage = _view(garbage.sedm(), removed_area)
    in_blocks = _view(images.blocks, removed_area)
    sum_to_garbage = float(np.sqrt(to_garbage[in_blocks]).sum()) * proximity_bias
    sum_to_others = float(np.sqrt(others[in_blocks]).sum()) * (1.0 - proximity_bias)
    if sum_to_garbage < sum_to_others:
        garbage.add(images.content, removed_area)
        return new_area, False
    return area, proximity_bias < 0.85


def _trim_side(images: _Images, area: Rect, garbage: _Garbage, side: str) -> Rect:
    """``trimLeft`` / ``trimRight`` / ``trimTop`` / ``trimBottom``."""
    horizontal = side in ("left", "right")
    hist = _view(images.blocks, area).sum(axis=0 if horizontal else 1)
    size = hist.size
    offset = area[0] if horizontal else area[1]
    forward = side in ("left", "top")
    start = 0 if forward else size - 1
    while (start < size) if forward else (start >= 0):
        step = 1 if forward else -1
        first_ws = start
        while 0 <= first_ws < size and hist[first_ws] != 0:
            first_ws += step
        first_non_ws = first_ws
        while 0 <= first_non_ws < size and hist[first_non_ws] == 0:
            first_non_ws += step
        first_ws += offset
        first_non_ws += offset
        left, top, right, bottom = area
        if side == "left":
            new_area = (first_non_ws, top, right, bottom)
            removed = (left, top, first_ws - 1, bottom)
        elif side == "right":
            new_area = (left, top, first_non_ws, bottom)
            removed = (first_ws + 1, top, right, bottom)
        elif side == "top":
            new_area = (left, first_non_ws, right, bottom)
            removed = (left, top, right, first_ws - 1)
        else:
            new_area = (left, top, right, first_non_ws)
            removed = (left, first_ws + 1, right, bottom)
        if _empty(new_area):
            return area
        if _empty(removed):
            return new_area
        result, can_retry_grouped = _trim(images, area, new_area, removed, garbage)
        if not can_retry_grouped:
            return result
        start = first_non_ws - offset
    return area


# --- the finder ----------------------------------------------------------------------


def find_content_box(gray150: np.ndarray, page_mask: Binary | None = None) -> Rect | None:
    """``ContentBoxFinder::findContentBox`` on a page already at 150 dpi.

    ``page_mask`` marks the page itself (False where an earlier rotation added area);
    upstream fills those areas with the darkest grey before binarising and black after.
    Returns the content box ``(left, top, right, bottom)``, inclusive, or None when the
    page has no content.
    """
    gray = np.asarray(gray150, dtype=np.uint8).copy()
    if gray.size == 0:
        return None
    if page_mask is not None:
        gray[~page_mask] = int(gray.min())
    bw150 = binarize_wolf(gray, (51, 51), lower_bound=50)
    if page_mask is not None:
        bw150[~page_mask] = True

    hor_seed = open_brick(bw150, (200, 14), black_surroundings=True)
    ver_seed = open_brick(bw150, (14, 300), black_surroundings=True)
    dilated = dilate_brick(bw150, (3, 3))
    garbage = seed_fill(hor_seed | ver_seed, dilated) & bw150
    garbage = filter_shadows(garbage)
    content = bw150 & ~garbage
    despeckled = despeckle_module.despeckle(content, (WORK_DPI, WORK_DPI), "normal")

    height, width = content.shape
    blocks = np.ones_like(content)
    area_threshold = min(width, height)
    for quality, limit, vertical in (
        (prefer_horizontal, 80, False),
        (prefer_vertical, 40, True),
    ):
        finder = MaxWhitespaceFinder(despeckled, quality)
        for _ in range(limit):
            ws = finder.next(MANUAL_OBSTACLES)
            if ws is None or _width(ws) * _height(ws) < area_threshold:
                break
            _view(blocks, ws)[...] = False
            left, top, right, bottom = ws
            if vertical:
                fraction = _width(ws) // 5
                obstacle = (left + fraction, top, right - fraction, bottom)
            else:
                fraction = _height(ws) // 5
                obstacle = (left, top + fraction, right, bottom - fraction)
            finder.add_obstacle(obstacle)
    trim_content_blocks_in_place(despeckled, blocks)

    # More whitespace, to separate blocks that don't belong together.
    finder = MaxWhitespaceFinder(content | ~blocks, area_quality, (4, 4))
    for _ in range(10):
        ws = finder.next()
        if ws is None or _width(ws) * _height(ws) < area_threshold:
            break
        _view(blocks, ws)[...] = False
    trim_content_blocks_in_place(despeckled, blocks)
    remove_areas_touching_borders(blocks)

    text = estimate_text_mask(content, blocks) & content
    found = bounding_box(blocks)
    if found is None:
        return None
    content_rect: Rect = found
    hor_garbage, vert_garbage = segment_garbage(garbage)
    images = _Images(content=content, blocks=blocks, text=text)
    garbage_by_side = {
        "left": _Garbage(vert_garbage, vertical=True),
        "right": None,
        "top": _Garbage(hor_garbage, vertical=False),
        "bottom": None,
    }
    garbage_by_side["right"] = garbage_by_side["left"]
    garbage_by_side["bottom"] = garbage_by_side["top"]
    reopen = {
        "left": {"left", "top", "bottom"},
        "right": {"right", "top", "bottom"},
        "top": {"top", "left", "right"},
        "bottom": {"bottom", "left", "right"},
    }
    pending = {"left", "right", "top", "bottom"}
    while pending and not _empty(content_rect):
        for side in ("left", "right", "top", "bottom"):
            if side not in pending:
                continue
            pending.discard(side)
            side_garbage = garbage_by_side[side]
            assert side_garbage is not None
            old = content_rect
            content_rect = _trim_side(images, content_rect, side_garbage, side)
            if _empty(content_rect):
                break
            if old != content_rect:
                pending |= reopen[side]
        if _empty(content_rect):
            break
        if _width(content_rect) < 8 or _height(content_rect) < 8:
            return None
        if _width(content_rect) < 30 and _height(content_rect) > _width(content_rect) * 20:
            return None
    if _empty(content_rect):
        return None
    return content_rect


def to_working(gray: np.ndarray, dpi: tuple[float, float]) -> tuple[np.ndarray, float, float]:
    """Scale a greyscale page to 150 dpi; returns it with the x and y scale factors."""
    sx = WORK_DPI / dpi[0] if dpi[0] > 0 else 1.0
    sy = WORK_DPI / dpi[1] if dpi[1] > 0 else 1.0
    height, width = gray.shape
    target = (max(1, round(width * sx)), max(1, round(height * sy)))
    if target == (width, height):
        return np.asarray(gray), 1.0, 1.0
    interpolation = cv2.INTER_AREA if target[0] < width else cv2.INTER_LINEAR
    scaled = cv2.resize(np.asarray(gray, dtype=np.uint8), target, interpolation=interpolation)
    return np.asarray(scaled), target[0] / width, target[1] / height


__all__ = [
    "SEDM_INF",
    "WORK_DPI",
    "estimate_text_mask",
    "filter_shadows",
    "find_content_box",
    "remove_areas_touching_borders",
    "segment_garbage",
    "to_working",
    "trim_content_blocks_in_place",
]
