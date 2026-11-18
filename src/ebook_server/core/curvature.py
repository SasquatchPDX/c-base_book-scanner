"""Opt-in vertical baseline flattening; not a general 3D page reconstruction (#171).

OpenCV detects ink bands on a reduced page. NumPy fits quadratic baseline curves;
the renderer interpolates their vertical displacement and remaps bounded tiles.
Perspective correction remains a separate, default model.
"""

from __future__ import annotations

from math import ceil
from typing import Any, Final

import cv2
import numpy as np
from PIL import Image

VERSION: Final = "baseline-curvature-v1"
DETECTION_EDGE: Final = 1200
TILE_EDGE: Final = 512
MAX_CURVES: Final = 32
MAX_POINTS: Final = 17
MAX_DISPLACEMENT: Final = 512


def normalize_curves(value: Any, size: tuple[int, int]) -> list[list[list[float]]]:
    width, height = size
    if not isinstance(value, list) or not 2 <= len(value) <= MAX_CURVES:
        raise ValueError("curvature needs 2–32 non-crossing baselines")
    curves: list[list[list[float]]] = []
    for curve in value:
        if not isinstance(curve, list) or not 3 <= len(curve) <= MAX_POINTS:
            raise ValueError("each baseline needs 3–17 points")
        points: Any = np.asarray(curve, dtype=np.float64)
        if points.shape != (len(curve), 2) or not np.isfinite(points).all():
            raise ValueError("baseline points must be finite x/y pairs")
        if (
            (points[:, 0] < 0).any()
            or (points[:, 0] > width - 1).any()
            or (points[:, 1] < 0).any()
            or (points[:, 1] > height - 1).any()
        ):
            raise ValueError("baseline points must be inside the input image")
        if (np.diff(points[:, 0]) <= 0).any():
            raise ValueError("baseline x coordinates must increase")
        curves.append(points.tolist())
    curves.sort(key=lambda curve: float(np.median(np.asarray(curve)[:, 1])))
    knots = sorted({point[0] for curve in curves for point in curve} | {0.0, float(width - 1)})
    profiles = np.asarray(
        [np.interp(knots, np.asarray(c)[:, 0], np.asarray(c)[:, 1]) for c in curves]
    )
    if (np.diff(profiles, axis=0) < 2).any():
        raise ValueError("baselines cross or are too close together")
    sample_x = np.linspace(0, width - 1, 65)
    anchors = np.median(
        np.asarray([np.interp(sample_x, np.asarray(c)[:, 0], np.asarray(c)[:, 1]) for c in curves]),
        axis=1,
    )
    displacement = float(np.max(np.abs(profiles - anchors[:, None])))
    if displacement > min(MAX_DISPLACEMENT, height * 0.15):
        raise ValueError("curvature displacement exceeds the safe rendering limit")
    return curves


