"""Compare the #223 experimental baseline filter over pinned and synthetic pages."""

from __future__ import annotations

import argparse
import hashlib
import os
from pathlib import Path
from types import SimpleNamespace

import django
import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from ebook_server.core import curvature
from tests.curvature_fixtures import curled_page, straightness
from tests.test_curvature import inner_band_test_page

PHOTOS = (
    (
        "a",
        "b7e3aac880412b7967c49b0b3271fb58a333a614fab0abf58e2bd137de3482f3",
        (420, 250, 2150, 3060),
    ),
    (
        "b",
        "5a518c607a701c26b5e60c366df06e64c1b8124d4452d1a3d06c427c975fce38",
        (305, 205, 2050, 3120),
    ),
)


def evaluate(label: str, source: Image.Image, calibration: Image.Image | None = None) -> None:
    from ebook_server.core import scantailor
    from tests.stage_helpers import run_stage

    print(label)
    for restrict in (False, True):
        name = "inner 80%" if restrict else "full height"
        try:
            curves = curvature.detect_curves(source, inner_band=restrict)
            _, padding, shift = curvature.measure(curves, source.size)
            outer = sum(
                not curvature.INNER_BAND_TOP
                <= np.median([y for _, y in curve]) / source.height
                <= curvature.INNER_BAND_BOTTOM
                for curve in curves
            )
            result = f"{len(curves)} curves ({outer} outside), {shift:.2f}px max shift"
            if calibration is not None:
                corrected, _ = curvature.render(calibration, curves)
                result += f", calibration RMS {straightness(corrected, padding=padding):.3f}px"
        except ValueError as exc:
            result = str(exc)
        stage = run_stage(
            scantailor._run_dewarping,
            SimpleNamespace(parameters={"dewarp_mode": "auto", "dewarp_inner_band": restrict}),
            source,
        )
        stage_result = (
            "applied" if stage.parameters["dewarp_applied"] else stage.parameters["dewarp_warning"]
        )
        print(f"  {name}: {result}; cylindrical stage: {stage_result}")


def draw_example(path: Path) -> None:
    source = inner_band_test_page()
    board = Image.new("RGB", (680, 492), "white")
    draw = ImageDraw.Draw(board)
    font = ImageFont.load_default(size=14)
    for offset, restrict in ((0, False), (340, True)):
        board.paste(source.resize((330, 462)).convert("RGB"), (offset, 27))
        draw.text(
            (offset + 6, 5), "Inner 80%" if restrict else "Full height", font=font, fill="black"
        )
        for curve in curvature.detect_curves(source, inner_band=restrict):
            draw.line(
                [(offset + x * 0.33, 27 + y * 0.33) for x, y in curve], fill="#bc382a", width=2
            )
    board.save(path, optimize=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--photos", type=Path, help="directory containing the two pinned JPEGs")
    parser.add_argument("--example", type=Path, help="write the synthetic baseline comparison PNG")
    args = parser.parse_args()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")
    django.setup()
    for lift in (24, 64):
        evaluate(f"synthetic curl {lift}px", curled_page(lift), curled_page(lift, calibration=True))
    evaluate("synthetic page numbers", inner_band_test_page())
    evaluate("synthetic tight margins", inner_band_test_page(sparse=True))
    if args.photos:
        for suffix, expected, box in PHOTOS:
            path = args.photos / f"boston_cooking_{suffix}.jpg"
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                raise ValueError(f"Unexpected photograph content: {path.name}")
            with Image.open(path) as image:
                evaluate(path.name, ImageOps.exif_transpose(image).crop(box))
    if args.example:
        draw_example(args.example)


if __name__ == "__main__":
    main()
