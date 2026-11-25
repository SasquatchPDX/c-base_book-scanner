# Derived from Scan Tailor filters/page_layout/Utils.cpp, filters/page_layout/Settings.cpp,
# filters/page_layout/Settings.h, filters/page_layout/Alignment.h and
# filters/page_layout/Task.cpp
# Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
# Copyright (C) c-base_book-scanner contributors
#
# This program is free software: you can redistribute it and/or modify it under the
# terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later version.
# See the LICENSE file at the repository root.
"""Page layout (Margins stage): a port of upstream ``filters/page_layout`` (#208).

Upstream keeps, per page, the content size in millimetres, the *hard* margins (mm)
and an alignment. ``hard size = content size + hard margins``. The *aggregate hard
size* is the largest hard width and the largest hard height over all pages aligned
with the others (a page that is not aligned contributes 0). Each aligned page then
gets *soft* margins, ``aggregate - own hard size``, split by its alignment
(``Utils::calcSoftMarginsMM``), so every aligned page comes out at the same physical
size; the hard margin is a minimum. A page without content uses
``Utils::adaptContentRect``: a tiny rectangle in the middle of the page.

Divergences, with reasons (also in ``docs/third-party/scantailor-port.md``):

1. Select Content has already cropped the page to its content box, so the content
   rectangle is the whole stage input and the page rectangle is expressed as a
   ``pad`` op (offset and canvas) instead of a polygon in the original image.
2. Hard margins are rounded to whole pixels per side *before* the hard size is
   converted to millimetres, so pages of the same dpi get exactly the same pixel size
   and the largest page's margin is exactly the set margin. Upstream keeps fractional
   pixels until Output rounds the crop area.
3. The adapted rectangle of an empty page has size 0 instead of 0.02 px.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from math import floor, isfinite
from typing import Any, Final

MM_PER_INCH: Final[float] = 25.4
#: Upper bound of one hard margin; guards against absurd canvases.
MAX_MARGIN_MM: Final[float] = 500.0
ALIGN_X: Final[tuple[str, ...]] = ("left", "center", "right")
ALIGN_Y: Final[tuple[str, ...]] = ("top", "center", "bottom")
#: Settings::Impl ``m_defaultAlignment(Alignment::TOP, Alignment::HCENTER)``.
DEFAULT_ALIGN_X: Final[str] = "center"
DEFAULT_ALIGN_Y: Final[str] = "top"
#: Bumped when the aggregate's meaning changes, so every fingerprint changes with it.
AGGREGATE_VERSION: Final[str] = "page-layout-v1"
#: Stage parameters the member sets (millimetres, alignment, "align with others").
MARGIN_KEYS_MM: Final[tuple[str, ...]] = ("left_mm", "top_mm", "right_mm", "bottom_mm")
#: Pixel parameters written before #208; read once and converted with the page dpi.
LEGACY_MARGIN_KEYS_PX: Final[tuple[str, ...]] = ("margin", "left", "top", "right", "bottom")


@dataclass(frozen=True)
class Margins:
    left: float
    top: float
    right: float
    bottom: float

    def as_dict(self, digits: int | None = None) -> dict[str, float]:
        values = {"left": self.left, "top": self.top, "right": self.right, "bottom": self.bottom}
        if digits is None:
            return values
        return {key: round(value, digits) for key, value in values.items()}


#: ``Settings::defaultHardMarginsMM()``: ``Margins(10.0, 5.0, 10.0, 5.0)``.
DEFAULT_HARD_MARGINS_MM: Final[Margins] = Margins(10.0, 5.0, 10.0, 5.0)


@dataclass(frozen=True)
class LayoutSettings:
    """One page's ``Params`` minus the content size: hard margins and alignment."""

    hard_margins_mm: Margins
    align_x: str = DEFAULT_ALIGN_X
    align_y: str = DEFAULT_ALIGN_Y
    #: False is upstream's null ``Alignment``: no soft margins, no influence.
    align_with_others: bool = True


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if isfinite(number) else None


def _margin_mm(value: object, fallback: float) -> float:
    number = _number(value)
    if number is None:
        return fallback
    return min(MAX_MARGIN_MM, max(0.0, number))


