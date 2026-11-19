# Derived from Scan Tailor imageproc/BinaryThreshold.cpp, imageproc/ReduceThreshold.cpp,
# imageproc/Morphology.cpp (openBrick, dilateBrick), imageproc/SeedFill.cpp,
# imageproc/UpscaleIntegerTimes.cpp, imageproc/Binarize.cpp (binarizeWolf) and
# imageproc/SEDM.cpp
# Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
# Copyright (C) c-base_book-scanner contributors
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# See the LICENSE file at the repository root.
"""Binary-image primitives of upstream ``imageproc``, on NumPy arrays (#195 step 4).

A binary image is a 2-D ``bool`` array, ``True`` meaning black (ink), as in upstream
``BinaryImage``. Upstream packs 32 pixels per word and works word by word; here the same
operations are vectorised NumPy/OpenCV calls with the same results.
"""

from __future__ import annotations

import cv2
import numpy as np

type Binary = np.ndarray


def otsu_threshold(gray: np.ndarray) -> int:
    """Upstream ``BinaryThreshold::otsuThreshold``: pixels *below* the result are black.

    Ties between equally good thresholds resolve to the middle one, and the result is
    shifted by one because the search counts ``<=`` as black while ``BinaryThreshold``
    uses ``<``.
    """
    pixels = np.ascontiguousarray(gray, dtype=np.uint8)
    histogram = cv2.calcHist([pixels], [0], None, [256], [0, 256]).ravel().astype(np.int64)
    pixels_below = np.cumsum(histogram)
    moment_below = np.cumsum(histogram * np.arange(256, dtype=np.int64))
    total_pixels = int(pixels_below[-1])
    total_moment = int(moment_below[-1])
    pixels_above = total_pixels - pixels_below
    valid = (pixels_below > 0) & (pixels_above > 0)
    if not valid.any():
        # Upstream returns (-1 + 1 + -1 + 1) >> 1 = 0: nothing is black.
        return 0
    below = np.where(valid, pixels_below, 1).astype(np.float64)
    above = np.where(valid, pixels_above, 1).astype(np.float64)
    mean_diff = moment_below / below - (total_moment - moment_below) / above
    variance = np.where(valid, mean_diff * mean_diff * below * above, 0.0)
    best = float(variance.max())
    if best <= 0.0:
        return 0
    hits = np.flatnonzero(variance == best)
    first, last = int(hits[0]) + 1, int(hits[-1]) + 1
    return (first + last) >> 1


def binarize(gray: np.ndarray, threshold: int) -> Binary:
    """``BinaryImage(gray, threshold)``: a pixel is black when ``gray < threshold``."""
    return np.asarray(gray) < threshold


def reduce_threshold(image: Binary, threshold: int) -> Binary:
    """One upstream ``ReduceThreshold::reduce(threshold)`` step: 2x2 blocks to one pixel.

    The output pixel is black when at least ``threshold`` (1..4) of its four source
    pixels are black. An odd last row or column is dropped; a one-pixel-wide or -high
    image is reduced along its long side only, and 1x1 stays as it is.
    """
    if not 1 <= threshold <= 4:
        raise ValueError("reduce_threshold: threshold must be 1..4")
    height, width = image.shape
    if height == 1 and width == 1:
        return image.copy()
    if height == 1 or width == 1:
        line = image.ravel()
        half = line.size // 2
        pairs = line[: 2 * half].reshape(half, 2).sum(axis=1)
        # A line holds two source pixels per output pixel: thresholds 1-2 need one of
        # them, 3-4 need both (upstream reduceHorLine / reduceVertLine).
        reduced = pairs >= (1 if threshold <= 2 else 2)
        return reduced.reshape(1, half) if height == 1 else reduced.reshape(half, 1)
    h2, w2 = height // 2, width // 2
    pixels = image.view(np.uint8) if image.dtype == np.bool_ else image.astype(np.uint8)
    total = pixels[0 : 2 * h2 : 2, 0 : 2 * w2 : 2] + pixels[1 : 2 * h2 : 2, 0 : 2 * w2 : 2]
    total += pixels[0 : 2 * h2 : 2, 1 : 2 * w2 : 2]
    total += pixels[1 : 2 * h2 : 2, 1 : 2 * w2 : 2]
    return np.asarray(total >= threshold)


