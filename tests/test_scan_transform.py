"""Transform ops of the step-2 pipeline (#195): sizes, scale agreement, direction."""

from __future__ import annotations

import numpy as np
import pytest
from PIL import Image, ImageDraw

from ebook_server.core import scan_transform
from ebook_server.core.scan_transform import Op


def _page(width: int = 600, height: int = 800, mode: str = "L") -> Image.Image:
    image = Image.new(mode, (width, height), 235 if mode == "L" else (235, 235, 235))
    draw = ImageDraw.Draw(image)
    for y in range(80, height - 80, 40):
        draw.rectangle((60, y, width - 60, y + 12), fill=20 if mode == "L" else (20, 20, 20))
    return image


OPS: list[Op] = [
    scan_transform.rotate(3.5),
    scan_transform.rotate(-90.0),
    scan_transform.crop((50, 40, 520, 700), (600, 800)),
    scan_transform.perspective(
        [[1.02, 0.03, 4.0], [0.01, 0.98, 6.0], [0.00002, 0.00001, 1.0]], (640, 820)
    ),
    scan_transform.pad((30, 50), (700, 920), (600, 800)),
    scan_transform.curvature_op(
        [[[0.0, y + 20.0], [300.0, y], [599.0, y + 20.0]] for y in (150.0, 400.0, 650.0)], 22
    ),
]


@pytest.mark.parametrize("op", OPS, ids=lambda op: str(op["op"]))
def test_full_resolution_render_has_the_predicted_size(op: Op) -> None:
    image = _page()
    rendered = scan_transform.apply(image, op)
    assert rendered.size == scan_transform.output_size(op, image.size)
    assert rendered.mode == image.mode


@pytest.mark.parametrize("op", OPS, ids=lambda op: str(op["op"]))
def test_working_resolution_render_matches_a_downscaled_full_render(op: Op) -> None:
    """The same op at scale 0.5 previews what Output will render at full resolution."""
    full = _page()
    work = full.resize((300, 400), Image.Resampling.LANCZOS)
    expected_size = scan_transform.output_size(op, full.size)
    preview = scan_transform.apply(work, op, (0.5, 0.5))
    assert abs(preview.width - expected_size[0] / 2) <= 1
    assert abs(preview.height - expected_size[1] / 2) <= 1
    reference = scan_transform.apply(full, op).resize(preview.size, Image.Resampling.LANCZOS)
    difference = np.abs(np.asarray(preview, dtype=np.int16) - np.asarray(reference, dtype=np.int16))
    assert float(difference.mean()) < 6.0, float(difference.mean())


def test_identity_returns_the_input_unchanged() -> None:
    image = _page()
    assert scan_transform.apply(image, scan_transform.IDENTITY) is image
    assert scan_transform.rotate(0.0) == scan_transform.IDENTITY
    assert scan_transform.crop((0, 0, 600, 800), (600, 800)) == scan_transform.IDENTITY
    assert scan_transform.pad((0, 0), (600, 800), (600, 800)) == scan_transform.IDENTITY


@pytest.mark.parametrize("angle", [2.0, -5.5, 90.0])
def test_rotation_turns_counter_clockwise_like_pillow(angle: float) -> None:
    image = Image.new("L", (400, 300), 255)
    ImageDraw.Draw(image).rectangle((330, 140, 350, 160), fill=0)
    ours = scan_transform.apply(image, scan_transform.rotate(angle), paper=255)
    pillow = image.rotate(angle, expand=True, fillcolor=255)
    assert abs(ours.width - pillow.width) <= 1 and abs(ours.height - pillow.height) <= 1

    def dark_centre(picture: Image.Image) -> tuple[float, float]:
        ys, xs = np.nonzero(np.asarray(picture) < 128)
        return float(xs.mean()), float(ys.mean())

    ours_x, ours_y = dark_centre(ours)
    pillow_x, pillow_y = dark_centre(pillow)
    assert abs(ours_x - pillow_x) <= 1.5 and abs(ours_y - pillow_y) <= 1.5
    predicted = scan_transform.map_point(scan_transform.rotate(angle), (340.0, 150.0), (400, 300))
    assert abs(predicted[0] - ours_x) <= 1.5 and abs(predicted[1] - ours_y) <= 1.5


def test_rotated_canvas_keeps_every_corner_and_fills_with_paper() -> None:
    image = Image.new("RGB", (300, 400), (240, 235, 220))
    rotated = scan_transform.apply(image, scan_transform.rotate(10.0))
    corner = rotated.getpixel((0, 0))
    assert isinstance(corner, tuple)
    assert all(
        abs(channel - expected) <= 2
        for channel, expected in zip(corner, (240, 235, 220), strict=True)
    )
    assert rotated.size == scan_transform.output_size(scan_transform.rotate(10.0), (300, 400))


def test_chain_composes_sizes_and_never_drifts() -> None:
    ops = [OPS[0], OPS[2], OPS[4]]
    full = _page()
    size = full.size
    for op in ops:
        size = scan_transform.output_size(op, size)
    rendered = scan_transform.apply_chain(full, ops, full.size)
    assert rendered.size == size
    work = scan_transform.apply_chain(
        full.resize((300, 400), Image.Resampling.LANCZOS), ops, full.size
    )
    assert abs(work.width * 2 - rendered.width) <= 2
    assert abs(work.height * 2 - rendered.height) <= 2


def test_unknown_or_broken_ops_are_rejected() -> None:
    with pytest.raises(scan_transform.TransformError):
        scan_transform.output_size({"op": "shear"}, (10, 10))
    with pytest.raises(scan_transform.TransformError):
        scan_transform.apply(_page(), {"op": "crop", "box": [5, 5, 5, 9]})
