# Derived from Scan Tailor dewarping/CylindricalSurfaceDewarper.cpp,
# dewarping/DistortionModel.cpp, dewarping/Curve.cpp, dewarping/RasterDewarper.cpp,
# math/XSpline.cpp, math/ArcLengthMapper.cpp, math/PolylineIntersector.cpp,
# math/ToLineProjector.cpp, math/HomographicTransform.h and
# filters/output/OutputGenerator.cpp (dewarp, setupTrivialDistortionModel)
# Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
# Copyright (C) c-base_book-scanner contributors
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# See the LICENSE file at the repository root.
"""Cylindrical-surface dewarping: a port of upstream's distortion model (#195 step 6).

A page is modelled as a piece of a cylinder. Two curves (*directrices*) follow the top
and bottom of the text block. Their four end points are assumed to lie on one plane;
a homography maps that plane to the unit square (``pln`` coordinates). Straight lines
between the curves at equal plane x are the cylinder's *generatrices*. Along x, the
plane is unrolled by arc length, where the "elevation" of the surface comes from how
much the generatrix is shortened between the curves, scaled by the depth perception
(upstream's ``initArcLengthMapper``). Along each generatrix a 1-D homography through
three points (both curves and the line that stays straight) gives the y coordinate.

Coordinates are continuous (pixel ``i`` covers ``[i, i + 1)``). The raster dewarper
samples at pixel centres, as upstream's RasterDewarper does.

Divergences (also in ``docs/third-party/scantailor-port.md``):

- ``RasterDewarper`` area-maps each output pixel's source quadrilateral; here the source
  point of each output pixel centre is interpolated bilinearly (``cv2.remap``). Upstream
  has the bilinear variant too (``INTERP_BILLINEAR``); area mapping only matters where
  the dewarping shrinks the page strongly.
- ``PolylineIntersector`` and ``ArcLengthMapper`` keep a search hint between calls;
  the port searches every time (binary search, same branch rules), which gives the same
  result except on S-shaped curves where a line crosses the curve twice.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import cv2
import numpy as np

type Point = tuple[float, float]

DEFAULT_DEPTH_PERCEPTION: Final[float] = 2.0
MIN_DEPTH_PERCEPTION: Final[float] = 1.0
MAX_DEPTH_PERCEPTION: Final[float] = 3.0


class DewarpError(ValueError):
    """The distortion model cannot be used (upstream throws std::runtime_error)."""


# --- XSpline ---------------------------------------------------------------------------


def _g_blend(q: float, p: float, u: float) -> float:
    c1, c2, c3, c4, c5 = q, 2 * q, 10 - 12 * q - p, 2 * p + 14 * q - 15, 6 - 5 * q - p
    return c1 * u + c2 * u**2 + c3 * u**3 + c4 * u**4 + c5 * u**5


def _h_blend(q: float, u: float) -> float:
    return q * u + 2 * q * u**2 - 2 * q * u**4 - q * u**5


class XSpline:
    """``XSpline``: Blanc and Schlick's X-spline through control points with tensions.

    Tension is in [-1, 1]; upstream's editor gives the end points 0 and the others 1.
    """

    def __init__(self, points: Sequence[Point] = (), tensions: Sequence[float] = ()) -> None:
        self.points: list[Point] = [(float(x), float(y)) for x, y in points]
        if tensions:
            self.tensions = [float(value) for value in tensions]
        else:
            self.tensions = [
                0.0 if i in (0, len(self.points) - 1) else 1.0 for i in range(len(self.points))
            ]
        if len(self.tensions) != len(self.points):
            raise ValueError("one tension per control point")

    @classmethod
    def editor_default(cls, start: Point, end: Point) -> XSpline:
        """``DewarpingView::initNewSpline``: five points on the line, ends with tension 0."""
        points = [
            (start[0] + (end[0] - start[0]) * f, start[1] + (end[1] - start[1]) * f)
            for f in (0.0, 0.25, 0.5, 0.75, 1.0)
        ]
        return cls(points, [0.0, 1.0, 1.0, 1.0, 0.0])

    @property
    def num_segments(self) -> int:
        return max(0, len(self.points) - 1)

    def _point_in_segment(self, segment: int, t: float) -> Point:
        """``pointAtImpl`` / ``linearCombinationFor``."""
        last = len(self.points) - 1
        idxs = (max(0, segment - 1), segment, segment + 1, min(segment + 2, last))
        s1 = max(self.tensions[segment], 0.0)
        s2 = max(self.tensions[segment + 1], 0.0)
        t0, t1, t2, t3 = -1.0, 0.0, 1.0, 2.0
        big_t0p, big_t1p = t1 + s1, t2 + s2
        big_t2m, big_t3m = t1 - s1, t2 - s2
        q1 = -0.5 * min(self.tensions[segment], 0.0)
        q2 = -0.5 * min(self.tensions[segment + 1], 0.0)
        q = (q1, q2, q1, q2)
        p = (
            2.0 * (t0 - big_t0p) ** 2,
            2.0 * (t1 - big_t1p) ** 2,
            2.0 * (t2 - big_t2m) ** 2,
            2.0 * (t3 - big_t3m) ** 2,
        )
        u0 = (t - big_t0p) / (t0 - big_t0p)
        a0 = _g_blend(q[0], p[0], u0) if t <= big_t0p else _h_blend(q[0], u0)
        a1 = _g_blend(q[1], p[1], (t - big_t1p) / (t1 - big_t1p))
        a2 = _g_blend(q[2], p[2], (t - big_t2m) / (t2 - big_t2m))
        u3 = (t - big_t3m) / (t3 - big_t3m)
        a3 = _g_blend(q[3], p[3], u3) if t >= big_t3m else _h_blend(q[3], u3)
        total = a0 + a1 + a2 + a3
        weights = (a0 / total, a1 / total, a2 / total, a3 / total)
        x = sum(w * self.points[i][0] for w, i in zip(weights, idxs, strict=True))
        y = sum(w * self.points[i][1] for w, i in zip(weights, idxs, strict=True))
        return x, y

    def point_at(self, t: float) -> Point:
        n = self.num_segments
        if n <= 0:
            raise DewarpError("an X-spline needs two control points")
        if t >= 1.0:
            return self._point_in_segment(n - 1, 1.0)
        scaled = t * n
        segment = math.floor(scaled)
        return self._point_in_segment(int(segment), scaled - segment)

    def to_polyline(self, max_dist_from_spline: float = 0.2) -> list[Point]:
        """``toPolyline`` with upstream's default sampling (0.2 px from the spline)."""
        if not self.points:
            return []
        n = self.num_segments
        start, end = self.point_at(0.0), self.point_at(1.0)
        out = [start]
        if n == 0:
            return out
        max_sq = max_dist_from_spline * max_dist_from_spline

        def refine(
            prev_t: float, prev_pt: Point, next_t: float, next_pt: Point, depth: int
        ) -> None:
            dx, dy = next_pt[0] - prev_pt[0], next_pt[1] - prev_pt[1]
            if dx * dx + dy * dy < 1e-6 or depth > 40:
                return
            mid_t = 0.5 * (prev_t + next_t)
            junction = math.floor(mid_t * n + 0.5) / n
            is_junction = (junction - prev_t) * (next_t - prev_t) > 0 and (junction - next_t) * (
                prev_t - next_t
            ) > 0
            if is_junction:
                mid_t = junction
            mid = self.point_at(mid_t)
            if not is_junction:
                proj = _project_point((prev_pt, next_pt), mid)
                if (mid[0] - proj[0]) ** 2 + (mid[1] - proj[1]) ** 2 <= max_sq:
                    return
            refine(prev_t, prev_pt, mid_t, mid, depth + 1)
            out.append(mid)
            refine(mid_t, mid, next_t, next_pt, depth + 1)

        refine(0.0, start, 1.0, end, 0)
        out.append(end)
        return out