def detect_curves(source: Image.Image) -> list[list[list[float]]]:
    scale = min(1.0, DETECTION_EDGE / max(source.size))
    reduced = source.resize(
        (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    )
    gray = np.asarray(reduced.convert("L"))
    height, width = gray.shape
    _, ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    xs = np.linspace(width * 0.08, width * 0.92, 25)
    radius = max(5, width // 65)
    bands: list[list[float]] = []
    for x in xs:
        stripe = ink[:, max(0, int(x) - radius) : min(width, int(x) + radius + 1)]
        active = (stripe.sum(axis=1) >= max(2, stripe.shape[1] * 0.08)).astype(np.uint8)
        active = cv2.morphologyEx(active[:, None], cv2.MORPH_CLOSE, np.ones((3, 1), np.uint8))[:, 0]
        edges = np.diff(np.pad(active.astype(np.int8), (1, 1)))
        starts, ends = np.flatnonzero(edges == 1), np.flatnonzero(edges == -1)
        bands.append(
            [
                float((a + b - 1) / 2)
                for a, b in zip(starts, ends, strict=True)
                if 3 <= b - a <= height * 0.045
            ]
        )
    seed = max(range(len(bands)), key=lambda i: len(bands[i]))
    if len(bands[seed]) < 3:
        raise ValueError("no reliable text baselines found")
    spacing = float(np.median(np.diff(bands[seed])))
    tolerance = min(height * 0.035, spacing * 0.45)
    curves: list[list[list[float]]] = []
    for y in bands[seed][: MAX_CURVES * 3]:
        observations = [(float(xs[seed]), y)]
        for direction in (-1, 1):
            last_x, last_y, slope = float(xs[seed]), y, 0.0
            for index in range(seed + direction, len(xs) if direction == 1 else -1, direction):
                prediction = last_y + slope * (float(xs[index]) - last_x)
                if not bands[index]:
                    continue
                match = min(bands[index], key=lambda candidate: abs(candidate - prediction))
                if abs(match - prediction) > tolerance:
                    continue
                slope = (match - last_y) / (float(xs[index]) - last_x)
                last_x, last_y = float(xs[index]), match
                observations.append((last_x, last_y))
        if len(observations) < 12 or np.ptp([p[0] for p in observations]) < width * 0.55:
            continue
        points = np.asarray(observations)
        along = points[:, 0] / width
        keep = np.ones(len(points), dtype=bool)
        for _ in range(3):
            coefficients = np.polyfit(along[keep], points[keep, 1], 2)
            residual = np.abs(points[:, 1] - np.polyval(coefficients, along))
            keep = residual <= max(2.0, float(np.median(residual)) * 3)
            if int(keep.sum()) < 10:
                break
        if int(keep.sum()) < 10 or float(np.median(residual)) > max(2, height * 0.004):
            continue
        controls_x = np.linspace(0, source.width - 1, 9)
        controls_y = np.polyval(coefficients, controls_x * scale / width) / scale
        if (controls_y < 0).any() or (controls_y > source.height - 1).any():
            continue
        # A narrow stripe can divide a glyph into several horizontal ink bands.
        # Tracking may make those seeds converge onto the same text baseline.
        if any(
            float(np.median(np.abs(np.asarray(existing)[:, 1] - controls_y)))
            < max(3, source.height * 0.008)
            for existing in curves
        ):
            continue
        curves.append(np.column_stack((controls_x, controls_y)).tolist())
        if len(curves) >= MAX_CURVES:
            break
    if len(curves) < 3:
        raise ValueError("too few reliable curved baselines")
    return normalize_curves(curves, source.size)


def render(source: Image.Image, value: Any, paper: Any = 255) -> tuple[Image.Image, dict[str, Any]]:
    curves = normalize_curves(value, source.size)
    arrays = [np.asarray(curve) for curve in curves]
    # Uniform control positions make anchors independent of image dimensions and
    # reproduce precisely when detected geometry is used as a manual correction.
    sample_x = np.linspace(0, source.width - 1, 65)
    samples = np.asarray([np.interp(sample_x, c[:, 0], c[:, 1]) for c in arrays])
    anchors = np.median(samples, axis=1)
    maximum = float(np.max(np.abs(samples - anchors[:, None])))
    padding = int(ceil(maximum)) + 2
    if maximum < 0.5:
        return source.copy(), {
            "curvature_padding": 0,
            "curvature_max_displacement": maximum,
            "dewarp_correction": "none_needed",
        }
    result = Image.new(source.mode, (source.width, source.height + 2 * padding), paper)
    for top in range(0, result.height, TILE_EDGE):
        bottom = min(result.height, top + TILE_EDGE)
        target_y = np.arange(top, bottom, dtype=np.float32) - padding
        high = np.clip(np.searchsorted(anchors, target_y), 0, len(anchors) - 1)
        low = np.maximum(0, high - 1)
        low[target_y >= anchors[-1]] = len(anchors) - 1
        denominator = anchors[high] - anchors[low]
        weight = np.divide(
            target_y - anchors[low],
            denominator,
            out=np.zeros_like(target_y),
            where=denominator != 0,
        )
        weight = np.clip(weight, 0, 1)
        for left in range(0, source.width, TILE_EDGE):
            right = min(source.width, left + TILE_EDGE)
            xs = np.arange(left, right, dtype=np.float32)
            profiles = np.asarray([np.interp(xs, c[:, 0], c[:, 1]) for c in arrays])
            shifts = profiles - anchors[:, None]
            delta = (1 - weight[:, None]) * shifts[low] + weight[:, None] * shifts[high]
            map_y = (target_y[:, None] + delta).astype(np.float32)
            y1 = max(0, int(np.floor(map_y.min())) - 2)
            y2 = min(source.height, int(np.ceil(map_y.max())) + 3)
            if y1 >= y2:
                continue
            region = np.asarray(source.crop((left, y1, right, y2)))
            map_x = np.broadcast_to(xs - left, map_y.shape).copy()
            warped = cv2.remap(
                region,
                map_x,
                map_y - y1,
                cv2.INTER_CUBIC,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=paper,
            )
            result.paste(Image.fromarray(warped), (left, top))
    return result, {
        "curvature_padding": padding,
        "curvature_max_displacement": maximum,
        "dewarp_correction": "applied",
    }
