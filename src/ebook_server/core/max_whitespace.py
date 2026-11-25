# Derived from Scan Tailor imageproc/MaxWhitespaceFinder.cpp and
# imageproc/MaxWhitespaceFinder.h
# Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
# Copyright (C) c-base_book-scanner contributors
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# See the LICENSE file at the repository root.
"""Maximal whitespace rectangles: a port of upstream ``MaxWhitespaceFinder`` (#195 step 5).

Breuel's branch-and-bound search: regions are kept in a priority queue ordered by a
quality function of their size. The best region is split around an obstacle (a manual
obstacle, or the black box around a black pixel near its centre) into the parts above,
below, left and right of it; the first region without obstacles or black pixels is the
next maximal whitespace rectangle.

Rectangles are ``(left, top, right, bottom)`` with inclusive coordinates, like ``QRect``.
Divergence: upstream's ``std::priority_queue`` breaks ties between equally good regions
in heap order; here ties are broken by insertion order. Either way one of several equally
large rectangles is returned first.
"""

from __future__ import annotations

import heapq
from collections.abc import Callable
from itertools import count
from typing import Final

import cv2
import numpy as np

from .binary_image import Binary

type Rect = tuple[int, int, int, int]
type Quality = Callable[[int, int], int]

AUTO_OBSTACLES: Final[str] = "auto"
MANUAL_OBSTACLES: Final[str] = "manual"


def area_quality(width: int, height: int) -> int:
    """``AreaCompare``."""
    return width * height


def prefer_horizontal(width: int, height: int) -> int:
    """``PreferHorizontal`` (ContentBoxFinder)."""
    return width * width * height


def prefer_vertical(width: int, height: int) -> int:
    """``PreferVertical`` (ContentBoxFinder)."""
    return width * height * height


def _intersect(a: Rect, b: Rect) -> Rect | None:
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[2], b[2]), min(a[3], b[3])
    if right < left or bottom < top:
        return None
    return left, top, right, bottom


