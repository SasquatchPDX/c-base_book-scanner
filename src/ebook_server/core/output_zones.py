"""Bounded OpenCV layout detection for ScanTailor output, not OCR (#172).

Continuous-tone/dense regions are protected first. Morphology then groups the
remaining glyphs into text blocks. Detection is intentionally heuristic: every
region is shown to the operator and can be replaced with explicit geometry.
"""

from __future__ import annotations

from math import ceil, floor
from typing import Any, Final

import cv2
import numpy as np
from PIL import Image

DETECTOR_VERSION: Final = "opencv-layout-v1"
MAX_DETECTION_EDGE: Final = 1200
MAX_ZONES: Final = 64


def detect_zones(source: Image.Image) -> list[dict[str, Any]]:
    """Return source-coordinate text/picture rectangles; scratch ≤1200² pixels."""
    scale = min(1.0, MAX_DETECTION_EDGE / max(source.size))
    reduced = source.resize(
        (max(1, round(source.width * scale)), max(1, round(source.height * scale))),
        Image.Resampling.BILINEAR,
    )
    gray = np.asarray(reduced.convert("L"))
    height, width = gray.shape
    if min(height, width) < 16:
        return []
    paper = float(np.percentile(gray, 90))
    midtones = ((gray > 24) & (gray < max(40, paper - 25))).astype(np.uint8)
    _, ink = cv2.threshold(gray, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Dense halftones and smooth continuous tones are both picture candidates.
    tone_density = cv2.blur(midtones.astype(np.float32), (15, 15))
    ink_density = cv2.blur(ink.astype(np.float32), (31, 31))
    pictures: Any = ((tone_density > 0.48) | (ink_density > 0.65)).astype(np.uint8)
    pictures = cv2.morphologyEx(pictures, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    pictures = cv2.morphologyEx(pictures, cv2.MORPH_OPEN, np.ones((7, 7), np.uint8))
    picture_boxes = _components(pictures, min_area=max(100, width * height // 800))
    # Suppress a photographed desk/frame surrounding the page, not interior photos.
    picture_boxes = [
        box
        for box in picture_boxes
        if not (box[0] <= 2 and box[1] <= 2 and box[2] >= width - 2 and box[3] >= height - 2)
    ]
    text = ink.copy()
    for x1, y1, x2, y2 in picture_boxes:
        text[max(0, y1 - 3) : min(height, y2 + 3), max(0, x1 - 3) : min(width, x2 + 3)] = 0
    text = cv2.morphologyEx(text, cv2.MORPH_CLOSE, np.ones((3, 17), np.uint8))
    text = cv2.morphologyEx(text, cv2.MORPH_CLOSE, np.ones((17, 3), np.uint8))
    text_boxes = _components(text, min_area=35)
    scale_x, scale_y = source.width / width, source.height / height
    zones = []
    for kind, boxes in (("picture", picture_boxes), ("text", text_boxes)):
        for x1, y1, x2, y2 in boxes:
            if x2 - x1 < 8 or y2 - y1 < 4:
                continue
            box = [
                max(0, floor((x1 - 2) * scale_x)),
                max(0, floor((y1 - 2) * scale_y)),
                min(source.width, ceil((x2 + 2) * scale_x)),
                min(source.height, ceil((y2 + 2) * scale_y)),
            ]
            zones.append({"kind": kind, "box": box, "source": "auto"})
    if len(zones) > MAX_ZONES:
        raise ValueError(f"layout exceeds {MAX_ZONES} regions; use manual zones")
    return zones


def _components(mask: Any, *, min_area: int) -> list[tuple[int, int, int, int]]:
    count, _labels, stats, _centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    return [
        (int(x), int(y), int(x + width), int(y + height))
        for x, y, width, height, area in stats[1:count]
        if area >= min_area and width >= 4 and height >= 4
    ]
