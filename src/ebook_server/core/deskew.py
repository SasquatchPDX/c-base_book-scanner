# Derived from Scan Tailor imageproc/SkewFinder.cpp, imageproc/SkewFinder.h,
# imageproc/Shear.cpp (vShearFromTo) and filters/deskew/Task.cpp
# Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
# Copyright (C) c-base_book-scanner contributors
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# See the LICENSE file at the repository root.
"""Automatic skew detection: a port of upstream ``SkewFinder`` (#195 step 4).

Upstream ``deskew::Task::process`` binarises the page with an Otsu threshold, removes
horizontal shadows (``Task::cleanup``), and runs ``SkewFinder::findSkew``:

1. the binary image is reduced (``ReduceThreshold``) for a coarse and a fine search;
2. the coarse search shears the reduced image vertically for every whole degree in
   ``[-max_angle, max_angle]`` and scores it by the sum of squared differences of
   black-pixel counts between neighbouring rows: text lines that run straight give
   sharp row-count jumps and a high score;
3. a binary search around the best coarse angle on the fine image narrows the bracket
   to the desired accuracy (0.1 degrees);
4. the confidence is the best score relative to the mean coarse score, minus one.
   Below :data:`GOOD_CONFIDENCE` upstream applies no rotation at all.

Angles here follow upstream's internal convention: a positive deskew angle rotates
the page **clockwise** on screen (the y axis points down). ``SkewFinder`` itself
reports the page's *skew*, which is the negative of the correcting rotation.

Divergences, with reasons (also in ``docs/third-party/scantailor-port.md``):

- Detection runs on the page's working image (about 150 dpi, #195 step 2) instead of the
  full-resolution original. The reductions are chosen from the working image's dpi so
  that the coarse search still runs at about 75 dpi and the fine search at about
  150 dpi, which is what upstream's defaults (2 and 1 reductions) give for a 300 dpi
  scan. The reduction that upstream performs from 300 to 150 dpi (``reduce(1)``) is
  replaced by the area-averaged working image followed by the Otsu threshold.
- The shear and score are computed from the coordinates of the black pixels with
  ``numpy.bincount`` rather than by shifting packed bit columns; the result is the
  same row histogram, including pixels that leave the image being dropped.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Final

import numpy as np

from .binary_image import (
    Binary,
    binarize,
    open_brick,
    otsu_threshold,
    reduce_threshold,
    seed_fill,
    upscale_integer_times,
)

DEFAULT_MAX_ANGLE: Final[float] = 7.0
DEFAULT_ACCURACY: Final[float] = 0.1
DEFAULT_COARSE_REDUCTION: Final[int] = 2
DEFAULT_FINE_REDUCTION: Final[int] = 1
#: ``Skew::GOOD_CONFIDENCE``: everything below means "do not rotate".
GOOD_CONFIDENCE: Final[float] = 2.0
#: ``SkewFinder::LOW_SCORE``: a best score at or below this has zero confidence.
LOW_SCORE: Final[float] = 1000.0
#: The resolution upstream's default reductions are tuned for.
REFERENCE_DPI: Final[float] = 300.0
#: Maximum angle of the manual controls (``deskew::OptionsWidget::MAX_ANGLE``).
MAX_MANUAL_ANGLE: Final[float] = 45.0


@dataclass(frozen=True)
class Skew:
    """``imageproc::Skew``: the page's skew in degrees (positive = clockwise skew)."""

    angle: float
    confidence: float

    @property
    def is_good(self) -> bool:
        return self.confidence >= GOOD_CONFIDENCE


@dataclass(frozen=True)
class Detection:
    """What auto mode found for one page."""

    #: The rotation to apply: positive = clockwise; 0 when the confidence is poor.
    angle: float
    #: The correcting rotation SkewFinder found, even when it is not applied.
    detected_angle: float
    confidence: float

    @property
    def applied(self) -> bool:
        return self.confidence >= GOOD_CONFIDENCE


def _shear_shifts(width: int, shear: float) -> np.ndarray:
    """Row shift of every column in ``vShearFromTo``: ``floor(0.5 + shear * (x + 0.5 - xc))``."""
    x_centre = 0.5 * width
    return np.floor(0.5 + shear * (np.arange(width) + 0.5 - x_centre)).astype(np.int64)