def _morph(image: Binary, op: int, brick: tuple[int, int], *, black_surroundings: bool) -> Binary:
    """Apply an OpenCV morphology op as if the image continued with its surroundings.

    With black surroundings upstream erodes into an area extended by the brick
    (``extendByBrick``) and dilates back from there; with white ones the area outside
    stays white. Both are the same as padding the image with the surroundings colour by
    the brick size, processing it, and cropping.
    """
    width, height = max(1, int(brick[0])), max(1, int(brick[1]))
    value = 1 if black_surroundings else 0
    padded = cv2.copyMakeBorder(
        image.astype(np.uint8), height, height, width, width, cv2.BORDER_CONSTANT, value=value
    )
    kernel = np.ones((height, width), dtype=np.uint8)
    result = cv2.morphologyEx(padded, op, kernel, borderType=cv2.BORDER_CONSTANT, borderValue=value)
    return np.asarray(result[height:-height, width:-width], dtype=bool)


def open_brick(
    image: Binary, brick: tuple[int, int], *, black_surroundings: bool = False
) -> Binary:
    """``openBrick(image, brick, surroundings)``: opening of the black pixels with a
    ``(width, height)`` brick. Upstream's default surroundings are white; with black
    surroundings a dark band along an edge survives the opening."""
    return _morph(image, cv2.MORPH_OPEN, brick, black_surroundings=black_surroundings)


def dilate_brick(
    image: Binary, brick: tuple[int, int], *, black_surroundings: bool = False
) -> Binary:
    """``dilateBrick(image, brick, surroundings)`` (default: white surroundings)."""
    return _morph(image, cv2.MORPH_DILATE, brick, black_surroundings=black_surroundings)


