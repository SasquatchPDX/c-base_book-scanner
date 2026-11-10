"""ScanTailor-Web worker orchestration helpers."""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import hypot
from pathlib import Path
from typing import Any, Final

import cv2
import numpy as np
import structlog
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from ebook_server.jobs import get_queue
from ebook_server.storage import LocalFilesystemStorage

from .models import (
    ProcessingJob,
    ScanPage,
    ScanTailorStageState,
    StorageLocation,
)

logger = structlog.get_logger(__name__)

STAGE_NAMESPACE: Final[str] = "scan-jobs"
type Point = tuple[int, int]
type Quad = tuple[Point, Point, Point, Point]
type Box = tuple[int, int, int, int]

DEFAULT_STAGE_ALGORITHM: Final[dict[str, str]] = {
    "page_split": "auto_split",
    "deskew": "projection_deskew",
    "select_content": "content_box_projection",
    "margins": "margin_pad",
    "dewarping": "projective_dewarp",
    "output": "binarization_otsu",
}

DEFAULT_STAGE_ALGORITHM_VERSION: Final[dict[str, str]] = {
    "page_split": "splitter-v1",
    "deskew": "projection-v1",
    "select_content": "projection-v1",
    "margins": "padding-v1",
    "dewarping": "opencv-v1",
    "output": "opencv-v1",
}


def normalize_stage(value: str) -> str | None:
    for stage in ScanTailorStageState.Stage:
        if stage.value == value:
            return stage.value
        if stage.name == value.upper():
            return stage.value
        normalized = value.strip().lower().replace(" ", "_")
        if stage.value == normalized:
            return stage.value
    return None


def stage_order() -> tuple[str, ...]:
    return (
        ScanTailorStageState.Stage.PAGE_SPLIT.value,
        ScanTailorStageState.Stage.DESKEW.value,
        ScanTailorStageState.Stage.SELECT_CONTENT.value,
        ScanTailorStageState.Stage.MARGINS.value,
        ScanTailorStageState.Stage.DEWARPING.value,
        ScanTailorStageState.Stage.OUTPUT.value,
    )


def stage_position(stage: str) -> int:
    try:
        return stage_order().index(stage)
    except ValueError:
        return len(stage_order())


def previous_stage(stage: str) -> str | None:
    try:
        position = stage_order().index(stage)
    except ValueError:
        return None
    if position == 0:
        return None
    return stage_order()[position - 1]


def downstream_stages(stage: str) -> tuple[str, ...]:
    try:
        position = stage_order().index(stage)
    except ValueError:
        return ()
    return stage_order()[position + 1 :]


def _scan_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(str(Path(django_settings.APP_DATA_DIR) / "scan-artifacts"))


def _scan_storage_location() -> StorageLocation:
    storage, _ = StorageLocation.objects.get_or_create(
        identifier="scan",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": "scan-artifacts",
        },
    )
    return storage


def _compute_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _build_output_name(state: ScanTailorStageState, *, preview: bool = False) -> str:
    # Every stage writes PNG data regardless of the input format, so the artifact
    # name must carry the extension of the format actually written.
    del state
    return "preview.png" if preview else "output.png"


def _output_namespace(state: ScanTailorStageState) -> str:
    return (
        f"{STAGE_NAMESPACE}/{state.scan_job_id}/"
        f"{state.scan_page_id}/{state.stage}/v{state.revision}"
    )


def output_identifier(state: ScanTailorStageState, *, preview: bool = False) -> str:
    return _scan_storage().generate_destination(
        _output_namespace(state),
        _build_output_name(state, preview=preview),
    )


def _resolve_input_identifier(state: ScanTailorStageState) -> str:
    if state.input_stage_state is not None:
        return state.input_stage_state.output_storage_identifier
    if state.input_scan_page_version is not None:
        return state.input_scan_page_version.storage_identifier
    if state.input_storage_identifier:
        return state.input_storage_identifier
    return ""


def _coerce_int(value: object, default: int, *, minimum: int | None = None) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        value_int = value
    elif isinstance(value, (str, bytes, bytearray, float)):
        try:
            value_int = int(value)
        except (TypeError, ValueError):
            return default
    else:
        return default
    if minimum is not None and value_int < minimum:
        return default
    return value_int


def _coerce_float(value: object, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "1", "yes", "on"}:
            return True
        if lowered in {"false", "0", "no", "off"}:
            return False
    return default