def _alignment(value: object, choices: tuple[str, ...], fallback: str) -> str:
    text = str(value if value is not None else "").strip().lower()
    if text in {"hcenter", "vcenter", "centre", "middle"}:
        text = "center"
    return text if text in choices else fallback


def _flag(value: object, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return fallback


def aligns_with_others(parameters: Mapping[str, Any]) -> bool:
    """Whether a page's Margins settings take part in the common page size."""
    return _flag(parameters.get("align_with_others"), True)


def settings_from_parameters(
    parameters: Mapping[str, Any], dpi: tuple[float, float]
) -> LayoutSettings:
    """The page's layout settings from its Margins stage parameters.

    Margins are millimetres (``left_mm`` ...). Parameters written before #208 hold
    pixels (``margin``, ``left`` ...); they are converted with the page's dpi. A page
    with neither uses upstream's defaults.
    """
    dpi_x, dpi_y = (max(1.0, float(value)) for value in dpi)
    defaults = DEFAULT_HARD_MARGINS_MM
    if not any(key in parameters for key in MARGIN_KEYS_MM) and any(
        key in parameters for key in LEGACY_MARGIN_KEYS_PX
    ):
        common = max(0.0, _number(parameters.get("margin")) or 0.0)

        def legacy(key: str, dpi_axis: float) -> float:
            pixels = _number(parameters.get(key))
            pixels = common if pixels is None or pixels < 0 else pixels
            return _margin_mm(pixels * MM_PER_INCH / dpi_axis, 0.0)

        margins = Margins(
            legacy("left", dpi_x),
            legacy("top", dpi_y),
            legacy("right", dpi_x),
            legacy("bottom", dpi_y),
        )
    else:
        margins = Margins(
            _margin_mm(parameters.get("left_mm"), defaults.left),
            _margin_mm(parameters.get("top_mm"), defaults.top),
            _margin_mm(parameters.get("right_mm"), defaults.right),
            _margin_mm(parameters.get("bottom_mm"), defaults.bottom),
        )
    return LayoutSettings(
        hard_margins_mm=margins,
        align_x=_alignment(parameters.get("align_x"), ALIGN_X, DEFAULT_ALIGN_X),
        align_y=_alignment(parameters.get("align_y"), ALIGN_Y, DEFAULT_ALIGN_Y),
        align_with_others=aligns_with_others(parameters),
    )


def hard_margins_px(margins_mm: Margins, dpi: tuple[float, float]) -> tuple[int, int, int, int]:
    """Hard margins in whole pixels (left, top, right, bottom) at the page's dpi."""
    to_x = max(1.0, float(dpi[0])) / MM_PER_INCH
    to_y = max(1.0, float(dpi[1])) / MM_PER_INCH
    return (
        round(margins_mm.left * to_x),
        round(margins_mm.top * to_y),
        round(margins_mm.right * to_x),
        round(margins_mm.bottom * to_y),
    )


def hard_size_mm(
    content_px: tuple[int, int], settings: LayoutSettings, dpi: tuple[float, float]
) -> tuple[float, float]:
    """``Settings::Item::hardWidthMM/hardHeightMM``: content plus hard margins, in mm."""
    left, top, right, bottom = hard_margins_px(settings.hard_margins_mm, dpi)
    dpi_x, dpi_y = (max(1.0, float(value)) for value in dpi)
    return (
        (content_px[0] + left + right) * MM_PER_INCH / dpi_x,
        (content_px[1] + top + bottom) * MM_PER_INCH / dpi_y,
    )


def soft_margins(
    hard_size: tuple[float, float], aggregate_size: tuple[float, float], settings: LayoutSettings
) -> Margins:
    """``Utils::calcSoftMarginsMM`` (any unit): the gap to the aggregate, split by alignment."""
    if not settings.align_with_others:
        # A null alignment: this page is not aligned with the others.
        return Margins(0.0, 0.0, 0.0, 0.0)
    left = right = top = bottom = 0.0
    delta_width = aggregate_size[0] - hard_size[0]
    if delta_width > 0.0:
        if settings.align_x == "left":
            right = delta_width
        elif settings.align_x == "right":
            left = delta_width
        else:
            left = right = 0.5 * delta_width
    delta_height = aggregate_size[1] - hard_size[1]
    if delta_height > 0.0:
        if settings.align_y == "top":
            bottom = delta_height
        elif settings.align_y == "bottom":
            top = delta_height
        else:
            top = bottom = 0.5 * delta_height
    return Margins(left, top, right, bottom)


@dataclass(frozen=True)
class PageEntry:
    """One page's contribution to the aggregate (``Settings::Item``)."""

    page_id: str
    hard_size_mm: tuple[float, float]
    align_with_others: bool


@dataclass(frozen=True)
class Aggregate:
    """The job-wide aggregate hard size and a stable fingerprint of it."""

    width_mm: float
    height_mm: float
    fingerprint: str

    @property
    def size_mm(self) -> tuple[float, float]:
        return self.width_mm, self.height_mm

    def as_parameters(self) -> dict[str, Any]:
        return {
            "width": round(self.width_mm, 3),
            "height": round(self.height_mm, 3),
        }


def fingerprint(width_mm: float, height_mm: float) -> str:
    """Stable across processes: the version and the size to 1/1000 mm."""
    payload = json.dumps(
        [AGGREGATE_VERSION, f"{width_mm:.3f}", f"{height_mm:.3f}"], separators=(",", ":")
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:32]


def aggregate_hard_size(entries: Iterable[PageEntry]) -> Aggregate:
    """``Settings::Impl::getAggregateHardSizeMMLocked``: max influence width and height."""
    width = height = 0.0
    for entry in entries:
        if not entry.align_with_others:
            continue  # influenceHardWidthMM/HeightMM are 0 for a null alignment
        width = max(width, entry.hard_size_mm[0])
        height = max(height, entry.hard_size_mm[1])
    return Aggregate(width, height, fingerprint(width, height))


@dataclass(frozen=True)
class PageRect:
    """``Utils::calcPageRectPhys`` as a pad op: where the input goes on the page canvas."""

    offset: tuple[int, int]
    canvas: tuple[int, int]
    hard_px: tuple[int, int, int, int]
    soft_px: tuple[int, int, int, int]
    hard_size_mm: tuple[float, float]


def page_rect(
    input_size: tuple[int, int],
    content_found: bool,
    settings: LayoutSettings,
    dpi: tuple[float, float],
    aggregate_size_mm: tuple[float, float],
) -> PageRect:
    """The page rectangle of one page around its content, in input pixels.

    The content is the whole stage input (Select Content cropped to it). An empty
    content box is ``Utils::adaptContentRect``: a point in the middle of the input,
    so the page is cut from around the page centre.
    """
    dpi_x, dpi_y = (max(1.0, float(value)) for value in dpi)
    left, top, right, bottom = hard_margins_px(settings.hard_margins_mm, (dpi_x, dpi_y))
    content_w, content_h = input_size if content_found else (0, 0)
    hard_w = content_w + left + right
    hard_h = content_h + top + bottom
    own_mm = (hard_w * MM_PER_INCH / dpi_x, hard_h * MM_PER_INCH / dpi_y)
    if settings.align_with_others:
        target_w = max(hard_w, round(aggregate_size_mm[0] * dpi_x / MM_PER_INCH))
        target_h = max(hard_h, round(aggregate_size_mm[1] * dpi_y / MM_PER_INCH))
    else:
        target_w, target_h = hard_w, hard_h
    # calcSoftMarginsMM in pixels; a centred odd pixel goes to the right/bottom side.
    soft = soft_margins((hard_w, hard_h), (target_w, target_h), settings)
    soft_left, soft_top = floor(soft.left), floor(soft.top)
    soft_right = target_w - hard_w - soft_left
    soft_bottom = target_h - hard_h - soft_top
    if content_found:
        offset = (left + soft_left, top + soft_top)
    else:
        offset = (
            round(left + soft_left - input_size[0] / 2.0),
            round(top + soft_top - input_size[1] / 2.0),
        )
    return PageRect(
        offset=offset,
        canvas=(max(1, target_w), max(1, target_h)),
        hard_px=(left, top, right, bottom),
        soft_px=(soft_left, soft_top, soft_right, soft_bottom),
        hard_size_mm=own_mm,
    )
