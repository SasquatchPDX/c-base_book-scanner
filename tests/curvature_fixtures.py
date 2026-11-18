"""Synthetic curl with known geometry; not photographs or a real-book corpus."""

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

SIZE = (1000, 1400)
LINES = [f"CURVED PAGE CALIBRATION LINE {number:02d}" for number in range(1, 19)]
CALIBRATION_ROWS = (200, 450, 700, 950, 1200)


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
