"""Synthetic curl with known geometry; not photographs or a real-book corpus.

``curled_page`` shifts every row by the same quadratic (the #171 baseline-curvature
fixture). ``cylinder_page`` is a pinhole photograph of the page bent around an axis
parallel to the spine, the surface that Scan Tailor's cylindrical dewarping models
(#195 step 6): lines above the camera axis bow one way, lines below it the other.
"""

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

SIZE = (1000, 1400)
LINES = [f"CURVED PAGE CALIBRATION LINE {number:02d}" for number in range(1, 19)]
CALIBRATION_ROWS = (200, 450, 700, 950, 1200)


def _flat(calibration: bool) -> Image.Image:
    flat = Image.new("L", SIZE, 255)
    draw = ImageDraw.Draw(flat)
    if calibration:
        for y in CALIBRATION_ROWS:
            draw.line((70, y, 930, y), fill=0, width=2)
    else:
        font = ImageFont.load_default(size=38)
        for index, text in enumerate(LINES):
            draw.text((70, 160 + index * 55), text, fill=0, font=font)
    return flat


#: Camera distance in pixels for ``cylinder_page`` (the page plane is z = 0).
CAMERA_DISTANCE = 3000.0
#: Glyph baselines of the first and last text line (``load_default(size=38)``).
TEXT_BASELINES = (160.0 + 35.0, 160.0 + 55.0 * (len(LINES) - 1) + 35.0)


def _cylinder_surface(lift: float) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Surface x, its height z and the flat (arc-length) x for ``cylinder_page``."""
    width = SIZE[0]
    xs = np.linspace(0.0, float(width), 8001)
    zs = lift * (1.0 - xs / width) ** 2
    arc = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(zs)))])
    return xs, zs, arc


def cylinder_projection(lift: float, flat_point: tuple[float, float]) -> tuple[float, float]:
    """Where a point of the flat page appears in ``cylinder_page(lift)``."""
    xs, zs, arc = _cylinder_surface(lift)
    cx, cy = SIZE[0] / 2, SIZE[1] / 2
    x = float(np.interp(flat_point[0], arc, xs))
    z = float(np.interp(flat_point[0], arc, zs))
    scale = CAMERA_DISTANCE / (CAMERA_DISTANCE - z)
    return cx + (x - cx) * scale, cy + (flat_point[1] - cy) * scale


def cylinder_page(lift: float, *, calibration: bool = False) -> Image.Image:
    """The flat page bent into a cylinder that rises by ``lift`` px towards the spine
    (x = 0, height ``lift * (1 - x / width)**2``), seen by a pinhole camera above the
    page centre. Horizontal positions follow the arc length, so the page is also
    foreshortened near the spine, as a real photographed page is."""
    flat = np.asarray(_flat(calibration))
    width, height = SIZE
    xs, zs, arc = _cylinder_surface(lift)
    cx, cy = width / 2, height / 2
    scale = CAMERA_DISTANCE / (CAMERA_DISTANCE - zs)
    image_x = cx + (xs - cx) * scale  # increasing for the lifts used here
    u = np.arange(width, dtype=np.float64) + 0.5
    surface_x = np.interp(u, image_x, xs, left=np.nan, right=np.nan)
    z = lift * (1.0 - surface_x / width) ** 2
    flat_x = np.interp(surface_x, xs, arc)
    v = np.arange(height, dtype=np.float64)[:, None] + 0.5
    flat_y = cy + (v - cy) * (CAMERA_DISTANCE - z[None, :]) / CAMERA_DISTANCE
    map_x = np.broadcast_to(flat_x[None, :] - 0.5, (height, width)).astype(np.float32)
    map_y = (flat_y - 0.5).astype(np.float32)
    map_x = np.nan_to_num(map_x, nan=-10.0)
    map_y = np.nan_to_num(map_y, nan=-10.0)
    return Image.fromarray(
        cv2.remap(
            flat, map_x, map_y, cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT, borderValue=255
        )
    )


def curled_page(amplitude: float, *, calibration: bool = False) -> Image.Image:
    flat = Image.new("L", SIZE, 255)
    draw = ImageDraw.Draw(flat)
    if calibration:
        for y in CALIBRATION_ROWS:
            draw.line((70, y, 930, y), fill=0, width=2)
    else:
        font = ImageFont.load_default(size=38)
        for index, text in enumerate(LINES):
            draw.text((70, 160 + index * 55), text, fill=0, font=font)
    y, x = np.mgrid[: SIZE[1], : SIZE[0]].astype(np.float32)
    offset = amplitude * ((x - SIZE[0] / 2) / (SIZE[0] / 2)) ** 2
    return Image.fromarray(
        cv2.remap(
            np.asarray(flat),
            x,
            y - offset,
            cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=255,
        )
    )


def straightness(image: Image.Image, *, padding: int = 0) -> float:
    """Pixel-measured RMS residual of calibration ink after removing linear tilt."""
    gray = np.asarray(image.convert("L"))
    residuals = []
    for y in CALIBRATION_ROWS:
        points = []
        for x in range(80, 920, 3):
            top, bottom = y + padding - 15, y + padding + 110
            ink = np.flatnonzero(gray[top:bottom, x] < 128)
            if len(ink):
                points.append((x, top + float(np.mean(ink))))
        assert len(points) >= 250, "the correction lost calibration ink"
        data = np.asarray(points)
        line = np.polyfit(data[:, 0], data[:, 1], 1)
        residuals.extend((data[:, 1] - np.polyval(line, data[:, 0])).tolist())
    return float(np.sqrt(np.mean(np.square(residuals))))
