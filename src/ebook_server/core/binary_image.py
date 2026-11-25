# Derived from Scan Tailor imageproc/BinaryThreshold.cpp, imageproc/ReduceThreshold.cpp,
# imageproc/Morphology.cpp (openBrick), imageproc/SeedFill.cpp and
# imageproc/UpscaleIntegerTimes.cpp
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


def open_brick(image: Binary, brick: tuple[int, int]) -> Binary:
    """Morphological opening of the black pixels with a ``(width, height)`` brick.

    Upstream calls ``openBrick(image, brick, BLACK)``: the area outside the image counts
    as black during the erosion, so a dark band along an edge survives the opening.
    OpenCV's default border for ``erode`` (the maximum value) is that behaviour, and
    for ``dilate`` (the minimum) adds nothing from outside.
    """
    width, height = max(1, int(brick[0])), max(1, int(brick[1]))
    kernel = np.ones((height, width), dtype=np.uint8)
    opened = cv2.morphologyEx(image.astype(np.uint8), cv2.MORPH_OPEN, kernel)
    return np.asarray(opened, dtype=bool)


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