def _center(rect: Rect) -> tuple[int, int]:
    """``QRect::center``."""
    return (rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2


class _Region:
    __slots__ = ("bounds", "known_new_obstacles", "obstacles")

    def __init__(self, known_new_obstacles: int, bounds: Rect) -> None:
        self.known_new_obstacles = known_new_obstacles
        self.bounds = bounds
        self.obstacles: list[Rect] = []

    def add_obstacles(self, other: _Region) -> None:
        self._clip_into(other.obstacles)

    def add_new_obstacles(self, new_obstacles: list[Rect]) -> None:
        if self.known_new_obstacles < len(new_obstacles):
            self._clip_into(new_obstacles[self.known_new_obstacles :])

    def _clip_into(self, obstacles: list[Rect]) -> None:
        """Append the parts of ``obstacles`` inside the bounds (hot loop, inlined)."""
        b_left, b_top, b_right, b_bottom = self.bounds
        append = self.obstacles.append
        for o_left, o_top, o_right, o_bottom in obstacles:
            left = o_left if o_left > b_left else b_left
            right = o_right if o_right < b_right else b_right
            if right < left:
                continue
            top = o_top if o_top > b_top else b_top
            bottom = o_bottom if o_bottom < b_bottom else b_bottom
            if bottom < top:
                continue
            append((left, top, right, bottom))


class MaxWhitespaceFinder:
    def __init__(
        self,
        image: Binary,
        quality: Quality = area_quality,
        min_size: tuple[int, int] = (1, 1),
    ) -> None:
        black = np.asarray(image, dtype=bool)
        height, width = black.shape
        integral = cv2.integral(
            black.view(np.uint8) if black.flags.c_contiguous else black.astype(np.uint8)
        )
        self._item = integral.item
        self._quality = quality
        self._min_w, self._min_h = int(min_size[0]), int(min_size[1])
        self._queue: list[tuple[int, int, _Region]] = []
        self._order = count()
        self._new_obstacles: list[Rect] = []
        if width > 0 and height > 0:
            self._push(_Region(0, (0, 0, width - 1, height - 1)))

    # -- queue ------------------------------------------------------------------------

    def _push(self, region: _Region) -> None:
        left, top, right, bottom = region.bounds
        priority = self._quality(right - left + 1, bottom - top + 1)
        heapq.heappush(self._queue, (-priority, next(self._order), region))

    def _sum(self, rect: Rect) -> int:
        """Black pixels in an inclusive rectangle (``IntegralImage::sum``)."""
        left, top, right, bottom = rect
        item = self._item
        return int(
            item(bottom + 1, right + 1)
            - item(top, right + 1)
            - item(bottom + 1, left)
            + item(top, left)
        )

    # -- public API -------------------------------------------------------------------

    def add_obstacle(self, obstacle: Rect) -> None:
        if len(self._queue) == 1:
            self._queue[0][2].obstacles.append(obstacle)
        else:
            self._new_obstacles.append(obstacle)

    def next(self, obstacle_mode: str = AUTO_OBSTACLES, max_iterations: int = 1000) -> Rect | None:
        while max_iterations > 0 and self._queue:
            max_iterations -= 1
            region = heapq.heappop(self._queue)[2]
            region.add_new_obstacles(self._new_obstacles)
            if region.obstacles:
                self._subdivide(region, region.bounds, self._pivot_obstacle(region))
                continue
            if self._sum(region.bounds) != 0:
                pivot = self._black_pixel_close_to_center(region.bounds)
                self._subdivide(
                    region, region.bounds, self._extend_to_black_box(pivot, region.bounds)
                )
                continue
            if obstacle_mode == AUTO_OBSTACLES:
                self._new_obstacles.append(region.bounds)
            return region.bounds
        return None

    # -- internals --------------------------------------------------------------------

    def _subdivide(self, region: _Region, bounds: Rect, pivot: Rect) -> None:
        left, top, right, bottom = bounds
        p_left, p_top, p_right, p_bottom = pivot
        known = len(self._new_obstacles)
        parts: list[Rect] = []
        if p_top - top >= self._min_h:
            parts.append((left, top, right, p_top - 1))
        if bottom - p_bottom >= self._min_h:
            parts.append((left, p_bottom + 1, right, bottom))
        if p_left - left >= self._min_w:
            parts.append((left, top, p_left - 1, bottom))
        if right - p_right >= self._min_w:
            parts.append((p_right + 1, top, right, bottom))
        for part in parts:
            new_region = _Region(known, part)
            new_region.add_obstacles(region)
            self._push(new_region)

    @staticmethod
    def _pivot_obstacle(region: _Region) -> Rect:
        cx, cy = _center(region.bounds)
        best = region.obstacles[0]
        best_distance: int | None = None
        for obstacle in region.obstacles:
            ox, oy = _center(obstacle)
            distance = (cx - ox) ** 2 + (cy - oy) ** 2
            if best_distance is None or distance <= best_distance:
                best, best_distance = obstacle, distance
        return best

    def _black_pixel_close_to_center(self, rect: Rect) -> tuple[int, int]:
        """``findBlackPixelCloseToCenter``: ``rect`` contains at least one black pixel."""
        cx, cy = _center(rect)
        if self._sum((cx, cy, cx, cy)) != 0:
            return cx, cy
        outer = rect
        inner = (cx, cy, cx, cy)
        while True:
            if (outer[2] - outer[0]) - (inner[2] - inner[0]) <= 1 and (outer[3] - outer[1]) - (
                inner[3] - inner[1]
            ) <= 1:
                break
            middle = (
                outer[0] + ((inner[0] - outer[0] + 1) >> 1),
                outer[1] + ((inner[1] - outer[1] + 1) >> 1),
                outer[2] - ((outer[2] - inner[2]) >> 1),
                outer[3] - ((outer[3] - inner[3]) >> 1),
            )
            if self._sum(middle) == 0:
                inner = middle
            else:
                outer = middle
        o_left, o_top, o_right, o_bottom = outer
        if o_left != inner[0]:
            edge = (o_left, o_top, o_left, o_bottom)
            found = self._sum(edge)
            if o_bottom == o_top:  # a horizontal line: check its endpoints
                return (o_left, o_top) if found else (o_right, o_top)
            if found:
                return self._black_pixel_close_to_center(edge)
        if o_right != inner[2]:
            edge = (o_right, o_top, o_right, o_bottom)
            found = self._sum(edge)
            if o_bottom == o_top:
                return (o_right, o_top) if found else (o_left, o_top)
            if found:
                return self._black_pixel_close_to_center(edge)
        if o_top != inner[1]:
            edge = (o_left, o_top, o_right, o_top)
            found = self._sum(edge)
            if o_right == o_left:  # a vertical line
                return (o_left, o_top) if found else (o_left, o_bottom)
            if found:
                return self._black_pixel_close_to_center(edge)
        edge = (o_left, o_bottom, o_right, o_bottom)
        if o_right == o_left:
            return o_left, o_bottom
        return self._black_pixel_close_to_center(edge)

    def _extend_to_black_box(self, pixel: tuple[int, int], bounds: Rect) -> Rect:
        """``extendBlackPixelToBlackBox``: the largest all-black box around ``pixel``
        found by narrowing ``bounds`` towards it."""
        outer = bounds
        width, height = outer[2] - outer[0] + 1, outer[3] - outer[1] + 1
        if self._sum(outer) == width * height:
            return outer
        inner = (pixel[0], pixel[1], pixel[0], pixel[1])
        while True:
            if (outer[2] - outer[0]) - (inner[2] - inner[0]) <= 1 and (outer[3] - outer[1]) - (
                inner[3] - inner[1]
            ) <= 1:
                break
            middle = (
                outer[0] + ((inner[0] - outer[0] + 1) >> 1),
                outer[1] + ((inner[1] - outer[1] + 1) >> 1),
                outer[2] - ((outer[2] - inner[2]) >> 1),
                outer[3] - ((outer[3] - inner[3]) >> 1),
            )
            area = (middle[2] - middle[0] + 1) * (middle[3] - middle[1] + 1)
            if self._sum(middle) == area:
                inner = middle
            else:
                outer = middle
        return inner