class _Scorer:
    """One reduced binary image, scored at any angle.

    ``score(angle)`` is ``vShearFromTo`` followed by ``SkewFinder::calcScore``: column
    ``x`` moves down by its shift, pixels that leave the image are lost (upstream fills
    the destination with white), and the score is the sum of squared differences of
    black-pixel counts between neighbouring rows.

    Columns that share a shift form contiguous blocks (the shift is monotonic in
    ``x``), so the sheared row histogram is the sum of each block's row histogram moved
    by the block's shift. Block histograms come from a running column sum, which makes
    one score cost O(rows x blocks) instead of O(black pixels).
    """

    def __init__(self, image: Binary, resolution_ratio: float) -> None:
        self.height, self.width = image.shape
        self.ratio = resolution_ratio
        prefix = np.zeros((self.height, self.width + 1), dtype=np.int32)
        np.cumsum(image, axis=1, dtype=np.int32, out=prefix[:, 1:])
        self.prefix = prefix
        self.rows = np.arange(self.height, dtype=np.int64)[:, None]

    def __call__(self, angle: float) -> float:
        shear = math.tan(math.radians(angle)) / self.ratio
        height, width = self.height, self.width
        if height < 2 or width < 1:
            return 0.0
        shifts = _shear_shifts(width, shear)
        if shifts[0] == shifts[-1]:
            # Upstream copies the image unchanged (the shift is then 0).
            counts = self.prefix[:, -1].astype(np.float64)
        else:
            starts = np.flatnonzero(np.diff(shifts, prepend=shifts[0] - 1))
            ends = np.append(starts[1:], width)
            block_shift = shifts[starts]
            blocks = self.prefix[:, ends] - self.prefix[:, starts]
            target = self.rows + block_shift[None, :]
            inside = (target >= 0) & (target < height)
            counts = np.bincount(target[inside], weights=blocks[inside], minlength=height).astype(
                np.float64
            )
        diff = np.diff(counts[:height])
        return float(np.dot(diff, diff))


def find_skew(
    image: Binary,
    *,
    max_angle: float = DEFAULT_MAX_ANGLE,
    accuracy: float = DEFAULT_ACCURACY,
    coarse_reduction: int = DEFAULT_COARSE_REDUCTION,
    fine_reduction: int = DEFAULT_FINE_REDUCTION,
    resolution_ratio: float = 1.0,
    reductions_done: int = 0,
) -> Skew:
    """``SkewFinder::findSkew``.

    ``reductions_done`` counts 2x reductions that already happened before ``image``
    (see the module docstring); upstream's first reduction uses threshold 1 and every
    later one threshold 2.
    """
    if not 0.0 <= max_angle <= 45.0:
        raise ValueError("max skew angle is invalid")
    if coarse_reduction < 0 or fine_reduction < 0:
        raise ValueError("reduction is invalid")
    if resolution_ratio <= 0.0:
        raise ValueError("resolution ratio is invalid")
    if image.size == 0:
        raise ValueError("empty image")

    def threshold(index: int) -> int:
        return 1 if index + reductions_done == 0 else 2

    coarse = image
    min_reduction = min(coarse_reduction, fine_reduction)
    for index in range(min_reduction):
        coarse = reduce_threshold(coarse, threshold(index))
    fine = coarse
    for index in range(min_reduction, coarse_reduction):
        coarse = reduce_threshold(coarse, threshold(index))

    coarse_score = _Scorer(coarse, resolution_ratio)
    coarse_step = 1.0
    num_coarse_scores = 0
    sum_coarse_scores = 0.0
    best_coarse_score = 0.0
    best_coarse_angle = -max_angle
    angle = -max_angle
    while angle <= max_angle:
        score = coarse_score(angle)
        sum_coarse_scores += score
        num_coarse_scores += 1
        if score > best_coarse_score:
            best_coarse_angle = angle
            best_coarse_score = score
        angle += coarse_step

    if accuracy >= coarse_step:
        confidence = 0.0
        if num_coarse_scores > 1 and sum_coarse_scores > 0.0:
            confidence = best_coarse_score / sum_coarse_scores * num_coarse_scores
        return Skew(-best_coarse_angle, confidence - 1.0)

    for index in range(min_reduction, fine_reduction):
        fine = reduce_threshold(fine, threshold(index))
    fine_score = coarse_score if fine is coarse else _Scorer(fine, resolution_ratio)

    angle_plus = best_coarse_angle + 0.5 * coarse_step
    angle_minus = best_coarse_angle - 0.5 * coarse_step
    score_plus = fine_score(angle_plus)
    score_minus = fine_score(angle_minus)
    fine_score1, fine_score2 = score_plus, score_minus
    tied = False
    while angle_plus - angle_minus > accuracy:
        if score_plus > score_minus:
            angle_minus = 0.5 * (angle_plus + angle_minus)
            score_minus = fine_score(angle_minus)
        elif score_plus < score_minus:
            angle_plus = 0.5 * (angle_plus + angle_minus)
            score_plus = fine_score(angle_plus)
        else:
            tied = True
            break  # protects against an unreasonably small accuracy

    if tied:
        # Divergence: upstream takes the lower end of the bracket here, which is half a
        # degree off for a perfectly straight page (both ends score the same by
        # symmetry). The middle of the bracket is the answer the search converges to.
        best_angle = 0.5 * (angle_plus + angle_minus)
        best_score = fine_score(best_angle)
    elif score_plus > score_minus:
        best_angle, best_score = angle_plus, score_plus
    else:
        best_angle, best_score = angle_minus, score_minus

    if best_score <= LOW_SCORE:
        return Skew(-best_angle, 0.0)

    if num_coarse_scores > 1:
        mean_scores, count = sum_coarse_scores, num_coarse_scores
    else:
        mean_scores = sum_coarse_scores + fine_score1 + fine_score2
        count = num_coarse_scores + 2
    confidence = best_score / mean_scores * count if mean_scores > 0.0 else 0.0
    return Skew(-best_angle, confidence - 1.0)