# --- small geometry ----------------------------------------------------------------------


def _projection_scalar(line: tuple[Point, Point], point: Point) -> float:
    """``ToLineProjector::projectionScalar``."""
    (ox, oy), (px, py) = line
    vx, vy = px - ox, py - oy
    norm = vx * vx + vy * vy
    if abs(norm) <= np.finfo(float).eps:
        return 0.0
    return ((point[0] - ox) * vx + (point[1] - oy) * vy) / norm


def _project_point(line: tuple[Point, Point], point: Point) -> Point:
    s = _projection_scalar(line, point)
    (ox, oy), (px, py) = line
    return ox + (px - ox) * s, oy + (py - oy) * s


def _line_intersection(a: tuple[Point, Point], b: tuple[Point, Point]) -> Point | None:
    """``QLineF::intersect`` of the infinite lines; None when parallel."""
    (x1, y1), (x2, y2) = a
    (x3, y3), (x4, y4) = b
    denom = (x1 - x2) * (y3 - y4) - (y1 - y2) * (x3 - x4)
    if denom == 0:
        return None
    det_a = x1 * y2 - y1 * x2
    det_b = x3 * y4 - y3 * x4
    return (
        (det_a * (x3 - x4) - (x1 - x2) * det_b) / denom,
        (det_a * (y3 - y4) - (y1 - y2) * det_b) / denom,
    )