def _as_recorded_parameters(parameters: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(parameters)
    merged.update(updates)
    return merged


def _default_algorithm(stage: str) -> str:
    return DEFAULT_STAGE_ALGORITHM.get(stage, "copy")


def _default_algorithm_version(stage: str) -> str:
    return DEFAULT_STAGE_ALGORITHM_VERSION.get(stage, "placeholder-v1")


@transaction.atomic
def get_or_create_stage_state(
    *,
    scan_job_id: str,
    page_id: str,
    stage: str,
) -> ScanTailorStageState:
    from .models import ScanJob  # local import to avoid cycle at module import time

    scan_job = ScanJob.objects.get(id=scan_job_id)
    scan_page = ScanPage.objects.get(id=page_id)
    predecessor_name = previous_stage(stage)
    predecessor: ScanTailorStageState | None = None
    if predecessor_name is not None:
        predecessor = (
            ScanTailorStageState.objects.filter(
                scan_job=scan_job,
                scan_page=scan_page,
                stage=predecessor_name,
            )
            .order_by("-created_at")
            .first()
        )

    active_version = scan_page.active_version()
    defaults = {
        "scan_job": scan_job,
        "scan_page": scan_page,
        "stage": stage,
        "algorithm": _default_algorithm(stage),
        "algorithm_version": _default_algorithm_version(stage),
        "input_stage_state": predecessor,
    }
    if predecessor is None and active_version is not None:
        defaults["input_scan_page_version"] = active_version
        defaults["input_storage_identifier"] = active_version.storage_identifier
    else:
        defaults["input_storage_identifier"] = ""

    state, _ = ScanTailorStageState.objects.select_for_update().get_or_create(
        scan_job=scan_job,
        scan_page=scan_page,
        stage=stage,
        defaults=defaults,
    )

    if state.input_stage_state_id != (predecessor.id if predecessor else None):
        state.input_stage_state = predecessor
    if predecessor is None and active_version is not None:
        state.input_scan_page_version = active_version
        state.input_storage_identifier = active_version.storage_identifier
    if not state.algorithm:
        state.algorithm = _default_algorithm(stage)
    if not state.algorithm_version:
        state.algorithm_version = _default_algorithm_version(stage)

    state.save(
        update_fields=[
            "input_stage_state",
            "input_scan_page_version",
            "input_storage_identifier",
            "algorithm",
            "algorithm_version",
        ],
    )
    return state


def invalidate_downstream_states(
    page: ScanPage,
    *,
    from_stage: str,
    reason: str,
) -> None:
    stages = downstream_stages(from_stage)
    if not stages:
        return
    query = Q()
    for stage in stages:
        query |= Q(scan_job=page.scan_job, scan_page=page, stage=stage)
    for state in ScanTailorStageState.objects.filter(query):
        state.status = ScanTailorStageState.StageStatus.INVALIDATED
        state.last_error = reason
        state.save(update_fields=["status", "last_error", "updated_at"])


def mark_state_stale(
    *,
    scan_job_id: str,
    page_id: str,
    reason: str,
) -> None:
    states = ScanTailorStageState.objects.filter(scan_job_id=scan_job_id, scan_page_id=page_id)
    for state in states:
        state.status = ScanTailorStageState.StageStatus.INVALIDATED
        state.last_error = reason
        state.save(update_fields=["status", "last_error", "updated_at"])


def _set_inputs_from_page(state: ScanTailorStageState, scan_page: ScanPage) -> None:
    active_version = scan_page.active_version()
    if active_version is None:
        return
    state.input_scan_page_version = active_version
    state.input_storage_identifier = active_version.storage_identifier
    predecessor = previous_stage(state.stage)
    if predecessor is None:
        state.input_stage_state = None


class ImageLoadError(RuntimeError):
    """Raised when a stage input cannot be decoded as an image."""


DEFAULT_DPI: Final[int] = 300


def _flatten_on_white(image: Image.Image) -> Image.Image:
    """Composite an image with an alpha channel onto an opaque white background."""
    rgba = image.convert("RGBA")
    background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
    background.alpha_composite(rgba)
    flattened = background.convert("RGB")
    if image.mode in {"LA", "La"}:
        return flattened.convert("L")
    return flattened


def _high_depth_to_uint8(image: Image.Image) -> Image.Image:
    """Scale 16/32-bit single-channel images down to 8-bit instead of clamping."""
    array = np.asarray(image)
    if array.dtype == np.uint8:
        return Image.fromarray(array, mode="L")
    if np.issubdtype(array.dtype, np.floating):
        peak = float(array.max()) if array.size else 0.0
        scaled = array * 255.0 if peak <= 1.0 else array
        return Image.fromarray(np.clip(scaled, 0, 255).astype(np.uint8), mode="L")
    peak_int = int(array.max()) if array.size else 0
    if peak_int <= 255:
        return Image.fromarray(array.astype(np.uint8), mode="L")
    scale = 65535.0 if peak_int <= 65535 else float(peak_int)
    scaled = array.astype(np.float64) * (255.0 / scale)
    return Image.fromarray(np.clip(np.rint(scaled), 0, 255).astype(np.uint8), mode="L")


def _sanitize_dpi(raw: object) -> tuple[int, int] | None:
    if not isinstance(raw, tuple | list) or len(raw) < 2:
        return None
    try:
        x_dpi, y_dpi = float(raw[0]), float(raw[1])
    except (TypeError, ValueError):
        return None
    if not (36 <= x_dpi <= 4800 and 36 <= y_dpi <= 4800):
        return None
    return int(round(x_dpi)), int(round(y_dpi))


def _normalise_image(image: Image.Image) -> Image.Image:
    """Return an 8-bit ``L`` or ``RGB`` image, whatever the source mode was."""
    mode = image.mode
    if mode in {"L", "RGB"}:
        return image
    if mode in {"I;16", "I;16L", "I;16B", "I;16N", "I", "F"}:
        return _high_depth_to_uint8(image)
    if mode == "1":
        return image.convert("L")
    if mode in {"RGBA", "LA", "La", "RGBa", "PA"}:
        return _flatten_on_white(image)
    if mode == "P":
        if "transparency" in image.info:
            return _flatten_on_white(image.convert("RGBA"))
        return image.convert("RGB")
    return image.convert("RGB")


def _load_image(path: Path) -> Image.Image:
    """Decode ``path`` into a normalised 8-bit L/RGB image or raise ImageLoadError.

    Shared by every stage: applies EXIF orientation, scales 16-bit data to 8-bit,
    converts CMYK/palette/YCbCr to RGB and flattens transparency onto white.
    Source DPI (if any) is kept in ``image.info["dpi"]``.
    """
    try:
        with Image.open(path) as opened:
            opened.load()
            dpi = _sanitize_dpi(opened.info.get("dpi"))
            oriented = ImageOps.exif_transpose(opened)
            image = _normalise_image(oriented)
            if image is opened:
                image = opened.copy()
    except (OSError, UnidentifiedImageError, ValueError, SyntaxError) as exc:
        raise ImageLoadError(f"cannot read image {path.name}: {exc}") from exc
    image.info["dpi"] = dpi or (DEFAULT_DPI, DEFAULT_DPI)
    return image


def _as_image_if_available(path: Path) -> tuple[Image.Image | None, bool]:
    try:
        return _load_image(path), True
    except ImageLoadError:
        return None, False


def _dpi_of(image: Image.Image) -> tuple[int, int]:
    return _sanitize_dpi(image.info.get("dpi")) or (DEFAULT_DPI, DEFAULT_DPI)


def _save_png(image: Image.Image, path: Path, dpi: tuple[int, int]) -> None:
    image.save(path, format="PNG", dpi=dpi)


def _paper_fill(image: Image.Image) -> int | tuple[int, int, int]:
    """Background colour for new pixels: bright paper level of the image itself."""
    small = _thumbnail(image, size=300)
    array = np.asarray(small)
    if array.ndim == 2:
        return int(np.percentile(array, 90))
    levels = np.percentile(array.reshape(-1, array.shape[2]), 90, axis=0)
    return (int(levels[0]), int(levels[1]), int(levels[2]))


def _thumbnail(image: Image.Image, size: int = 1200) -> Image.Image:
    if image.width <= size and image.height <= size:
        return image
    copy = image.copy()
    copy.thumbnail((size, size))
    return copy


def _save_preview(source: Image.Image, preview_path: Path) -> None:
    with source.copy() as preview_image:
        preview_image = preview_image.convert("RGB")
        preview_image = _thumbnail(preview_image, size=960)
        preview_image.save(preview_path, format="PNG")


def _passthrough(source_path: Path, output_path: Path, preview_path: Path) -> dict[str, Any]:
    """Copy a stage input through unchanged (re-encoded as normalised PNG)."""
    image = _load_image(source_path)
    _save_png(image, output_path, _dpi_of(image))
    _save_preview(image, preview_path)
    return {"algorithm": "copy"}


def _copy_fallback(source_path: Path, output_path: Path, preview_path: Path) -> dict[str, Any]:
    return _passthrough(source_path, output_path, preview_path)


def _record_image_dimensions(
    source_path: Path,
    output_path: Path,
    parameters: dict[str, Any],
) -> dict[str, Any]:
    """Record current input/output sizes; always overwrite so they never go stale."""
    recorded = dict(parameters)
    source, source_loaded = _as_image_if_available(source_path)
    if source is not None and source_loaded:
        recorded["input_width"] = source.width
        recorded["input_height"] = source.height
        source.close()
    output, output_loaded = _as_image_if_available(output_path)
    if output is not None and output_loaded:
        recorded["output_width"] = output.width
        recorded["output_height"] = output.height
        output.close()
    return recorded


def _run_copy(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    del state
    return _copy_fallback(source_path, output_path, preview_path)


@dataclass(frozen=True)
class SplitResult:
    x: int
    confidence: float
    method: str


MIN_SPLIT_CONFIDENCE: Final[float] = 0.08


def _column_darkness_profile(gray: np.ndarray) -> np.ndarray:
    """Per-column mean darkness in 0..1, normalised to the image's own paper/ink range."""
    height = gray.shape[0]
    rows = gray[int(height * 0.05) : max(int(height * 0.95), 1)]
    paper = float(np.percentile(rows, 99))
    ink = float(np.percentile(rows, 1))
    span = paper - ink
    if span < 20.0:
        return np.zeros(gray.shape[1], dtype=np.float64)
    darkness = np.clip((paper - rows.astype(np.float32)) / span, 0.0, 1.0)
    return np.asarray(darkness.mean(axis=0), dtype=np.float64)


def _find_gutter(image: Image.Image) -> SplitResult | None:
    """Locate the page gutter in a spread.

    Works on the per-column darkness profile of the central band (30-70% of the
    width). A gutter shows up either as a narrow dark ridge (binding shadow) or as a
    narrow bright valley between two text blocks (flat scan). Whichever contrasts
    more with its surroundings wins; if neither does, no split is reported.
    """
    width, height = image.size
    if width < 96 or width <= height:
        return None
    scale = min(1.0, 1000.0 / width)
    gray = np.asarray(image.convert("L"))
    if scale < 1.0:
        gray = cv2.resize(
            gray, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA
        )
    small_w = gray.shape[1]
    profile = _column_darkness_profile(gray)
    smooth_w = max(1, int(small_w * 0.006))
    kernel = np.ones(smooth_w) / smooth_w
    profile = np.convolve(np.pad(profile, smooth_w, mode="edge"), kernel, mode="same")[
        smooth_w:-smooth_w
    ]

    inner = max(2, int(small_w * 0.04))
    outer = max(inner + 2, int(small_w * 0.12))
    lo = int(small_w * 0.30)
    hi = int(small_w * 0.70)
    best_strength = 0.0
    best_kind = ""
    candidates: list[tuple[float, str, int]] = []
    ridge = np.full(small_w, -1.0)
    valley = np.full(small_w, -1.0)
    for x in range(max(lo, outer), min(hi, small_w - outer)):
        neighbours = np.concatenate(
            (profile[x - outer : x - inner], profile[x + inner : x + outer + 1])
        )
        level = float(neighbours.mean())
        ridge[x] = float(profile[x]) - level
        valley[x] = level - float(profile[x])
    for kind, strength_map in (("ridge", ridge), ("valley", valley)):
        peak = float(strength_map.max())
        candidates.append((peak, kind, int(strength_map.argmax())))
    best_strength, best_kind, _ = max(candidates)
    if best_strength <= 0.0:
        return None
    strength_map = ridge if best_kind == "ridge" else valley
    # centre of the plateau of near-maximal responses (flat gaps are wide, not 1px)
    near = np.flatnonzero(strength_map >= best_strength * 0.9)
    # keep the run of indexes that contains the argmax
    peak_index = int(strength_map.argmax())
    run = [int(i) for i in near if abs(int(i) - peak_index) <= int(small_w * 0.05)]
    centre = (min(run) + max(run)) / 2.0 if run else float(peak_index)
    x_full = int(round(centre / (gray.shape[1] / width)))
    return SplitResult(x=x_full, confidence=round(best_strength, 4), method=best_kind)


def _auto_split_position(
    image: Image.Image,
    *,
    minimum_confidence: float = MIN_SPLIT_CONFIDENCE,
) -> int | None:
    result = _find_gutter(image)
    if result is None or result.confidence < minimum_confidence:
        return None
    width = image.width
    if not (width * 0.25 <= result.x <= width * 0.75):
        return None
    return result.x


def _run_page_split(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    source = _load_image(source_path)

    parameters = dict(state.parameters or {})
    split_mode = str(parameters.get("split_mode", "auto")).strip().lower()
    if split_mode not in {"auto", "manual", "single"}:
        split_mode = "auto"
    side = state.scan_page.side

    dpi = _dpi_of(source)
    if source.width <= source.height:
        _save_png(source, output_path, dpi)
        _save_preview(source, preview_path)
        return {
            "resolved_split_mode": "single",
            "source_width": source.width,
            "source_height": source.height,
        }

    split_x = None
    split_confidence: float | None = None
    split_method = split_mode
    if split_mode == "manual":
        split_x = _coerce_int(parameters.get("split_x"), -1)
        if split_x < 0:
            split_x = None
    elif split_mode == "single":
        split_x = None
    else:
        gutter = _find_gutter(source)
        if gutter is not None:
            split_confidence = gutter.confidence
            split_method = gutter.method
            if (
                gutter.confidence >= MIN_SPLIT_CONFIDENCE
                and source.width * 0.25 <= gutter.x <= source.width * 0.75
            ):
                split_x = gutter.x
        else:
            split_confidence = 0.0
        if split_x is None:
            split_method = "center_fallback"

    resolved = split_mode
    output = source
    output_side = "single"
    split_detected = None

    if split_x is None:
        split_x = source.width // 2
        if split_x <= 0 or split_x >= source.width:
            split_x = None

    if split_x is not None and side in {ScanPage.Side.LEFT, ScanPage.Side.RIGHT}:
        split_x = max(1, min(source.width - 1, split_x))
        if side == ScanPage.Side.LEFT:
            output = source.crop((0, 0, split_x, source.height))
            output_side = "left"
        else:
            output = source.crop((split_x, 0, source.width, source.height))
            output_side = "right"
            resolved = "auto" if split_mode == "auto" else split_mode
        split_detected = split_x
    elif side == ScanPage.Side.LEFT:
        output = source.crop((0, 0, source.width // 2, source.height))
        output_side = "left-heuristic"
        split_detected = source.width // 2
        resolved = "heuristic-left"
    elif side == ScanPage.Side.RIGHT:
        output = source.crop((source.width // 2, 0, source.width, source.height))
        output_side = "right-heuristic"
        split_detected = source.width // 2
        resolved = "heuristic-right"
    else:
        split_detected = None

    with output as out:
        _save_png(out, output_path, dpi)
        _save_preview(out, preview_path)

    updates: dict[str, Any] = {
        "split_mode": split_mode,
        "resolved_split_side": output_side,
        "source_width": source.width,
        "source_height": source.height,
    }
    if split_confidence is not None:
        updates["split_confidence"] = split_confidence
        updates["split_method"] = split_method
    if split_detected is not None:
        updates["resolved_split_x"] = split_detected
        updates["resolved_width"] = output.width
    return _as_recorded_parameters(
        parameters,
        {
            "resolved_split_mode": resolved,
            **updates,
        },
    )


def _candidate_angles(start: float, stop: float, step: float) -> list[float]:
    if step <= 0:
        step = 1.0
    if start > stop:
        start, stop = stop, start
    values: list[float] = []
    current = start
    while current <= stop + 1e-6:
        values.append(round(current, 3))
        current += step
    return values


def _ink_mask(gray: np.ndarray) -> np.ndarray | None:
    """Boolean ink mask using a per-image Otsu threshold, or None for blank pages."""
    low, high = np.percentile(gray, [0.5, 99.5])
    if high - low < max(6.0, 0.12 * float(high)):
        return None
    threshold, _ = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    dark = gray[gray <= threshold]
    light = gray[gray > threshold]
    if dark.size == 0 or light.size == 0:
        return None
    if float(light.mean()) - float(dark.mean()) < max(6.0, 0.2 * float(light.mean())):
        return None
    return np.asarray(gray <= threshold)


def _rotated_row_score(mask: np.ndarray, angle: float) -> float:
    height, width = mask.shape
    matrix = cv2.getRotationMatrix2D((width / 2.0, height / 2.0), angle, 1.0)
    rotated = cv2.warpAffine(mask, matrix, (width, height), flags=cv2.INTER_LINEAR, borderValue=0)
    return float(np.var(rotated.sum(axis=1, dtype=np.float64)))


def _estimate_deskew_angle(image: Image.Image, parameters: dict[str, Any]) -> float:
    """Return the CCW rotation (degrees, PIL ``rotate`` convention) that straightens text.

    The returned value is exactly what ``_run_deskew`` applies and what the UI seeds
    ``manual_angle`` with, so auto and manual agree. 0.0 is returned for blank or
    ambiguous pages (tie / negligible improvement).
    """
    min_angle = _coerce_float(parameters.get("angle_min", -7.0), -7.0)
    max_angle = _coerce_float(parameters.get("angle_max", 7.0), 7.0)
    step = _coerce_float(parameters.get("angle_step", 1.0), 1.0)
    if step <= 0:
        step = 1.0

    gray = np.asarray(_thumbnail(image.convert("L"), size=900))
    mask = _ink_mask(gray)
    if mask is None:
        return 0.0
    mask_u8 = mask.astype(np.uint8)

    def score(angle: float) -> float:
        return _rotated_row_score(mask_u8, angle)

    baseline = score(0.0)
    best_angle = 0.0
    best_score = baseline
    for angle in _candidate_angles(min_angle, max_angle, step):
        value = score(angle)
        if value > best_score * 1.0001 and value > best_score:
            best_score = value
            best_angle = angle
    # refine around the coarse optimum with a finer step
    fine = step / 4.0
    if best_angle != 0.0 or best_score > baseline:
        for angle in _candidate_angles(best_angle - step, best_angle + step, fine):
            if not (min_angle <= angle <= max_angle):
                continue
            value = score(angle)
            if value > best_score:
                best_score = value
                best_angle = angle
    if best_score < baseline * 1.02:
        return 0.0
    return float(round(best_angle, 3))


def _run_deskew(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    source = _load_image(source_path)

    parameters = dict(state.parameters or {})

    manual = parameters.get("manual_angle")
    if manual is not None:
        angle = _coerce_float(manual, 0.0)
        mode = "manual"
    else:
        angle = _estimate_deskew_angle(source, parameters)
        mode = "auto"

    if abs(angle) < 1e-9:
        output = source
    else:
        output = source.rotate(
            angle,
            resample=Image.Resampling.BICUBIC,
            expand=True,
            fillcolor=_paper_fill(source),
        )
    _save_png(output, output_path, _dpi_of(source))
    _save_preview(output, preview_path)

    return _as_recorded_parameters(
        parameters,
        {
            "deskew_mode": mode,
            "angle": angle,
        },
    )


def _run_select_content(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    source = _load_image(source_path)

    parameters = dict(state.parameters or {})
    mode = str(parameters.get("content_mode", "auto")).strip().lower()
    if mode not in {"auto", "manual"}:
        mode = "auto"
    explicit_box = parameters.get("content_box")
    if mode == "manual":
        detected_box = _coerce_box(
            {
                "x1": parameters.get("x1"),
                "y1": parameters.get("y1"),
                "x2": parameters.get("x2"),
                "y2": parameters.get("y2"),
                "left": parameters.get("left"),
                "top": parameters.get("top"),
                "right": parameters.get("right"),
                "bottom": parameters.get("bottom"),
                "width": parameters.get("width"),
                "height": parameters.get("height"),
            },
            explicit_box,
            source.size,
        )
    else:
        detected_box = None
    if detected_box is None:
        detected_box = _detect_content_box(source)
        mode = "auto"

    left, top, right, bottom = detected_box
    output = source.crop((left, top, right, bottom))
    with output as out:
        _save_png(out, output_path, _dpi_of(source))
        _save_preview(out, preview_path)

    return _as_recorded_parameters(
        parameters,
        {
            "content_mode": mode,
            "content_box": {
                "x1": left,
                "y1": top,
                "x2": right,
                "y2": bottom,
                "width": right - left,
                "height": bottom - top,
            },
            "content_area_width": right - left,
            "content_area_height": bottom - top,
            "source_width": source.width,
            "source_height": source.height,
        },
    )


def _coerce_box(
    explicit: dict[str, Any],
    legacy_payload: Any,
    image_size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    values: tuple[int, int, int, int] | None = None

    if isinstance(legacy_payload, (tuple, list)) and len(legacy_payload) >= 4:
        values = (
            _coerce_int(legacy_payload[0], 0),
            _coerce_int(legacy_payload[1], 0),
            _coerce_int(legacy_payload[2], 0),
            _coerce_int(legacy_payload[3], 0),
        )
    elif isinstance(legacy_payload, dict):
        x1 = _coerce_int(legacy_payload.get("x1"), 0)
        y1 = _coerce_int(legacy_payload.get("y1"), 0)
        x2 = _coerce_int(legacy_payload.get("x2"), 0)
        y2 = _coerce_int(legacy_payload.get("y2"), 0)
        width = _coerce_int(legacy_payload.get("width"), 0)
        height = _coerce_int(legacy_payload.get("height"), 0)
        if x2 <= 0 and width > 0:
            x2 = x1 + width
        if y2 <= 0 and height > 0:
            y2 = y1 + height
        values = (x1, y1, x2, y2)

    if values is None:
        left = _coerce_int(explicit.get("left"), 0)
        top = _coerce_int(explicit.get("top"), 0)
        right = _coerce_int(explicit.get("right"), 0)
        bottom = _coerce_int(explicit.get("bottom"), 0)
        width = _coerce_int(explicit.get("width"), 0)
        height = _coerce_int(explicit.get("height"), 0)
        if right <= 0 and width > 0:
            right = left + width
        if bottom <= 0 and height > 0:
            bottom = top + height
        values = (left, top, right, bottom)

    if values is None:
        return None

    left, top, right, bottom = values
    width_px, height_px = image_size
    left = max(0, min(left, width_px))
    top = max(0, min(top, height_px))
    right = max(0, min(right, width_px))
    bottom = max(0, min(bottom, height_px))
    if right <= left:
        right = left + min(width_px - left, 1)
    if bottom <= top:
        bottom = top + min(height_px - top, 1)
    return (left, top, right, bottom)


def _detect_content_box(image: Image.Image) -> tuple[int, int, int, int]:
    """Bounding box of real content: per-image Otsu threshold, specks filtered out."""
    width, height = image.size
    full = (0, 0, width, height)
    scale = min(1.0, 1600.0 / max(width, height))
    gray = np.asarray(image.convert("L"))
    if scale < 1.0:
        gray = cv2.resize(
            gray,
            (max(1, int(width * scale)), max(1, int(height * scale))),
            interpolation=cv2.INTER_AREA,
        )
    mask = _ink_mask(gray)
    if mask is None:
        return full
    offset_x = offset_y = 0
    small_h, small_w = mask.shape
    ring = max(2, int(min(small_h, small_w) * 0.02))
    ring_mask = np.ones_like(mask)
    ring_mask[ring:-ring, ring:-ring] = False
    if float(mask[ring_mask].mean()) > 0.6:
        # Dark surroundings (table, scanner lid): ink is only meaningful on the page,
        # so restrict the search to the largest light region first.
        light = cv2.morphologyEx(
            (~mask).astype(np.uint8),
            cv2.MORPH_OPEN,
            np.ones((5, 5), np.uint8),
        )
        count, _, stats, _ = cv2.connectedComponentsWithStats(light, connectivity=4)
        if count <= 1:
            return full
        biggest = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
        px, py, pw, ph = (int(stats[biggest, i]) for i in range(4))
        inset_x, inset_y = max(1, int(pw * 0.01)), max(1, int(ph * 0.01))
        offset_x, offset_y = px + inset_x, py + inset_y
        region = gray[offset_y : py + ph - inset_y, offset_x : px + pw - inset_x]
        if region.size == 0:
            return full
        mask = _ink_mask(region)
        if mask is None:
            return full
    mask_u8 = mask.astype(np.uint8)
    small_h, small_w = mask_u8.shape
    # Group neighbouring glyphs, then drop groups with too little ink (dust specks).
    grow = max(3, small_w // 150) | 1
    grouped = cv2.dilate(mask_u8, np.ones((grow, grow), np.uint8))
    count, labels = cv2.connectedComponents(grouped, connectivity=8)
    if count <= 1:
        return full
    ink_per_label = np.bincount(labels[mask], minlength=count)
    min_ink = max(12, int(small_w * small_h * 2e-5))
    keep = ink_per_label >= min_ink
    keep[0] = False
    kept = keep[labels] & mask
    ys, xs = np.nonzero(kept)
    if ys.size == 0:
        return full
    inv = 1.0 / scale
    left = int((xs.min() + offset_x) * inv)
    right = int(min(width, (xs.max() + offset_x + 1) * inv + 1))
    top = int((ys.min() + offset_y) * inv)
    bottom = int(min(height, (ys.max() + offset_y + 1) * inv + 1))

    pad_x = max(1, (right - left) // 120)
    pad_y = max(1, (bottom - top) // 120)
    left = max(0, left - pad_x)
    top = max(0, top - pad_y)
    right = min(width, right + pad_x)
    bottom = min(height, bottom + pad_y)
    if right <= left:
        right = min(width, left + 1)
    if bottom <= top:
        bottom = min(height, top + 1)
    return (left, top, right, bottom)


def _run_margins(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    source = _load_image(source_path)

    parameters = dict(state.parameters or {})
    mode = str(parameters.get("margins_mode", "auto")).strip().lower()
    if mode not in {"auto", "manual"}:
        mode = "auto"

    default_margin = _coerce_int(parameters.get("margin"), 0, minimum=0)
    top = _coerce_int(parameters.get("top"), default_margin, minimum=0)
    right = _coerce_int(parameters.get("right"), default_margin, minimum=0)
    bottom = _coerce_int(parameters.get("bottom"), default_margin, minimum=0)
    left = _coerce_int(parameters.get("left"), default_margin, minimum=0)

    if default_margin:
        if top == default_margin:
            top = default_margin
        if right == default_margin:
            right = default_margin
        if bottom == default_margin:
            bottom = default_margin
        if left == default_margin:
            left = default_margin

    requested_width = _coerce_int(parameters.get("canvas_width"), source.width + left + right)
    requested_height = _coerce_int(parameters.get("canvas_height"), source.height + top + bottom)

    target_w = max(requested_width, source.width + left + right)
    target_h = max(requested_height, source.height + top + bottom)

    align_x = str(parameters.get("align_x", "center")).strip().lower()
    align_y = str(parameters.get("align_y", "center")).strip().lower()

    # The loader guarantees L or RGB; pad with the page's own paper colour.
    fill = _paper_fill(source)
    canvas = Image.new(source.mode, (target_w, target_h), fill)

    spare_x = max(0, target_w - (source.width + left + right))
    spare_y = max(0, target_h - (source.height + top + bottom))
    if spare_x < 0 or spare_y < 0:
        spare_x = max(0, spare_x)
        spare_y = max(0, spare_y)

    if align_x == "left":
        extra_x = 0
    elif align_x == "right":
        extra_x = spare_x
    else:
        extra_x = spare_x // 2

    if align_y == "top":
        extra_y = 0
    elif align_y == "bottom":
        extra_y = spare_y
    else:
        extra_y = spare_y // 2

    offset_x = left + extra_x
    offset_y = top + extra_y
    canvas.paste(source, (offset_x, offset_y))
    _save_png(canvas, output_path, _dpi_of(source))
    _save_preview(canvas, preview_path)

    return _as_recorded_parameters(
        parameters,
        {
            "margins_mode": mode,
            "margin_top": top,
            "margin_right": right,
            "margin_bottom": bottom,
            "margin_left": left,
            "align_x": align_x,
            "align_y": align_y,
            "canvas_width": target_w,
            "canvas_height": target_h,
        },
    )


def _ordered_quad_points(points: list[Point]) -> Quad:
    if len(points) != 4:
        raise ValueError("need four points")
    np_points = np.array(points, dtype=np.float32) if np is not None else []
    if np is None or np_points.shape != (4, 2):
        # Degrade gracefully to a stable deterministic ordering.
        ordered = sorted(points)
        return (
            ordered[0],
            ordered[1],
            ordered[3],
            ordered[2],
        )

    s = np_points.sum(axis=1)
    diff = np_points[:, 1] - np_points[:, 0]
    top_left = (
        int(np_points[np.argmin(s)][0]),
        int(np_points[np.argmin(s)][1]),
    )
    bottom_right = (
        int(np_points[np.argmax(s)][0]),
        int(np_points[np.argmax(s)][1]),
    )
    top_right = (
        int(np_points[np.argmin(diff)][0]),
        int(np_points[np.argmin(diff)][1]),
    )
    bottom_left = (
        int(np_points[np.argmax(diff)][0]),
        int(np_points[np.argmax(diff)][1]),
    )
    return (top_left, top_right, bottom_right, bottom_left)


def _coerce_dewarp_points(
    raw_points: Any,
    image_size: tuple[int, int],
) -> list[tuple[int, int]] | None:
    width, height = image_size
    values: list[Point] = []

    if isinstance(raw_points, dict):
        sequence: Sequence[Any] = (
            raw_points.get("tl"),
            raw_points.get("tr"),
            raw_points.get("br"),
            raw_points.get("bl"),
        )
    elif isinstance(raw_points, (list, tuple)):
        sequence = raw_points
    else:
        return None

    if len(sequence) != 4:
        return None

    for item in sequence:
        if isinstance(item, dict):
            x = item.get("x")
            y = item.get("y")
            if not isinstance(x, int | float) or not isinstance(y, int | float):
                return None
            values.append((_coerce_int(int(x), 0), _coerce_int(int(y), 0)))
            continue
        if not isinstance(item, (list, tuple)) or len(item) != 2:
            return None
        x = item[0]
        y = item[1]
        if not isinstance(x, int | float) or not isinstance(y, int | float):
            return None
        values.append((_coerce_int(int(x), 0), _coerce_int(int(y), 0)))

    values = [(max(0, min(x, width)), max(0, min(y, height))) for x, y in values]
    return values


def _auto_dewarp_points(
    image: Image.Image,
) -> tuple[tuple[int, int], tuple[int, int], tuple[int, int], tuple[int, int]] | None:
    if cv2 is None or np is None:
        return None
    grayscale = np.array(image.convert("L"))
    blurred = cv2.GaussianBlur(grayscale, (5, 5), 0)
    edges = cv2.Canny(blurred, 50, 150)
    contours, _ = cv2.findContours(edges, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    width, height = image.size
    min_area = (width * height) * 0.15
    best: list[Point] | None = None

    for contour in sorted(contours, key=cv2.contourArea, reverse=True):
        area = cv2.contourArea(contour)
        if area < min_area:
            continue
        perimeter = cv2.arcLength(contour, True)
        if perimeter <= 0:
            continue
        approximated = cv2.approxPolyDP(contour, 0.02 * perimeter, True)
        if len(approximated) < 4:
            continue
        if len(approximated) > 4:
            hull = cv2.convexHull(approximated)
            approx = cv2.approxPolyDP(hull, 0.02 * perimeter, True)
            if len(approx) != 4:
                continue
            candidates = [(int(point[0][0]), int(point[0][1])) for point in approx]
            best = candidates
            break
        candidates = [(int(point[0][0]), int(point[0][1])) for point in approximated]
        best = candidates
        break

    if best is None:
        return None
    ordered = _ordered_quad_points(best)
    return ordered


def _estimate_target_dimensions(
    quad: Quad,
) -> tuple[int, int]:
    top_left, top_right, bottom_right, bottom_left = quad

    width_bottom = hypot(bottom_right[0] - bottom_left[0], bottom_right[1] - bottom_left[1])
    width_top = hypot(top_right[0] - top_left[0], top_right[1] - top_left[1])
    width = max(1, int(round(max(width_top, width_bottom))))

    height_right = hypot(top_right[0] - bottom_right[0], top_right[1] - bottom_right[1])
    height_left = hypot(top_left[0] - bottom_left[0], top_left[1] - bottom_left[1])
    height = max(1, int(round(max(height_left, height_right))))
    return width, height


def _run_perspective_dewarp(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    parameters = dict(state.parameters or {})
    mode = str(parameters.get("dewarp_mode", "auto")).strip().lower()
    if mode not in {"auto", "manual", "bypass"}:
        mode = "auto"

    if mode == "bypass":
        return _copy_fallback(source_path, output_path, preview_path) | {
            "dewarp_mode": "bypass",
        }

    source = _load_image(source_path)

    if cv2 is None or np is None:
        if mode == "manual":
            resolved_points = _coerce_dewarp_points(parameters.get("dewarp_points"), source.size)
            fallback_payload = _copy_fallback(source_path, output_path, preview_path) | {
                "dewarp_mode": "manual",
            }
            if resolved_points is not None:
                fallback_payload["dewarp_source_points"] = _ordered_quad_points(resolved_points)
                fallback_payload["dewarp_warning"] = "missing_library_fallback"
            return fallback_payload
        if mode == "bypass":
            return _copy_fallback(source_path, output_path, preview_path) | {
                "dewarp_mode": "bypass",
            }
        return _copy_fallback(source_path, output_path, preview_path) | {
            "dewarp_mode": "missing_library_fallback",
        }

    manual_points = _coerce_dewarp_points(
        parameters.get("dewarp_points"),
        source.size,
    )

    source_quad: Quad | None
    if mode == "manual" and manual_points is not None:
        source_quad = _ordered_quad_points(manual_points)
    else:
        source_quad = _auto_dewarp_points(source)
        if source_quad is None:
            return _copy_fallback(source_path, output_path, preview_path) | {
                "dewarp_mode": "auto_fallback",
            }

    try:
        width, height = _estimate_target_dimensions(source_quad)
        src = np.array(source.convert("RGB"))
        paper = _paper_fill(source.convert("RGB"))
        source_matrix = np.array(source_quad, dtype=np.float32)
        destination_matrix = np.array(
            [
                [0, 0],
                [width - 1, 0],
                [width - 1, height - 1],
                [0, height - 1],
            ],
            dtype=np.float32,
        )
        transform = cv2.getPerspectiveTransform(source_matrix, destination_matrix)
        warped = cv2.warpPerspective(
            src,
            transform,
            (width, height),
            flags=cv2.INTER_CUBIC,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=paper,
        )
        output = Image.fromarray(warped)
        _save_png(output, output_path, _dpi_of(source))
        _save_preview(output, preview_path)
        recorded_mode = "manual" if mode == "manual" else "auto"
        return {
            "dewarp_mode": recorded_mode,
            "dewarp_source_points": source_quad,
            "dewarp_width": width,
            "dewarp_height": height,
        }
    except Exception as exc:  # pragma: no cover - geometry failures
        return _copy_fallback(source_path, output_path, preview_path) | {
            "dewarp_mode": "processing_error_fallback",
            "dewarp_error": str(exc),
        }


def _run_dewarping(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    return _run_perspective_dewarp(
        state,
        source_path,
        output_path,
        preview_path,
    )


def _coerce_output_mode(value: object, default: str = "otsu") -> str:
    mode = str(value).strip().lower()
    if mode in {"otsu", "sauvola", "wolf", "fox", "window", "grad", "copy"}:
        return mode
    if mode in {"adaptiveta", "adaptive", "window_threshold"}:
        return "window"
    return default


def _normalize_zone_mode(value: object, default: str = "auto") -> str:
    mode = str(value).strip().lower()
    return mode if mode in {"auto", "manual"} else default


def _zone_rectangles(
    parameters: dict[str, Any],
    image_size: tuple[int, int],
) -> dict[str, Box | None]:
    text_box = _coerce_box(
        {},
        parameters.get("text_box"),
        image_size,
    )
    picture_box = _coerce_box(
        {},
        parameters.get("picture_box"),
        image_size,
    )
    return {
        "text": text_box,
        "picture": picture_box,
    }


def _pil_otsu_threshold(image: Image.Image) -> int:
    grayscale = image.convert("L")
    histogram = grayscale.histogram()
    total = sum(histogram)
    if total == 0:
        return 128

    cumulative_count = 0
    cumulative_sum = 0
    total_weighted_sum = sum(value * count for value, count in enumerate(histogram))
    best = 0.0
    best_threshold = 0

    for value, count in enumerate(histogram):
        cumulative_count += count
        cumulative_sum += value * count
        if cumulative_count == 0 or cumulative_count == total:
            continue

        background_weight = cumulative_count
        foreground_weight = total - cumulative_count

        mean_background = cumulative_sum / background_weight
        mean_foreground = (total_weighted_sum - cumulative_sum) / foreground_weight

        between = background_weight * foreground_weight * (mean_background - mean_foreground) ** 2
        if between > best:
            best = between
            best_threshold = value
    return best_threshold


def _odd_window(window_size: int) -> int:
    block = max(3, window_size)
    return block if block % 2 == 1 else block + 1


def _binarize_with_cv2(
    grayscale: Any,
    source: Image.Image,
    mode: str,
    *,
    window_size: int,
    k: float,
    r: float,
    invert: bool,
) -> Image.Image:
    if cv2 is None or np is None:
        raise RuntimeError("opencv is not available")

    gray = grayscale
    if not isinstance(gray, np.ndarray):
        gray = np.array(source.convert("L"))

    if mode == "otsu":
        _, threshold = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        output = threshold
    elif mode == "window":
        # Local-mean threshold with a *relative* offset below the mean: paper noise
        # stays white, only pixels clearly darker than their surroundings turn black.
        block = _odd_window(window_size)
        local_mean = cv2.blur(gray.astype(np.float32), (block, block))
        threshold = local_mean * (1.0 - min(max(k, 0.0), 1.0) * 0.5)
        output = (gray.astype(np.float32) > threshold).astype(np.uint8) * 255
    elif mode == "fox":
        # Niblack-style (mean - k*std) with a floor on the offset so flat, noisy
        # paper (std ~ 0) is not split in half by noise around its own mean.
        block = _odd_window(window_size)
        gray_f = gray.astype(np.float32)
        local_mean = cv2.blur(gray_f, (block, block))
        local_sq = cv2.blur(gray_f * gray_f, (block, block))
        local_std = np.sqrt(np.maximum(local_sq - local_mean * local_mean, 0.0))
        offset = np.maximum(abs(k) * local_std, 0.12 * local_mean)
        output = (gray_f > (local_mean - offset)).astype(np.uint8) * 255
    elif mode == "sauvola":
        # T = m * (1 + k * (s / r - 1)); box filters keep this fast on 24MP scans.
        block = _odd_window(window_size)
        gray_f = gray.astype(np.float32)
        local_mean = cv2.blur(gray_f, (block, block))
        local_sq = cv2.blur(gray_f * gray_f, (block, block))
        local_std = np.sqrt(np.maximum(local_sq - local_mean * local_mean, 0.0))
        threshold = local_mean * (1.0 + k * (local_std / max(r, 1.0) - 1.0))
        output = (gray_f > threshold).astype(np.uint8) * 255
    elif mode == "wolf":
        # Wolf-Jolion: T = (1-a)*m + a*Min + a*(s/Smax)*(m - Min)
        block = _odd_window(window_size)
        gray_f = gray.astype(np.float32)
        local_mean = cv2.blur(gray_f, (block, block))
        local_sq = cv2.blur(gray_f * gray_f, (block, block))
        local_std = np.sqrt(np.maximum(local_sq - local_mean * local_mean, 0.0))
        floor = float(np.percentile(gray_f[::4, ::4], 1))
        max_std = max(float(local_std.max()), 1e-3)
        a_coeff = min(max(k, 0.0), 1.0)
        threshold = (
            (1.0 - a_coeff) * local_mean
            + a_coeff * floor
            + a_coeff * (local_std / max_std) * (local_mean - floor)
        )
        output = (gray_f > threshold).astype(np.uint8) * 255
    elif mode == "grad":
        # Gradient-aware fallback for bleed-through and uneven lighting.
        blurred = cv2.GaussianBlur(gray, (3, 3), 0)
        gx = cv2.Sobel(blurred, cv2.CV_32F, 1, 0, ksize=3)
        gy = cv2.Sobel(blurred, cv2.CV_32F, 0, 1, ksize=3)
        gradient = cv2.magnitude(gx, gy)
        normalized = cv2.normalize(  # type: ignore[call-overload]
            np.asarray(gradient, dtype=np.float32),
            None,
            alpha=0,
            beta=255,
            norm_type=cv2.NORM_MINMAX,
        )
        _, gradient_mask = cv2.threshold(
            normalized.astype(np.uint8),
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
        _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        output = cv2.bitwise_xor(binary, gradient_mask)
    else:
        raise RuntimeError(f"unsupported output mode '{mode}'")

    if invert:
        output = cv2.bitwise_not(output)
    return Image.fromarray(output.astype(np.uint8))


def _run_output(
    state: ScanTailorStageState,
    source_path: Path,
    output_path: Path,
    preview_path: Path,
) -> dict[str, Any]:
    source = _load_image(source_path)

    parameters = dict(state.parameters or {})
    output_mode = _coerce_output_mode(parameters.get("output_mode"), default="otsu")
    keep_color = _coerce_bool(parameters.get("keep_color"), default=False)
    invert = _coerce_bool(parameters.get("invert"), default=False)
    zone_mode = _normalize_zone_mode(parameters.get("zone_mode"), default="auto")
    warning: str | None = None

    output_size = source.size
    grayscale = source.convert("L")

    if output_mode == "copy":
        _copy_fallback(source_path, output_path, preview_path)
        return {
            "output_mode": "copy",
            "output_width": output_size[0],
            "output_height": output_size[1],
        }

    window_size = _coerce_int(parameters.get("window_size"), 35, minimum=3)
    k = _coerce_float(parameters.get("k"), 0.33)
    r = _coerce_float(parameters.get("r"), 128.0)
    text_algorithm = _coerce_output_mode(
        parameters.get("text_algorithm"),
        default=output_mode,
    )
    picture_algorithm = _coerce_output_mode(
        parameters.get("picture_algorithm"),
        default=output_mode,
    )

    try:
        if cv2 is None or np is None:
            raise RuntimeError("opencv missing")

        if zone_mode == "manual":
            zones = _zone_rectangles(parameters, source.size)
            text_box = zones["text"]
            picture_box = zones["picture"]

            if text_box is not None:
                region = source.crop(text_box)
                region_output = _binarize_with_cv2(
                    np.array(region.convert("L")),
                    region,
                    text_algorithm,
                    window_size=window_size,
                    k=k,
                    r=r,
                    invert=invert,
                )
            else:
                region_output = None

            if picture_box is not None:
                region = source.crop(picture_box)
                picture_output = _binarize_with_cv2(
                    np.array(region.convert("L")),
                    region,
                    picture_algorithm,
                    window_size=window_size,
                    k=k,
                    r=r,
                    invert=invert,
                )
            else:
                picture_output = None

            full_output = _binarize_with_cv2(
                np.array(grayscale),
                grayscale,
                output_mode,
                window_size=window_size,
                k=k,
                r=r,
                invert=invert,
            )
            result = full_output
            if region_output is not None and text_box is not None:
                result.paste(region_output, text_box)
            if picture_output is not None and picture_box is not None:
                result.paste(picture_output, picture_box)
        else:
            result = _binarize_with_cv2(
                np.array(grayscale),
                grayscale,
                output_mode,
                window_size=window_size,
                k=k,
                r=r,
                invert=invert,
            )
    except Exception as exc:  # pragma: no cover - geometry and library edge cases
        warning = str(exc)
        fallback_threshold = _coerce_int(_pil_otsu_threshold(grayscale), 128)
        result = grayscale.point(
            lambda value, cutoff=fallback_threshold: 255 if value > cutoff else 0,
        )

    if not keep_color and result.mode != "L":
        result = result.convert("L")
    if keep_color and result.mode != "RGB":
        result = result.convert("RGB")

    _save_png(result, output_path, _dpi_of(source))
    _save_preview(result, preview_path)
    output_payload: dict[str, Any] = {
        "output_mode": output_mode,
        "output_width": output_size[0],
        "output_height": output_size[1],
    }
    if zone_mode == "manual":
        zones = _zone_rectangles(parameters, source.size)
        if zones["text"] is not None:
            output_payload["text_box"] = list(zones["text"])
        if zones["picture"] is not None:
            output_payload["picture_box"] = list(zones["picture"])
        output_payload["text_algorithm"] = text_algorithm
        output_payload["picture_algorithm"] = picture_algorithm
        output_payload["zone_mode"] = "manual"
    else:
        output_payload["zone_mode"] = "auto"
    if warning is not None:
        output_payload["output_warning"] = warning
    return output_payload


_STAGE_RUNNERS: dict[
    str,
    dict[
        str,
        Callable[
            [
                ScanTailorStageState,
                Path,
                Path,
                Path,
            ],
            dict[str, Any],
        ],
    ],
] = {
    ScanTailorStageState.Stage.PAGE_SPLIT.value: {
        "copy": _run_copy,
        "auto_split": _run_page_split,
        "manual_split": _run_page_split,
    },
    ScanTailorStageState.Stage.DESKEW.value: {
        "copy": _run_copy,
        "projection_deskew": _run_deskew,
        "manual_deskew": _run_deskew,
    },
    ScanTailorStageState.Stage.SELECT_CONTENT.value: {
        "content_box_projection": _run_select_content,
        "content_projection": _run_select_content,
        "copy": _run_select_content,
        "copy_detect": _run_select_content,
    },
    ScanTailorStageState.Stage.MARGINS.value: {
        "margin_pad": _run_margins,
        "copy": _run_margins,
        "copy_detect": _run_margins,
    },
    ScanTailorStageState.Stage.DEWARPING.value: {
        "projective_dewarp": _run_dewarping,
        "copy": _run_dewarping,
        "copy_detect": _run_dewarping,
    },
    ScanTailorStageState.Stage.OUTPUT.value: {
        "binarization_otsu": _run_output,
        "binarization_sauvola": _run_output,
        "binarization_wolf": _run_output,
        "binarization_fox": _run_output,
        "binarization_window": _run_output,
        "binarization_grad": _run_output,
        "binarization": _run_output,
        "copy": _run_output,
        "copy_detect": _run_output,
    },
}


def _resolve_stage_algorithm(
    state: ScanTailorStageState,
) -> Callable[
    [
        ScanTailorStageState,
        Path,
        Path,
        Path,
    ],
    dict[str, Any],
]:
    stage_algorithms = _STAGE_RUNNERS.get(state.stage, {})
    algorithm = (state.algorithm or "").strip()
    runner = stage_algorithms.get(algorithm)
    if runner is not None:
        return runner

    fallback = _default_algorithm(state.stage)
    runner = stage_algorithms.get(fallback)
    if runner is None:
        raise RuntimeError(f"unsupported stage algorithm '{state.stage}'")

    state.algorithm = fallback
    state.algorithm_version = state.algorithm_version or _default_algorithm_version(state.stage)
    state.save(update_fields=["algorithm", "algorithm_version", "updated_at"])
    return runner


def request_stage_job(
    *,
    state: ScanTailorStageState,
    requested_by_user_id: int | None = None,
) -> ProcessingJob:
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.STAGE,
        state=ProcessingJob.JobState.QUEUED,
        details={
            "scan_job_id": str(state.scan_job_id),
            "scan_tailor_stage_state_id": state.id,
            "requested_by_user_id": requested_by_user_id,
            "stage": state.stage,
        },
    )
    state.worker_job_id = str(job.job_id)
    state.status = ScanTailorStageState.StageStatus.QUEUED
    state.last_error = ""
    if not state.algorithm:
        state.algorithm = _default_algorithm(state.stage)
    if not state.algorithm_version:
        state.algorithm_version = _default_algorithm_version(state.stage)
    state.save(
        update_fields=[
            "worker_job_id",
            "status",
            "algorithm",
            "algorithm_version",
            "last_error",
            "updated_at",
        ],
    )
    dispatch_stage_job(str(job.job_id))
    return job


def dispatch_stage_job(job_id: str) -> None:
    queue = get_queue()
    if queue is None:
        process_stage_job(job_id)
        return
    queue.enqueue(process_stage_job, job_id)


def stage_job_payload(job: ProcessingJob) -> dict[str, Any]:
    details = job.details if isinstance(job.details, dict) else {}
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "scan_job_id": details.get("scan_job_id"),
        "stage_state_id": details.get("scan_tailor_stage_state_id"),
        "stage": details.get("stage", ""),
        "error": details.get("error", ""),
    }


def process_stage_job(job_id: str) -> None:
    try:
        job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except ProcessingJob.DoesNotExist:
        return

    if job.job_type != ProcessingJob.JobType.STAGE or job.state == ProcessingJob.JobState.SUCCESS:
        return

    details = job.details if isinstance(job.details, dict) else {}
    stage_state_id = details.get("scan_tailor_stage_state_id")
    if not isinstance(stage_state_id, int):
        _fail_stage_job(job, "missing stage state id")
        return

    state = (
        ScanTailorStageState.objects.select_related(
            "scan_job",
            "scan_page",
            "input_stage_state",
            "input_scan_page_version",
        )
        .filter(id=stage_state_id)
        .first()
    )
    if state is None:
        _fail_stage_job(job, f"stage state not found: {stage_state_id}")
        return

    if state.input_stage_state is not None:
        input_state = state.input_stage_state
        if input_state.status != ScanTailorStageState.StageStatus.SUCCESS:
            _fail_stage_job(job, f"upstream stage {input_state.stage} not complete")
            return

    now = timezone.now()
    job.state = ProcessingJob.JobState.RUNNING
    job.started_at = now
    started_revision = state.revision
    claimed = ScanTailorStageState.objects.filter(
        id=state.id,
        revision=started_revision,
    ).update(
        status=ScanTailorStageState.StageStatus.RUNNING,
        started_at=now,
        updated_at=now,
    )
    if not claimed:
        _supersede_stage_job(job, details, "stage revision changed before job start")
        return
    state.status = ScanTailorStageState.StageStatus.RUNNING
    state.started_at = now
    job.save(update_fields=["state", "started_at"])
    logger.info(
        "scantailor_stage_started",
        processing_job_id=str(job.job_id),
        scan_job_id=str(state.scan_job_id),
        stage=state.stage,
    )

    try:
        source_identifier = _resolve_input_identifier(state)
        if not source_identifier:
            raise RuntimeError("missing source artifact")
        storage = _scan_storage()
        source_path = storage.resolve_location(source_identifier)
        if not source_path.exists():
            raise RuntimeError(f"source missing: {source_identifier}")

        storage_location = _scan_storage_location()
        artifact_identifier = output_identifier(state)
        preview_identifier = output_identifier(state, preview=True)

        runner = _resolve_stage_algorithm(state)
        with tempfile.TemporaryDirectory(prefix="stages-") as tempdir:
            staged_output = Path(tempdir) / "output.png"
            staged_preview = Path(tempdir) / "preview.png"
            output_updates = runner(
                state,
                source_path,
                staged_output,
                staged_preview,
            )
            if not staged_output.exists() or not staged_preview.exists():
                raise RuntimeError("processing did not produce expected outputs")
            output_params = output_updates if isinstance(output_updates, dict) else {}
            output_params = _record_image_dimensions(
                source_path,
                staged_output,
                output_params,
            )
            if output_params:
                merged = dict(state.parameters or {})
                merged.update(output_params)
                state.parameters = merged
            if _locked_if_current(state.id, started_revision) is None:
                _supersede_stage_job(job, details, "stage edited while job was running")
                return
            storage.store(str(staged_output), artifact_identifier)
            storage.store(str(staged_preview), preview_identifier)

        artifact_path = storage.resolve_location(artifact_identifier)
        output_size = artifact_path.stat().st_size
        output_sha = _compute_sha256(artifact_path)

        with transaction.atomic():
            if _locked_if_current(state.id, started_revision) is None:
                _supersede_stage_job(job, details, "stage edited while job was running")
                return
            state.output_storage_location = storage_location
            state.preview_storage_location = storage_location
            state.output_storage_identifier = artifact_identifier
            state.preview_storage_identifier = preview_identifier
            state.output_size_bytes = output_size
            state.output_sha256 = output_sha
            state.status = ScanTailorStageState.StageStatus.SUCCESS
            state.finished_at = timezone.now()
            state.last_error = ""
            state.save(
                update_fields=[
                    "output_storage_location",
                    "preview_storage_location",
                    "output_storage_identifier",
                    "preview_storage_identifier",
                    "output_size_bytes",
                    "output_sha256",
                    "status",
                    "finished_at",
                    "last_error",
                    "parameters",
                    "updated_at",
                ],
            )

        job.state = ProcessingJob.JobState.SUCCESS
        job.finished_at = timezone.now()
        job.details = {
            **details,
            "scan_job_id": str(state.scan_job_id),
            "output_storage_identifier": artifact_identifier,
            "preview_storage_identifier": preview_identifier,
            "output_storage_location_id": storage_location.id,
        }
        job.save(update_fields=["state", "finished_at", "details"])
        logger.info(
            "scantailor_stage_succeeded",
            processing_job_id=str(job.job_id),
            scan_job_id=str(state.scan_job_id),
            stage=state.stage,
            output_size_bytes=output_size,
        )
    except Exception as exc:
        with transaction.atomic():
            if _locked_if_current(state.id, started_revision) is not None:
                state.status = ScanTailorStageState.StageStatus.FAILED
                state.last_error = str(exc)
                state.finished_at = timezone.now()
                state.save(update_fields=["status", "last_error", "finished_at", "updated_at"])
        _fail_stage_job(job, str(exc))


def _locked_if_current(state_id: int, revision: int) -> ScanTailorStageState | None:
    """Lock the stage row; return it only if this job's run is still the current one.

    A user edit bumps ``revision`` (and re-queues the state); an upstream edit flips
    the status to INVALIDATED. In either case this job's result is stale and must not
    overwrite the newer parameters/status.
    """
    with transaction.atomic():
        fresh = ScanTailorStageState.objects.select_for_update().filter(id=state_id).first()
        if fresh is None:
            return None
        if fresh.revision != revision:
            return None
        if fresh.status != ScanTailorStageState.StageStatus.RUNNING:
            return None
        return fresh


def _supersede_stage_job(job: ProcessingJob, details: dict[str, Any], reason: str) -> None:
    """Close a job whose stage was edited meanwhile, leaving the stage row untouched."""
    job.state = ProcessingJob.JobState.SUCCESS
    job.finished_at = timezone.now()
    job.details = {**details, "superseded": True, "superseded_reason": reason}
    job.save(update_fields=["state", "finished_at", "details"])
    logger.info(
        "scantailor_stage_superseded",
        processing_job_id=str(job.job_id),
        stage=details.get("stage"),
        reason=reason,
    )


def _fail_stage_job(job: ProcessingJob, message: str) -> None:
    details = job.details if isinstance(job.details, dict) else {}
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = {**details, "error": message}
    job.save(update_fields=["state", "finished_at", "details"])
    logger.error(
        "scantailor_stage_failed",
        processing_job_id=str(job.job_id),
        stage=details.get("stage"),
    )