def _from_150dpi(size: int, dpi: float) -> int:
    """``deskew::Task::from150dpi``."""
    return max(1, int((size * dpi + 75) // 150))


def remove_horizontal_shadows(image: Binary, dpi: tuple[float, float]) -> Binary:
    """``deskew::Task::cleanup``: drop black regions that contain a long horizontal bar.

    Only horizontal shadows matter for skew detection (a dark band along the top or
    bottom edge would otherwise dominate the row score). The image is reduced to below
    200 dpi, opened with a brick of 200x14 pixels at 150 dpi, and every 8-connected
    region touching what survives the opening is removed from the full image.
    """
    reduced = image
    dpi_x, dpi_y = dpi
    first = True
    while dpi_x >= 200 and dpi_y >= 200:
        reduced = reduce_threshold(reduced, 1 if first else 2)
        first = False
        dpi_x, dpi_y = dpi_x / 2, dpi_y / 2
    brick = (_from_150dpi(200, dpi_x), _from_150dpi(14, dpi_y))
    opened = open_brick(reduced, brick, black_surroundings=True)
    if not opened.any():
        return image
    seed = upscale_integer_times(opened, (image.shape[1], image.shape[0]))
    garbage = seed_fill(seed, image)
    return image & ~garbage


def reductions_for(dpi: float) -> tuple[int, int, int]:
    """(coarse, fine, already done) reductions for an image at ``dpi``.

    Upstream's defaults (coarse 2, fine 1) are meant for 300 dpi. For another
    resolution the same target resolutions (75 and 150 dpi) are kept, counting the
    reductions the working image has effectively had already.
    """
    halvings = round(math.log2(max(1.0, REFERENCE_DPI / max(1.0, dpi)))) if dpi < 300 else 0
    extra = round(math.log2(dpi / REFERENCE_DPI)) if dpi > REFERENCE_DPI else 0
    fine = max(0, DEFAULT_FINE_REDUCTION - halvings + extra)
    coarse = max(0, DEFAULT_COARSE_REDUCTION - halvings + extra)
    return coarse, fine, min(halvings, DEFAULT_COARSE_REDUCTION)


def detect(gray: np.ndarray, dpi: tuple[float, float]) -> Detection:
    """Auto mode of upstream ``deskew::Task::process`` on a greyscale page."""
    gray = np.asarray(gray, dtype=np.uint8)
    binary = binarize(gray, otsu_threshold(gray))
    if not binary.any():
        return Detection(angle=0.0, detected_angle=0.0, confidence=0.0)
    binary = remove_horizontal_shadows(binary, dpi)
    if not binary.any():
        return Detection(angle=0.0, detected_angle=0.0, confidence=0.0)
    coarse, fine, done = reductions_for(min(dpi))
    skew = find_skew(
        binary,
        coarse_reduction=coarse,
        fine_reduction=fine,
        reductions_done=done,
        resolution_ratio=dpi[0] / dpi[1] if dpi[1] > 0 else 1.0,
    )
    correction = -skew.angle
    applied = correction if skew.is_good else 0.0
    return Detection(
        angle=round(applied, 3) + 0.0,
        detected_angle=round(correction, 3) + 0.0,
        confidence=round(skew.confidence, 3),
    )


def clamp_manual(angle: float) -> float:
    return max(-MAX_MANUAL_ANGLE, min(MAX_MANUAL_ANGLE, float(angle)))