class _Homography2D:
    def __init__(self, matrix: np.ndarray) -> None:
        self.matrix = matrix

    def __call__(self, point: Point) -> Point:
        x, y = point
        m = self.matrix
        w = m[2, 0] * x + m[2, 1] * y + m[2, 2]
        return (m[0, 0] * x + m[0, 1] * y + m[0, 2]) / w, (m[1, 0] * x + m[1, 1] * y + m[1, 2]) / w

    def inverse(self) -> _Homography2D:
        return _Homography2D(np.linalg.inv(self.matrix))


def _four_point_homography(pairs: Sequence[tuple[Point, Point]]) -> _Homography2D:
    """``fourPoint2DHomography``."""
    a = np.zeros((8, 8))
    b = np.zeros(8)
    for i, ((fx, fy), (tx, ty)) in enumerate(pairs):
        a[2 * i] = (-fx, -fy, -1, 0, 0, 0, tx * fx, tx * fy)
        b[2 * i] = -tx
        a[2 * i + 1] = (0, 0, 0, -fx, -fy, -1, ty * fx, ty * fy)
        b[2 * i + 1] = -ty
    try:
        h = np.linalg.solve(a, b)
    except np.linalg.LinAlgError as exc:
        raise DewarpError("degenerate distortion model") from exc
    return _Homography2D(np.append(h, 1.0).reshape(3, 3))


def _three_point_1d(pairs: Sequence[tuple[float, float]]) -> tuple[float, float, float]:
    """``threePoint1DHomography``: (a, b, c) with ``to = (a * from + b) / (c * from + 1)``."""
    a = np.array([[-f, -1.0, f * t] for f, t in pairs])
    b = np.array([-t for _, t in pairs])
    try:
        sol = np.linalg.solve(a, b)
    except np.linalg.LinAlgError as exc:
        raise DewarpError("degenerate generatrix") from exc
    return float(sol[0]), float(sol[1]), float(sol[2])


def _apply_1d(h: tuple[float, float, float], value: float | np.ndarray) -> float | np.ndarray:
    a, b, c = h
    return (value * a + b) / (value * c + 1.0)


class _PolylineIntersector:
    """``PolylineIntersector`` (without the hint; see the module docstring)."""

    def __init__(self, polyline: Sequence[Point]) -> None:
        self.points = np.asarray(polyline, dtype=np.float64)

    def intersect(self, line: tuple[Point, Point]) -> Point:
        (ox, oy), (px, py) = line
        # The line's normal: dot products give the signed side of each point.
        nx, ny = -(py - oy), px - ox
        dots = (self.points[:, 0] - ox) * nx + (self.points[:, 1] - oy) * ny
        front, back = dots[0], dots[-1]
        if front * back > 0:
            # The whole polyline is on one side: project the nearer end point.
            end = self.points[0] if abs(front) < abs(back) else self.points[-1]
            return _project_point(line, (float(end[0]), float(end[1])))
        left, right = 0, len(self.points) - 1
        left_dot = dots[left]
        while left + 1 < right:
            mid = (left + right) >> 1
            if dots[mid] * left_dot <= 0:
                right = mid
            else:
                left, left_dot = mid, dots[mid]
        seg = (
            (float(self.points[left][0]), float(self.points[left][1])),
            (float(self.points[left + 1][0]), float(self.points[left + 1][1])),
        )
        found = _line_intersection(line, seg)
        if found is None:
            return 0.5 * (seg[0][0] + seg[1][0]), 0.5 * (seg[0][1] + seg[1][1])
        return found


