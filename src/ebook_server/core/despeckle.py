# Derived from Scan Tailor Despeckle.cpp and Despeckle.h
# Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
# Copyright (C) c-base_book-scanner contributors
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# See the LICENSE file at the repository root.
"""Despeckling: a port of upstream ``Despeckle::despeckleInPlace`` (#195 step 5).

Upstream's idea:

- connected components (8-connected) whose bounding box reaches ``bigObjectThreshold``
  in width or height are kept; they are merged into one "big" component;
- every other component is kept when it is close enough to a kept component that is
  not much smaller than itself ("close" grows with the component's pixel count);
- "close" is measured between neighbouring cells of a Voronoi diagram of the
  components, where vertical distances count twice (``VERTICAL_SCALE``), so text
  lines hold together better than columns.

Divergences (also in ``docs/third-party/scantailor-port.md``):

- The Voronoi diagram is the exact anisotropic Euclidean one
  (``scipy.ndimage.distance_transform_edt`` with ``sampling=(2, 1)``) instead of
  upstream's 4-neighbour vector propagation, which approximates it. Territory
  boundaries can differ by a pixel.
- Upstream's second Voronoi pass ("give such components a second chance") is not
  ported. It marks every pixel of a component that is not "anchored to small but
  not big" with a sentinel distance that never propagates, so the connections it
  adds to such components get a distance of about 2^31 and can never attach; the
  only usable new connections are between components that themselves only reach
  small neighbours, which cannot make anything reachable from the big component
  that was not reachable before through the first pass's connections in practice.
  Leaving it out keeps the result of the first pass and saves a second Voronoi.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final, Literal

import cv2
import numpy as np

from .binary_image import Binary

type Level = Literal["cautious", "normal", "aggressive"]

VERTICAL_SCALE: Final[int] = 2


def _round_half_away(value: float) -> int:
    """``qRound``."""
    return int(np.floor(value + 0.5)) if value >= 0 else -int(np.floor(-value + 0.5))


@dataclass(frozen=True)
class Settings:
    min_relative_parent_weight: float
    #: Upstream stores it in a uint32_t: 6.5 * 6.5 becomes 42.
    pixels_to_sq_dist: int
    big_object_threshold: int


def settings_for(level: Level, dpi: tuple[float, float]) -> Settings:
    """``Settings::get``. Thresholds scale with the resolution (300 dpi reference)."""
    dpi_factor = int(min(dpi)) / 300.0
    if level == "cautious":
        return Settings(0.125 * dpi_factor, int(10.0 * 10.0), _round_half_away(7 * dpi_factor))
    if level == "aggressive":
        return Settings(0.225 * dpi_factor, int(3.5 * 3.5), _round_half_away(17 * dpi_factor))
    return Settings(0.175 * dpi_factor, int(6.5 * 6.5), _round_half_away(12 * dpi_factor))


def despeckle(image: Binary, dpi: tuple[float, float], level: Level = "normal") -> Binary:
    """Return ``image`` without the speckles ``level`` removes."""
    from scipy import ndimage
    from scipy.sparse import csr_matrix
    from scipy.sparse.csgraph import breadth_first_order

    settings = settings_for(level, dpi)
    big_threshold = settings.big_object_threshold
    pixels_to_sq_dist = settings.pixels_to_sq_dist
    black = np.asarray(image, dtype=bool)
    height, width = black.shape
    count, labels, stats, _ = cv2.connectedComponentsWithStats(
        black.astype(np.uint8), connectivity=8
    )
    if count <= 1:
        return black.copy()

    widths = stats[:, cv2.CC_STAT_WIDTH]
    heights = stats[:, cv2.CC_STAT_HEIGHT]
    areas = stats[:, cv2.CC_STAT_AREA].astype(np.int64)
    is_big = (widths >= big_threshold) | (heights >= big_threshold)
    is_big[0] = False
    small = np.flatnonzero(~is_big)
    small = small[small > 0]
    if not is_big.any():
        # Upstream starts from label 0 then, which nothing is attached to.
        return np.zeros_like(black)

    # Small components keep their own label; all big ones become one component.
    remap = np.zeros(count, dtype=np.int64)
    remap[small] = np.arange(1, small.size + 1)
    big_label = small.size + 1
    remap[is_big] = big_label
    pixels = np.zeros(big_label + 1, dtype=np.int64)
    pixels[1 : small.size + 1] = areas[small]
    # So that attaching to the big component is always allowed.
    pixels[big_label] = width * height
    component = remap[labels]

    # Voronoi territories with vertical distances counted twice.
    nearest = ndimage.distance_transform_edt(
        component == 0, sampling=(VERTICAL_SCALE, 1), return_distances=False, return_indices=True
    )
    near_y, near_x = nearest[0], nearest[1]
    territory = component[near_y, near_x]

    # Squared distances between the nearest pixels of neighbouring territories.
    lows: list[np.ndarray] = []
    highs: list[np.ndarray] = []
    dists: list[np.ndarray] = []
    for a, b in (
        (np.s_[:, :-1], np.s_[:, 1:]),
        (np.s_[:-1, :], np.s_[1:, :]),
    ):
        label_a, label_b = territory[a], territory[b]
        differs = label_a != label_b
        if not differs.any():
            continue
        dx = near_x[a][differs] - near_x[b][differs]
        dy = near_y[a][differs] - near_y[b][differs]
        la, lb = label_a[differs], label_b[differs]
        lows.append(np.minimum(la, lb))
        highs.append(np.maximum(la, lb))
        dists.append((dx * dx + dy * dy).astype(np.int64))
    retained = np.zeros(big_label + 1, dtype=bool)
    retained[big_label] = True
    if lows:
        low = np.concatenate(lows)
        high = np.concatenate(highs)
        dist = np.concatenate(dists)
        key = low * (big_label + 1) + high
        order = np.argsort(key, kind="stable")
        key, dist = key[order], dist[order]
        starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
        pair_key = key[starts]
        pair_dist = np.minimum.reduceat(dist, starts)
        first = pair_key // (big_label + 1)
        second = pair_key % (big_label + 1)

        def can_attach(comp: np.ndarray, target: np.ndarray) -> np.ndarray:
            """``canBeAttachedTo(comp, target)``."""
            near_enough = pair_dist <= pixels[comp] * pixels_to_sq_dist
            heavy_enough = pixels[target] >= pixels[comp] * settings.min_relative_parent_weight
            return np.asarray(near_enough & heavy_enough)

        # Edges target -> source: keeping the target keeps the source.
        forward = can_attach(first, second)
        backward = can_attach(second, first)
        targets = np.concatenate([second[forward], first[backward]])
        sources = np.concatenate([first[forward], second[backward]])
        graph = csr_matrix(
            (np.ones(targets.size, dtype=np.int8), (targets, sources)),
            shape=(big_label + 1, big_label + 1),
        )
        reached = breadth_first_order(graph, big_label, directed=True, return_predecessors=False)
        retained[reached] = True
    return np.asarray(black & retained[component])