def upscale_integer_times(image: Binary, size: tuple[int, int]) -> Binary:
    """``upscaleIntegerTimes(src, size, WHITE)``: repeat pixels, pad with white."""
    width, height = size
    if image.shape == (height, width):
        return image
    factor_x = max(1, width // max(1, image.shape[1]))
    factor_y = max(1, height // max(1, image.shape[0]))
    scaled = np.repeat(np.repeat(image, factor_y, axis=0), factor_x, axis=1)
    result = np.zeros((height, width), dtype=bool)
    rows, cols = min(height, scaled.shape[0]), min(width, scaled.shape[1])
    result[:rows, :cols] = scaled[:rows, :cols]
    return result


def seed_fill(seed: Binary, mask: Binary) -> Binary:
    """``seedFill(seed, mask, CONN8)``: every 8-connected black region of ``mask``
    that contains a black ``seed`` pixel."""
    count, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=8)
    if count <= 1:
        return np.zeros_like(mask, dtype=bool)
    hit = np.zeros(count, dtype=bool)
    hit[np.unique(labels[seed & mask])] = True
    hit[0] = False
    return hit[labels]


def seed_fill4(seed: Binary, mask: Binary) -> Binary:
    """``seedFill(seed, mask, CONN4)``."""
    count, labels = cv2.connectedComponents(mask.astype(np.uint8), connectivity=4)
    if count <= 1:
        return np.zeros_like(mask, dtype=bool)
    hit = np.zeros(count, dtype=bool)
    hit[np.unique(labels[seed & mask])] = True
    hit[0] = False
    return hit[labels]


def binarize_wolf(
    gray: np.ndarray,
    window: tuple[int, int],
    lower_bound: int = 1,
    upper_bound: int = 254,
) -> Binary:
    """``binarizeWolf``: Wolf/Jolion local thresholding with ``k = 0.3``.

    The window is clipped at the image edges, as the upstream integral-image sums are.
    A pixel is black when it is below ``lower_bound``, or not above ``upper_bound`` and
    below ``mean - k * (1 - deviation / max_deviation) * (mean - min_gray)``.
    """
    pixels = np.asarray(gray, dtype=np.uint8)
    win_w, win_h = int(window[0]), int(window[1])
    if win_w <= 0 or win_h <= 0:
        raise ValueError("binarize_wolf: invalid window")
    values = pixels.astype(np.float64)
    lower_half_h = win_h >> 1
    left_half_w = win_w >> 1
    anchor = (left_half_w, lower_half_h)

    def window_sum(plane: np.ndarray) -> np.ndarray:
        # Sum over [x - left_half, x + right_half) x [y - lower_half, y + upper_half),
        # clipped at the image edges (zero outside), as upstream's integral image does.
        return np.asarray(
            cv2.boxFilter(
                plane,
                cv2.CV_64F,
                (win_w, win_h),
                anchor=anchor,
                normalize=False,
                borderType=cv2.BORDER_CONSTANT,
            )
        )

    area = window_sum(np.ones_like(values))
    mean = window_sum(values) / area
    sq_mean = window_sum(values * values) / area
    deviation = np.sqrt(np.abs(sq_mean - mean * mean))
    # Upstream stores means and deviations as float.
    mean = mean.astype(np.float32).astype(np.float64)
    deviation = deviation.astype(np.float32).astype(np.float64)
    max_deviation = float(deviation.max()) if deviation.size else 0.0
    min_gray = float(pixels.min()) if pixels.size else 0.0
    if max_deviation > 0:
        weight = 1.0 - deviation / max_deviation
    else:
        weight = np.full_like(deviation, np.nan)  # upstream divides by zero
    threshold = mean - 0.3 * weight * (mean - min_gray)
    with np.errstate(invalid="ignore"):
        below = values < threshold
    return np.asarray((pixels < lower_bound) | ((pixels <= upper_bound) & below))


#: ``SEDM::INF_DIST``: the squared distance of a pixel with no black pixel to reach.
SEDM_INF: float = float(2**32 - 2)


def sedm(
    image: Binary,
    *,
    left_right_borders: bool = False,
    top_bottom_borders: bool = False,
) -> np.ndarray:
    """``SEDM(image, DIST_TO_BLACK, borders)``: squared Euclidean distance of every pixel
    to the nearest black pixel. The selected image borders count as black lines one
    pixel outside the image. Without any black pixel the distance is ``SEDM_INF``.
    """
    black = np.asarray(image, dtype=bool)
    pad_x = 1 if left_right_borders else 0
    pad_y = 1 if top_bottom_borders else 0
    if not (pad_x or pad_y):
        if not black.any():
            return np.full(black.shape, SEDM_INF)
        exact = cv2.distanceTransform(
            np.ascontiguousarray(~black).view(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE
        )
        return np.asarray(np.rint(np.square(exact)))
    padded = np.zeros((black.shape[0] + 2 * pad_y, black.shape[1] + 2 * pad_x), dtype=bool)
    padded[pad_y : pad_y + black.shape[0], pad_x : pad_x + black.shape[1]] = black
    if pad_x:
        padded[:, 0] = True
        padded[:, -1] = True
    if pad_y:
        padded[0, :] = True
        padded[-1, :] = True
    if not padded.any():
        return np.full(black.shape, SEDM_INF)
    # Exact Euclidean distances (OpenCV's precise mask is the exact transform) to the
    # nearest zero pixel, i.e. to the nearest black one.
    distances = cv2.distanceTransform((~padded).view(np.uint8), cv2.DIST_L2, cv2.DIST_MASK_PRECISE)
    if pad_x or pad_y:
        distances = distances[pad_y : pad_y + black.shape[0], pad_x : pad_x + black.shape[1]]
    # Squared distances are integers; float32 holds them exactly up to 2**24.
    return np.asarray(np.rint(np.square(distances)))


def _max3x3(values: np.ndarray) -> np.ndarray:
    return np.asarray(cv2.dilate(values, np.ones((3, 3), np.uint8)))


def sedm_peaks(distances: np.ndarray, padding: float = SEDM_INF) -> Binary:
    """``SEDM::findPeaksDestructive``: cells whose 8-connected plateau has only lower
    neighbours. The one-cell padding around the map holds ``padding``, the value
    upstream initialises its padding with."""
    height, width = distances.shape
    padded = np.full((height + 2, width + 2), padding, dtype=np.float64)
    padded[1:-1, 1:-1] = distances
    candidates = np.zeros(padded.shape, dtype=bool)
    candidates[1:-1, 1:-1] = (padded == _max3x3(padded))[1:-1, 1:-1]
    neighbourhood = _max3x3(candidates.astype(np.uint8)).astype(bool) & ~candidates
    bumped = padded + neighbourhood
    candidates2 = np.zeros(padded.shape, dtype=bool)
    candidates2[1:-1, 1:-1] = (bumped == _max3x3(bumped))[1:-1, 1:-1]
    diff = (candidates2 ^ candidates)[1:-1, 1:-1]
    inner = candidates[1:-1, 1:-1]
    not_peaks = seed_fill(diff, inner)
    return np.asarray(inner & ~not_peaks)


def bounding_box(image: Binary) -> tuple[int, int, int, int] | None:
    """``BinaryImage::contentBoundingBox``: (left, top, right, bottom), inclusive."""
    rows = np.flatnonzero(image.any(axis=1))
    if rows.size == 0:
        return None
    cols = np.flatnonzero(image.any(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])