class _ArcLengthMapper:
    """``ArcLengthMapper``: plane x <-> arc length of the unrolled surface."""

    def __init__(self) -> None:
        self.xs: list[float] = []
        self.arcs: list[float] = []
        self._prev_fx = 0.0

    def add_sample(self, x: float, fx: float) -> None:
        arc = 0.0
        if self.xs:
            dx, dy = x - self.xs[-1], fx - self._prev_fx
            arc = self.arcs[-1] + math.hypot(dx, dy)
        self.xs.append(x)
        self.arcs.append(arc)
        self._prev_fx = fx

    def total(self) -> float:
        return self.arcs[-1] if len(self.arcs) >= 2 else 0.0

    def normalize(self, total: float) -> None:
        if len(self.arcs) <= 1 or self.arcs[-1] == 0:
            return
        scale = total / self.arcs[-1]
        self.arcs = [arc * scale for arc in self.arcs]

    @staticmethod
    def _interp(value: float, src: list[float], dst: list[float]) -> float:
        if not src:
            return 0.0
        if len(src) == 1:
            return dst[0]
        if value < src[0]:
            segment = 0
        elif value > src[-1]:
            segment = len(src) - 2
        else:
            left, right = 0, len(src) - 1
            while left + 1 < right:
                mid = (left + right) >> 1
                if (value - src[mid]) * (value - src[left]) <= 0:
                    right = mid
                else:
                    left = mid
            segment = left
        s0, s1 = src[segment], src[segment + 1]
        d0, d1 = dst[segment], dst[segment + 1]
        return d0 + (value - s0) * (d1 - d0) / (s1 - s0)

    def arc_to_x(self, arc: float) -> float:
        return self._interp(arc, self.arcs, self.xs)

    def x_to_arc(self, x: float) -> float:
        return self._interp(x, self.xs, self.arcs)


@dataclass(frozen=True)
class Generatrix:
    p1: Point
    p2: Point
    pln2img: tuple[float, float, float]

    def point(self, crv_y: float) -> Point:
        t = float(_apply_1d(self.pln2img, crv_y))
        return self.p1[0] + (self.p2[0] - self.p1[0]) * t, self.p1[1] + (
            self.p2[1] - self.p1[1]
        ) * t


