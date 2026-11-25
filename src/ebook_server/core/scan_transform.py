"""Geometric transformations of the ScanTailor-Web pipeline (#195 step 2).

Stages 1-4 (Deskew, Select Content, Dewarping, Margins) do not store full-resolution
pixels. Each records one *transform op* in the full-resolution coordinates of its own
input, as upstream Scan Tailor passes an ``ImageTransformation`` between filters
instead of images. An op can be

- applied at any scale: the working-resolution chain (previews, detection) applies it
  with the scale of its working image, while the Output stage applies every op at scale
  1 to the untouched original, once;
- sized without rendering: :func:`output_size` gives the full-resolution output size,
  which the editor needs to map preview pixels to stage coordinates.

Ops are plain JSON objects, stored in the stage's ``parameters["transform"]``:

``{"op": "identity"}``
``{"op": "rotate", "angle": a}``
    Counter-clockwise by ``a`` degrees about the centre (the Pillow ``rotate``
    convention used by the deskew stage), on a canvas that holds the whole result.
``{"op": "crop", "box": [left, top, right, bottom]}``
``{"op": "perspective", "matrix": 3x3, "canvas": [w, h]}``
    Homography from the input onto an output canvas.
``{"op": "curvature", "curves": [...], "padding": p}``
    Baseline flattening (:mod:`.curvature`); the output is ``2 * p`` taller.
``{"op": "pad", "offset": [x, y], "canvas": [w, h]}``
    Place the input at ``offset`` on a paper-coloured canvas.
``{"op": "cylindrical", "top": [[x, y], ...], "bottom": [...], "depth": d, "canvas": [w, h]}``
    Upstream's cylindrical-surface dewarping (#195 step 6, :mod:`.cylindrical`): the
    top and bottom curves are polylines in continuous input coordinates (pixel ``i``
    covers ``[i, i + 1)``); the output keeps the input's size.
"""

from __future__ import annotations

import math
from typing import Any, Final

import cv2
import numpy as np
from PIL import Image

from . import curvature, cylindrical

type Size = tuple[int, int]
type Op = dict[str, Any]

IDENTITY: Final[Op] = {"op": "identity"}
_CEIL_TOLERANCE: Final[float] = 1e-6


class TransformError(ValueError):
    """An op is malformed or cannot be applied."""


def _ceil(value: float) -> int:
    """Round up, tolerating float noise (100.0000001 must not add a pixel)."""
    return math.ceil(value - _CEIL_TOLERANCE)


# --- constructors -----------------------------------------------------------------


def rotate(angle: float) -> Op:
    return IDENTITY if abs(angle) < 1e-9 else {"op": "rotate", "angle": float(angle)}


def crop(box: tuple[int, int, int, int], size: Size) -> Op:
    if tuple(box) == (0, 0, size[0], size[1]):
        return IDENTITY
    return {"op": "crop", "box": [int(value) for value in box]}


def perspective(matrix: Any, canvas: Size) -> Op:
    return {
        "op": "perspective",
        "matrix": np.asarray(matrix, dtype=np.float64).reshape(3, 3).tolist(),
        "canvas": [int(canvas[0]), int(canvas[1])],
    }


def curvature_op(curves: list[list[list[float]]], padding: int) -> Op:
    return {"op": "curvature", "curves": curves, "padding": int(padding)}


def cylindrical_op(
    top: list[tuple[float, float]],
    bottom: list[tuple[float, float]],
    depth: float,
    canvas: Size,
) -> Op:
    return {
        "op": "cylindrical",
        "top": [[float(x), float(y)] for x, y in top],
        "bottom": [[float(x), float(y)] for x, y in bottom],
        "depth": float(depth),
        "canvas": [int(canvas[0]), int(canvas[1])],
    }