class CylindricalSurfaceDewarper:
    """``CylindricalSurfaceDewarper`` for a top and a bottom polyline (image coordinates)."""

    def __init__(
        self, top: Sequence[Point], bottom: Sequence[Point], depth_perception: float
    ) -> None:
        if len(top) < 2 or len(bottom) < 2:
            raise DewarpError("each curve needs two points")
        self.top = [(float(x), float(y)) for x, y in top]
        self.bottom = [(float(x), float(y)) for x, y in bottom]
        self.pln2img = _four_point_homography(
            [
                ((0.0, 0.0), self.top[0]),
                ((1.0, 0.0), self.top[-1]),
                ((0.0, 1.0), self.bottom[0]),
                ((1.0, 1.0), self.bottom[-1]),
            ]
        )
        self.img2pln = self.pln2img.inverse()
        self.depth_perception = depth_perception
        self.straight_line_y = self._straight_line_y()
        self._top_intersector = _PolylineIntersector(self.top)
        self._bottom_intersector = _PolylineIntersector(self.bottom)
        self.arc = _ArcLengthMapper()
        self.directrix_arc_length = 1.0
        self._init_arc_length_mapper()

    # -- coupled iteration over both polylines ----------------------------------------

    def _coupled(self) -> list[tuple[Point, Point, float]]:
        """``CoupledPolylinesIterator``: matching points on both curves, by plane x."""
        seq1, seq2 = self.top, self.bottom
        i1 = i2 = 0
        prev1 = next1 = seq1[0]
        prev2 = next2 = seq2[0]
        next_x1 = next_x2 = 0.0
        out: list[tuple[Point, Point, float]] = []

        def advance1() -> None:
            nonlocal i1, prev1, next1, next_x1
            i1 += 1
            if i1 == len(seq1):
                return
            prev1, next1 = next1, seq1[i1]
            next_x1 = self.img2pln(next1)[0]

        def advance2() -> None:
            nonlocal i2, prev2, next2, next_x2
            i2 += 1
            if i2 == len(seq2):
                return
            prev2, next2 = next2, seq2[i2]
            next_x2 = self.img2pln(next2)[0]

        while True:
            if next_x1 < next_x2 and i1 != len(seq1):
                pln = self.img2pln(next1)
                img1 = next1
                other = self.pln2img((pln[0], pln[1] + 1))
                img2 = _line_intersection((img1, other), (next2, prev2)) or next2
                out.append((img1, img2, pln[0]))
                advance1()
                if i2 != len(seq2) and (next2[0] - img2[0]) ** 2 + (next2[1] - img2[1]) ** 2 < 1:
                    advance2()
            elif i2 != len(seq2):
                pln = self.img2pln(next2)
                img2 = next2
                other = self.pln2img((pln[0], pln[1] + 1))
                img1 = _line_intersection((img2, other), (next1, prev1)) or next1
                out.append((img1, img2, pln[0]))
                advance2()
                if i1 != len(seq1) and (next1[0] - img1[0]) ** 2 + (next1[1] - img1[1]) ** 2 < 1:
                    advance1()
            else:
                return out

    def _straight_line_y(self) -> float:
        """``calcPlnStraightLineY``: the plane y of the line that stays straight."""
        accum = weight_sum = 0.0
        for img1, img2, pln_x in self._coupled():
            generatrix = (img1, img2)
            p2 = _projection_scalar(generatrix, self.pln2img((pln_x, 0.0)))
            p3 = _projection_scalar(generatrix, self.pln2img((pln_x, 1.0)))
            dp1, dp2 = p2 - 0.0, 1.0 - p3
            weight = abs(dp1 + dp2)
            if weight < 0.01:
                continue
            p0 = (p3 * dp1 + p2 * dp2) / (dp1 + dp2)
            point = (img1[0] + (img2[0] - img1[0]) * p0, img1[1] + (img2[1] - img1[1]) * p0)
            accum += self.img2pln(point)[1] * weight
            weight_sum += weight
        return 0.5 if weight_sum == 0 else accum / weight_sum

    def _init_arc_length_mapper(self) -> None:
        prev_x = -math.inf
        for img1, img2, pln_x in self._coupled():
            if pln_x <= prev_x:
                continue  # an S-shaped surface: not supported, as upstream
            generatrix = (img1, img2)
            y1 = _projection_scalar(generatrix, self.pln2img((pln_x, 0.0)))
            y2 = _projection_scalar(generatrix, self.pln2img((pln_x, 1.0)))
            elevation = self.depth_perception * (1.0 - (y2 - y1))
            elevation = min(0.5, max(-0.5, elevation))
            self.arc.add_sample(pln_x, elevation)
            prev_x = pln_x
        self.directrix_arc_length = self.arc.total()
        self.arc.normalize(1.0)

    # -- mapping ------------------------------------------------------------------------

    def _three_pairs(
        self, top_proj: float, bottom_proj: float, straight_proj: float
    ) -> list[tuple[float, float]]:
        y = self.straight_line_y
        if abs(y) < 0.05 or abs(y - 1.0) < 0.05:
            return [(0.0, top_proj), (1.0, bottom_proj), (0.5, 0.5 * (top_proj + bottom_proj))]
        return [(0.0, top_proj), (1.0, bottom_proj), (y, straight_proj)]

    def _projections(self, pln_x: float) -> tuple[tuple[Point, Point], float, float, float]:
        line = (self.pln2img((pln_x, 0.0)), self.pln2img((pln_x, 1.0)))
        top_pt = self._top_intersector.intersect(line)
        bottom_pt = self._bottom_intersector.intersect(line)
        straight = self.pln2img((pln_x, self.straight_line_y))
        return (
            line,
            _projection_scalar(line, top_pt),
            _projection_scalar(line, bottom_pt),
            _projection_scalar(line, straight),
        )

    def map_generatrix(self, crv_x: float) -> Generatrix:
        pln_x = self.arc.arc_to_x(crv_x)
        line, top, bottom, straight = self._projections(pln_x)
        return Generatrix(
            line[0], line[1], _three_point_1d(self._three_pairs(top, bottom, straight))
        )

    def map_to_dewarped(self, img_pt: Point) -> Point:
        pln_x = self.img2pln(img_pt)[0]
        crv_x = self.arc.x_to_arc(pln_x)
        line, top, bottom, straight = self._projections(pln_x)
        pairs = [(to, frm) for frm, to in self._three_pairs(top, bottom, straight)]
        h = _three_point_1d(pairs)
        return crv_x, float(_apply_1d(h, _projection_scalar(line, img_pt)))

    def map_to_warped(self, crv_pt: Point) -> Point:
        return self.map_generatrix(crv_pt[0]).point(crv_pt[1])


# --- distortion model --------------------------------------------------------------------


def model_is_valid(top: Sequence[Point], bottom: Sequence[Point]) -> bool:
    """``DistortionModel::isValid``: both curves valid, the corner quad convex."""
    for curve in (top, bottom):
        if len(curve) < 2 or tuple(curve[0]) == tuple(curve[-1]):
            return False
    poly = [top[0], top[-1], bottom[-1], bottom[0]]
    dots = []
    for i in range(4):
        cur, prev, nxt = poly[i], poly[(i + 3) & 3], poly[(i + 1) & 3]
        normal = (-(cur[1] - prev[1]), cur[0] - prev[0])
        dots.append(normal[0] * (nxt[0] - cur[0]) + normal[1] * (nxt[1] - cur[1]))
    low, high = min(dots), max(dots)
    if low * high <= 0:
        return False  # not convex
    return not (abs(low) < 0.01 or abs(high) < 0.01)


def model_domain(
    top: Sequence[Point],
    bottom: Sequence[Point],
    dewarper: CylindricalSurfaceDewarper,
    content_rect: tuple[float, float, float, float],
) -> tuple[float, float, float, float]:
    """``DistortionModel::modelDomain`` (identity ``to_output``): the curves' bounding
    box, shrunk vertically by the directrix arc length around the content centre.
    Returns (left, top, right, bottom)."""
    points = list(top) + list(bottom)
    left = min(p[0] for p in points)
    right = max(p[0] for p in points)
    top_y = min(p[1] for p in points)
    bottom_y = max(p[1] for p in points)
    if dewarper.directrix_arc_length <= 0:
        raise DewarpError("zero-length directrix")
    scale = 1.0 / dewarper.directrix_arc_length
    origin_y = 0.5 * (content_rect[1] + content_rect[3])
    new_top = origin_y - (origin_y - top_y) * scale
    new_height = (bottom_y - top_y) * scale
    return left, new_top, right, new_top + new_height


def trivial_curves(width: float, height: float) -> tuple[list[Point], list[Point]]:
    """``setupTrivialDistortionModel`` for a content box covering the whole input."""
    return [(0.0, 0.0), (width, 0.0)], [(0.0, height), (width, height)]


def dewarp_image(
    image: np.ndarray,
    out_size: tuple[int, int],
    dewarper: CylindricalSurfaceDewarper,
    domain: tuple[float, float, float, float],
    background: int | tuple[int, ...],
) -> np.ndarray:
    """``RasterDewarper::dewarp``: sample the warped image for every output pixel."""
    out_w, out_h = out_size
    left, top, right, bottom = domain
    if right <= left or bottom <= top:
        raise DewarpError("empty model domain")
    model_y = (np.arange(out_h, dtype=np.float64) + 0.5 - top) / (bottom - top)
    map_x = np.empty((out_h, out_w), dtype=np.float32)
    map_y = np.empty((out_h, out_w), dtype=np.float32)
    for dst_x in range(out_w):
        g = dewarper.map_generatrix((dst_x + 0.5 - left) / (right - left))
        t = _apply_1d(g.pln2img, model_y)
        map_x[:, dst_x] = g.p1[0] + (g.p2[0] - g.p1[0]) * t - 0.5
        map_y[:, dst_x] = g.p1[1] + (g.p2[1] - g.p1[1]) * t - 0.5
    border = background if isinstance(background, tuple) else (background,) * 4
    return np.asarray(
        cv2.remap(
            image,
            map_x,
            map_y,
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=border,
        )
    )