def _cylindrical_model(
    op: Op, sx: float = 1.0, sy: float = 1.0
) -> tuple[cylindrical.CylindricalSurfaceDewarper, tuple[float, float, float, float], Size]:
    """The dewarper, its model domain and the output size of a cylindrical op at a scale."""
    try:
        top = [(float(x) * sx, float(y) * sy) for x, y in op["top"]]
        bottom = [(float(x) * sx, float(y) * sy) for x, y in op["bottom"]]
        canvas = (
            max(1, round(int(op["canvas"][0]) * sx)),
            max(1, round(int(op["canvas"][1]) * sy)),
        )
        dewarper = cylindrical.CylindricalSurfaceDewarper(top, bottom, float(op["depth"]))
        domain = cylindrical.model_domain(top, bottom, dewarper, (0.0, 0.0, canvas[0], canvas[1]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TransformError(f"bad cylindrical op: {exc}") from exc
    return dewarper, domain, canvas


def pad(offset: tuple[int, int], canvas: Size, size: Size) -> Op:
    if tuple(offset) == (0, 0) and tuple(canvas) == tuple(size):
        return IDENTITY
    return {
        "op": "pad",
        "offset": [int(offset[0]), int(offset[1])],
        "canvas": [int(canvas[0]), int(canvas[1])],
    }


# --- geometry ---------------------------------------------------------------------


def _rotation(angle: float, size: tuple[float, float]) -> tuple[np.ndarray, tuple[float, float]]:
    """Forward 2x3 matrix rotating CCW by ``angle`` about the centre, and the exact canvas."""
    width, height = size
    radians = math.radians(angle)
    cos, sin = math.cos(radians), math.sin(radians)
    canvas_w = abs(width * cos) + abs(height * sin)
    canvas_h = abs(width * sin) + abs(height * cos)
    # Image y points down, so a counter-clockwise turn on screen uses +sin in x.
    matrix = np.array(
        [
            [cos, sin, 0.0],
            [-sin, cos, 0.0],
        ],
        dtype=np.float64,
    )
    centre = np.array([width / 2.0, height / 2.0])
    target = np.array([canvas_w / 2.0, canvas_h / 2.0])
    matrix[:, 2] = target - matrix[:, :2] @ centre
    return matrix, (canvas_w, canvas_h)


def _half_pixel(matrix: np.ndarray) -> np.ndarray:
    """A 3x3 matrix on continuous coordinates as a matrix on pixel-centre indices.

    Pixel ``i`` covers ``[i, i + 1)``; OpenCV samples at its centre ``i + 0.5``.
    """
    to_continuous = np.array([[1, 0, 0.5], [0, 1, 0.5], [0, 0, 1]], dtype=np.float64)
    to_index = np.array([[1, 0, -0.5], [0, 1, -0.5], [0, 0, 1]], dtype=np.float64)
    return to_index @ matrix @ to_continuous


def _scaled(matrix: np.ndarray, sx: float, sy: float) -> np.ndarray:
    """``matrix`` (pixel-centre indices at full resolution) for an image scaled by sx, sy."""
    scaling = _half_pixel(np.diag([sx, sy, 1.0]))
    return scaling @ matrix @ np.linalg.inv(scaling)


def output_size(op: Op, size: Size) -> Size:
    """Full-resolution output size of ``op`` applied to an input of ``size``."""
    kind = op.get("op")
    width, height = size
    if kind == "identity":
        return size
    if kind == "rotate":
        _, (canvas_w, canvas_h) = _rotation(float(op["angle"]), (width, height))
        return max(1, _ceil(canvas_w)), max(1, _ceil(canvas_h))
    if kind == "crop":
        left, top, right, bottom = _box(op)
        return max(1, right - left), max(1, bottom - top)
    if kind in {"perspective", "pad", "cylindrical"}:
        canvas = op["canvas"]
        return int(canvas[0]), int(canvas[1])
    if kind == "curvature":
        return width, height + 2 * int(op["padding"])
    raise TransformError(f"unknown transform op {kind!r}")


def _box(op: Op) -> tuple[int, int, int, int]:
    box = op.get("box")
    if not isinstance(box, list | tuple) or len(box) != 4:
        raise TransformError("crop needs a four-value box")
    left, top, right, bottom = (int(value) for value in box)
    if right <= left or bottom <= top:
        raise TransformError("crop box has no area")
    return left, top, right, bottom


def map_point(op: Op, point: tuple[float, float], size: Size) -> tuple[float, float]:
    """Where a full-resolution input point lands in the op's output (for overlays/tests)."""
    kind = op.get("op")
    x, y = point
    if kind == "identity":
        return x, y
    if kind == "rotate":
        matrix, _ = _rotation(float(op["angle"]), size)
        mapped = _half_pixel(np.vstack([matrix, [0, 0, 1]])) @ np.array([x, y, 1.0])
        return float(mapped[0]), float(mapped[1])
    if kind == "crop":
        left, top, _, _ = _box(op)
        return x - left, y - top
    if kind == "perspective":
        mapped = np.asarray(op["matrix"], dtype=np.float64) @ np.array([x, y, 1.0])
        return float(mapped[0] / mapped[2]), float(mapped[1] / mapped[2])
    if kind == "pad":
        return x + int(op["offset"][0]), y + int(op["offset"][1])
    if kind == "curvature":
        return x, y + int(op["padding"])
    if kind == "cylindrical":
        dewarper, domain, _ = _cylindrical_model(op)
        crv_x, crv_y = dewarper.map_to_dewarped((x + 0.5, y + 0.5))
        return (
            float(domain[0] + crv_x * (domain[2] - domain[0]) - 0.5),
            float(domain[1] + crv_y * (domain[3] - domain[1]) - 0.5),
        )
    raise TransformError(f"unknown transform op {kind!r}")


# --- rendering ----------------------------------------------------------------------


def thumbnail(image: Image.Image, size: int = 1200) -> Image.Image:
    if image.width <= size and image.height <= size:
        return image
    copy = image.copy()
    copy.thumbnail((size, size))
    return copy


def paper_fill(image: Image.Image) -> int | tuple[int, int, int]:
    """Background colour for new pixels: bright paper level of the image itself."""
    array = np.asarray(thumbnail(image, size=300))
    if array.ndim == 2:
        return int(np.percentile(array, 90))
    levels = np.percentile(array.reshape(-1, array.shape[2]), 90, axis=0)
    return (int(levels[0]), int(levels[1]), int(levels[2]))


def _border(paper: int | tuple[int, int, int], image: Image.Image) -> Any:
    if image.mode == "RGB":
        return paper if isinstance(paper, tuple) else (paper, paper, paper)
    return paper if isinstance(paper, int) else int(sum(paper) / 3)


def apply(
    image: Image.Image,
    op: Op,
    scale: tuple[float, float] = (1.0, 1.0),
    paper: int | tuple[int, int, int] | None = None,
) -> Image.Image:
    """Apply ``op`` (full-resolution coordinates) to ``image`` at ``scale``.

    ``scale`` is ``(image width / full width, image height / full height)`` of the
    op's input, so the same op renders the working-resolution preview and, with
    scale 1, the full-resolution output. Returns a new image (or ``image`` itself for
    the identity op).
    """
    kind = op.get("op")
    sx, sy = scale
    if kind == "identity":
        return image
    fill = _border(paper if paper is not None else paper_fill(image), image)
    if kind == "rotate":
        full_size = (image.width / sx, image.height / sy)
        matrix, (canvas_w, canvas_h) = _rotation(float(op["angle"]), full_size)
        scaled = _scaled(_half_pixel(np.vstack([matrix, [0, 0, 1]])), sx, sy)
        canvas = (max(1, _ceil(canvas_w * sx)), max(1, _ceil(canvas_h * sy)))
        warped = cv2.warpAffine(
            np.asarray(image),
            scaled[:2],
            canvas,
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=fill,
        )
        return Image.fromarray(warped)
    if kind == "crop":
        left, top, right, bottom = _box(op)
        scaled_box = (
            max(0, math.floor(left * sx)),
            max(0, math.floor(top * sy)),
            min(image.width, max(math.floor(left * sx) + 1, _ceil(right * sx))),
            min(image.height, max(math.floor(top * sy) + 1, _ceil(bottom * sy))),
        )
        return image.crop(scaled_box)
    if kind == "perspective":
        # The homography maps pixel-centre indices (it is fitted to pixel points).
        matrix = np.asarray(op["matrix"], dtype=np.float64).reshape(3, 3)
        scaled = _scaled(matrix, sx, sy)
        canvas = (max(1, round(op["canvas"][0] * sx)), max(1, round(op["canvas"][1] * sy)))
        warped = cv2.warpPerspective(
            np.asarray(image),
            scaled,
            canvas,
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=fill,
        )
        return Image.fromarray(warped)
    if kind == "pad":
        offset = (round(op["offset"][0] * sx), round(op["offset"][1] * sy))
        canvas_size = (
            max(image.width + offset[0], round(op["canvas"][0] * sx)),
            max(image.height + offset[1], round(op["canvas"][1] * sy)),
        )
        canvas_image = Image.new(image.mode, canvas_size, fill)
        canvas_image.paste(image, offset)
        return canvas_image
    if kind == "curvature":
        curves = [
            [[(x + 0.5) * sx - 0.5, (y + 0.5) * sy - 0.5] for x, y in curve]
            for curve in op["curves"]
        ]
        padding = max(0, round(int(op["padding"]) * sy))
        return curvature.warp(image, curves, padding, fill)
    if kind == "cylindrical":
        dewarper, domain, canvas = _cylindrical_model(op, sx, sy)
        try:
            warped = cylindrical.dewarp_image(np.asarray(image), canvas, dewarper, domain, fill)
        except cylindrical.DewarpError as exc:
            raise TransformError(str(exc)) from exc
        return Image.fromarray(warped)
    raise TransformError(f"unknown transform op {kind!r}")


def apply_chain(
    image: Image.Image,
    ops: list[Op],
    full_size: Size,
) -> Image.Image:
    """Apply ``ops`` in order to ``image`` whose full-resolution size is ``full_size``.

    The scale of each step is the ratio of the current image to the current full
    size, so rounding at working resolution never accumulates into drift.
    """
    size = full_size
    for op in ops:
        scale = (image.width / size[0], image.height / size[1])
        image = apply(image, op, scale)
        size = output_size(op, size)
    return image
