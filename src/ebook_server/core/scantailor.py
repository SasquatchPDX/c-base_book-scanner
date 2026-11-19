"""ScanTailor-Web worker orchestration helpers."""

from __future__ import annotations

import hashlib
import tempfile
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from math import ceil, floor, isfinite
from pathlib import Path
from typing import Any, Final

import cv2
import numpy as np
import structlog
from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from ebook_server.jobs import get_queue
from ebook_server.storage import LocalFilesystemStorage

from . import (
    content_box,
    curvature,
    cylindrical,
    deskew,
    output_zones,
    scan_images,
    scan_transform,
)
from .models import (
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    ScanTailorStageState,
    StorageLocation,
)
from .storage_registry import filesystem_backend_for_location

logger = structlog.get_logger(__name__)

STAGE_NAMESPACE: Final[str] = "scan-jobs"
type Point = tuple[int, int]
type Quad = tuple[Point, Point, Point, Point]
type Box = tuple[int, int, int, int]

DEFAULT_STAGE_ALGORITHM: Final[dict[str, str]] = {
    "deskew": "skew_finder",
    "select_content": "content_box_finder",
    "margins": "margin_pad",
    "dewarping": "cylindrical_dewarp",
    "output": "binarization_otsu",
}

DEFAULT_STAGE_ALGORITHM_VERSION: Final[dict[str, str]] = {
    # v2 (#195 step 2): parameters + transform op, detection at working resolution.
    # v3 (#195 step 4): port of upstream SkewFinder; positive angles turn clockwise.
    "deskew": "skewfinder-v3",
    # v3 (#195 step 5): port of upstream ContentBoxFinder.
    "select_content": "contentboxfinder-v3",
    "margins": "padding-v2",
    # v2: the output canvas keeps the whole warped input instead of cropping to the quad.
    # v3: auto mode derives the quad from the text lines.
    # v4: transform op, detection at working resolution (#195 step 2).
    # v5 (#195 step 6a): upstream's cylindrical-surface model (CylindricalSurfaceDewarper).
    "dewarping": "cylindrical-v5",
    # v3: rendered once from the original through the composed transform.
    "output": "opencv-zones-v3",
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
    """Pipeline order (#195): Deskew, Select Content, Dewarping, Margins, Output."""
    return tuple(stage.value for stage in ScanTailorStageState.Stage)


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
    # Stages 1-4 keep a lossless working-resolution image (the next stage's input) and
    # a JPEG preview. Output keeps the full-resolution page and a PNG preview, so
    # binarised text stays crisp.
    if state.stage == ScanTailorStageState.Stage.OUTPUT.value:
        return "preview.png" if preview else "output.png"
    return "preview.jpg" if preview else "work.png"


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


def _default_algorithm_version(stage: str, algorithm: str | None = None) -> str:
    del algorithm  # every stage has one current version
    return DEFAULT_STAGE_ALGORITHM_VERSION.get(stage, "placeholder-v1")


@transaction.atomic
def get_or_create_stage_state(
    *,
    scan_job_id: str,
    page_id: str,
    stage: str,
) -> ScanTailorStageState:
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

    changed = _apply_stage_state_defaults(
        state,
        predecessor=predecessor,
        active_version=active_version,
    )
    if changed:
        # Only write when something actually changed: workspace loads call this for
        # every page/stage and must not turn into thousands of no-op UPDATEs (#51).
        state.save(update_fields=[*changed, "updated_at"])
    return state


_STAGE_STATE_DEFAULT_FIELDS: Final[tuple[str, ...]] = (
    "input_stage_state",
    "input_scan_page_version",
    "input_storage_identifier",
    "algorithm",
    "algorithm_version",
)


def _apply_stage_state_defaults(
    state: ScanTailorStageState,
    *,
    predecessor: ScanTailorStageState | None,
    active_version: ScanPageVersion | None,
) -> list[str]:
    """Bring ``state``'s input links/algorithm defaults up to date; return changed fields."""
    changed: list[str] = []
    predecessor_id = predecessor.id if predecessor is not None else None
    if state.input_stage_state_id != predecessor_id:
        state.input_stage_state = predecessor
        changed.append("input_stage_state")
    if predecessor is None and active_version is not None:
        if state.input_scan_page_version_id != active_version.id:
            state.input_scan_page_version = active_version
            changed.append("input_scan_page_version")
        if state.input_storage_identifier != active_version.storage_identifier:
            state.input_storage_identifier = active_version.storage_identifier
            changed.append("input_storage_identifier")
    if not state.algorithm:
        state.algorithm = _default_algorithm(state.stage)
        changed.append("algorithm")
    if not state.algorithm_version:
        state.algorithm_version = _default_algorithm_version(state.stage)
        changed.append("algorithm_version")
    return changed


@transaction.atomic
def ensure_stage_states_for_pages(
    scan_job: ScanJob,
    pages: Sequence[ScanPage],
    active_versions: dict[Any, ScanPageVersion | None],
) -> dict[Any, dict[str, ScanTailorStageState]]:
    """Bulk equivalent of ``get_or_create_stage_state`` for every stage of ``pages``.

    Uses a bounded number of queries independent of the page count: one read of the
    job's existing states, plus (only when rows are missing) one insert and one
    re-read per stage. Rows whose input links are already correct are not written.
    Rows that do need relinking (e.g. after a rescan) go through the locked
    single-row path, which is rare.

    ``active_versions`` maps page id -> active ScanPageVersion (callers usually have
    the versions prefetched). Returns page id -> {stage: state}.
    """
    page_ids = [page.id for page in pages]
    by_page: dict[Any, dict[str, ScanTailorStageState]] = {page_id: {} for page_id in page_ids}
    if not page_ids:
        return by_page

    def _load(stage: str | None = None) -> None:
        query = ScanTailorStageState.objects.filter(scan_job=scan_job, scan_page_id__in=page_ids)
        if stage is not None:
            query = query.filter(stage=stage)
        for existing in query:
            by_page[existing.scan_page_id][existing.stage] = existing

    _load()
    for stage in stage_order():
        predecessor_name = previous_stage(stage)
        missing: list[ScanTailorStageState] = []
        for page in pages:
            if stage in by_page[page.id]:
                continue
            predecessor = by_page[page.id].get(predecessor_name) if predecessor_name else None
            active_version = active_versions.get(page.id)
            first_input = predecessor is None and active_version is not None
            missing.append(
                ScanTailorStageState(
                    scan_job=scan_job,
                    scan_page=page,
                    stage=stage,
                    algorithm=_default_algorithm(stage),
                    algorithm_version=_default_algorithm_version(stage),
                    input_stage_state=predecessor,
                    input_scan_page_version=active_version if first_input else None,
                    input_storage_identifier=(
                        active_version.storage_identifier
                        if first_input and active_version is not None
                        else ""
                    ),
                ),
            )
        if missing:
            # ignore_conflicts: a concurrent request may have created some rows.
            ScanTailorStageState.objects.bulk_create(missing, ignore_conflicts=True)
            _load(stage)

    for page in pages:
        page_states = by_page[page.id]
        for stage in stage_order():
            state = page_states.get(stage)
            if state is None:
                continue
            predecessor_name = previous_stage(stage)
            predecessor = page_states.get(predecessor_name) if predecessor_name else None
            probe = ScanTailorStageState(
                stage=state.stage,
                input_stage_state_id=state.input_stage_state_id,
                input_scan_page_version_id=state.input_scan_page_version_id,
                input_storage_identifier=state.input_storage_identifier,
                algorithm=state.algorithm,
                algorithm_version=state.algorithm_version,
            )
            if _apply_stage_state_defaults(
                probe,
                predecessor=predecessor,
                active_version=active_versions.get(page.id),
            ):
                page_states[stage] = get_or_create_stage_state(
                    scan_job_id=str(scan_job.id),
                    page_id=str(page.id),
                    stage=stage,
                )
    return by_page


def _invalidate(states: Any, reason: str) -> int:
    """Mark ``states`` invalidated in one UPDATE (#195 §8.2 item 13).

    ``updated_at`` is set explicitly: ``QuerySet.update`` skips ``auto_now``, and the
    live workspace stream detects changes through it.
    """
    return int(
        states.update(
            status=ScanTailorStageState.StageStatus.INVALIDATED,
            invalidated_reason=reason,
            last_error="",
            updated_at=timezone.now(),
        )
    )


def invalidate_downstream_states(
    page: ScanPage,
    *,
    from_stage: str,
    reason: str,
) -> int:
    stages = downstream_stages(from_stage)
    if not stages:
        return 0
    return _invalidate(
        ScanTailorStageState.objects.filter(
            scan_job_id=page.scan_job_id, scan_page=page, stage__in=stages
        ),
        reason,
    )


def mark_state_stale(
    *,
    scan_job_id: str,
    page_id: str,
    reason: str,
) -> int:
    return _invalidate(
        ScanTailorStageState.objects.filter(scan_job_id=scan_job_id, scan_page_id=page_id),
        reason,
    )


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
    except (
        OSError,
        UnidentifiedImageError,
        ValueError,
        SyntaxError,
        Image.DecompressionBombError,
    ) as exc:
        raise ImageLoadError(f"cannot read image {path.name}: {exc}") from exc
    image.info["dpi"] = dpi or (DEFAULT_DPI, DEFAULT_DPI)
    return image


def _dpi_of(image: Image.Image) -> tuple[int, int]:
    return _sanitize_dpi(image.info.get("dpi")) or (DEFAULT_DPI, DEFAULT_DPI)


def _save_png(image: Image.Image, path: Path, dpi: tuple[int, int]) -> None:
    # Level 1: several times faster than the default and only slightly larger.
    image.save(path, format="PNG", dpi=dpi, compress_level=1)


_paper_fill = scan_transform.paper_fill


_thumbnail = scan_transform.thumbnail


@dataclass(frozen=True)
class StageInput:
    """What a stage runs on (#195 step 2).

    ``image`` is the stage input at working resolution: the page's working image,
    transformed by every upstream op. ``full_size`` is the same input at full
    resolution, the coordinate space of the stage's parameters. ``render_full``
    renders that full-resolution input from the untouched original; only the Output
    stage calls it.
    """

    image: Image.Image
    full_size: tuple[int, int]
    render_full: Callable[[], Image.Image]
    dpi: tuple[int, int] = (DEFAULT_DPI, DEFAULT_DPI)
    #: Upstream transform ops and the original page size (#195 step 5: the page
    #: outline inside the stage input, which Select Content needs).
    ops: tuple[scan_transform.Op, ...] = ()
    original_size: tuple[int, int] | None = None

    @property
    def scale(self) -> tuple[float, float]:
        return (self.image.width / self.full_size[0], self.image.height / self.full_size[1])


@dataclass
class StageResult:
    """A stage's output: ``image`` at working resolution (Output: full resolution)."""

    image: Image.Image
    full_size: tuple[int, int]
    parameters: dict[str, Any]
    full_resolution: bool = False


type StageRunner = Callable[[ScanTailorStageState, StageInput], StageResult]


def _transform_result(
    parameters: dict[str, Any],
    stage_input: StageInput,
    op: scan_transform.Op,
    updates: dict[str, Any],
) -> StageResult:
    """Apply ``op`` at working resolution and record it with the stage's parameters."""
    return StageResult(
        image=scan_transform.apply(stage_input.image, op, stage_input.scale),
        full_size=scan_transform.output_size(op, stage_input.full_size),
        parameters=_as_recorded_parameters(parameters, {**updates, "transform": op}),
    )


def _box_to_full(
    box: tuple[int, int, int, int], stage_input: StageInput
) -> tuple[int, int, int, int]:
    """A working-resolution box in full-resolution coordinates, clamped to the input."""
    sx, sy = stage_input.scale
    width, height = stage_input.full_size
    left = max(0, min(width - 1, int(box[0] / sx)))
    top = max(0, min(height - 1, int(box[1] / sy)))
    right = max(left + 1, min(width, ceil(box[2] / sx - 1e-6)))
    bottom = max(top + 1, min(height, ceil(box[3] / sy - 1e-6)))
    return left, top, right, bottom


def _run_copy(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    return _transform_result(
        dict(state.parameters or {}), stage_input, scan_transform.IDENTITY, {"algorithm": "copy"}
    )


def _working_dpi(stage_input: StageInput) -> tuple[float, float]:
    """Resolution of the working-resolution input (the page's dpi times the work scale)."""
    sx, sy = stage_input.scale
    return max(1.0, stage_input.dpi[0] * sx), max(1.0, stage_input.dpi[1] * sy)


def _run_deskew(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    """Deskew (#195 step 4): upstream ``deskew::Task`` with the ``SkewFinder`` port.

    ``angle`` is the rotation applied, in degrees, positive = clockwise on screen (the
    editor's convention and upstream's internal one). Manual mode applies
    ``manual_angle``; auto mode applies what :func:`deskew.detect` finds, or 0 when its
    confidence is below upstream's ``GOOD_CONFIDENCE``.
    """
    parameters = dict(state.parameters or {})
    for legacy in ("angle_min", "angle_max", "angle_step"):
        parameters.pop(legacy, None)
    manual = parameters.get("manual_angle")
    updates: dict[str, Any]
    if manual is not None:
        angle = deskew.clamp_manual(_coerce_float(manual, 0.0))
        parameters["manual_angle"] = angle
        updates = {"deskew_mode": "manual", "angle": angle}
        for stale in ("detected_angle", "confidence", "confidence_good"):
            parameters.pop(stale, None)
    else:
        found = deskew.detect(np.asarray(stage_input.image.convert("L")), _working_dpi(stage_input))
        angle = found.angle
        updates = {
            "deskew_mode": "auto",
            "angle": angle,
            "detected_angle": found.detected_angle,
            "confidence": found.confidence,
            "confidence_good": found.applied,
        }
    # The transform op rotates counter-clockwise (Pillow convention, scan_transform).
    return _transform_result(parameters, stage_input, scan_transform.rotate(-angle), updates)


CONTENT_MODES: Final[tuple[str, ...]] = ("auto", "manual", "disabled")
MM_PER_INCH: Final[float] = 25.4


def _page_outline(stage_input: StageInput) -> list[tuple[float, float]] | None:
    """The original page's corners inside the stage input (full-resolution coordinates).

    Upstream passes the page's ``preCropArea`` along with its transformation; here the
    original rectangle is mapped through the recorded upstream ops. None when unknown.
    """
    if stage_input.original_size is None:
        return None
    width, height = stage_input.original_size
    corners = [(0.0, 0.0), (float(width), 0.0), (float(width), float(height)), (0.0, float(height))]
    size = (width, height)
    try:
        for op in stage_input.ops:
            # map_point works on pixel indices (pixel centres); corners are continuous.
            corners = [
                (mapped[0] + 0.5, mapped[1] + 0.5)
                for mapped in (
                    scan_transform.map_point(op, (x - 0.5, y - 0.5), size) for x, y in corners
                )
            ]
            size = scan_transform.output_size(op, size)
    except (scan_transform.TransformError, KeyError, TypeError, ValueError):
        return None
    if tuple(size) != tuple(stage_input.full_size):
        return None
    return corners


def _detect_content(stage_input: StageInput) -> tuple[int, int, int, int] | None:
    """Upstream ``ContentBoxFinder`` on the stage input; a full-resolution box or None."""
    gray = np.asarray(stage_input.image.convert("L"))
    gray150, _, _ = content_box.to_working(gray, _working_dpi(stage_input))
    full_w, full_h = stage_input.full_size
    to150_x = gray150.shape[1] / full_w
    to150_y = gray150.shape[0] / full_h
    page_mask = None
    outline = _page_outline(stage_input)
    if outline is not None:
        polygon = np.array([[x * to150_x, y * to150_y] for x, y in outline], dtype=np.float64)
        mask = np.zeros(gray150.shape, dtype=np.uint8)
        cv2.fillPoly(mask, [np.round(polygon * 16).astype(np.int32)], 1, shift=4)
        page_mask = mask.astype(bool)
    found = content_box.find_content_box(gray150, page_mask)
    if found is None:
        return None
    left, top, right, bottom = found
    # QRect -> QRectF: the right/bottom edge of the last pixel, then back to full size.
    box = (
        int(floor(left / to150_x)),
        int(floor(top / to150_y)),
        int(ceil((right + 1) / to150_x - 1e-6)),
        int(ceil((bottom + 1) / to150_y - 1e-6)),
    )
    return (
        max(0, min(full_w - 1, box[0])),
        max(0, min(full_h - 1, box[1])),
        max(1, min(full_w, box[2])),
        max(1, min(full_h, box[3])),
    )


def _run_select_content(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    """Select Content (#195 step 5): upstream ``select_content::Task`` with the
    ``ContentBoxFinder`` port.

    Modes: ``auto`` finds the box; ``manual`` uses ``content_box``; ``disabled`` keeps
    the whole page (Scan Tailor Advanced's "content detection disabled"). When auto
    finds no content the whole page is kept too and ``content_found`` is false
    (upstream records an empty content box; Margins handles it in step 7).
    """
    parameters = dict(state.parameters or {})
    mode = str(parameters.get("content_mode", "auto")).strip().lower()
    if mode not in CONTENT_MODES:
        mode = "auto"
    full_width, full_height = stage_input.full_size
    whole = (0, 0, full_width, full_height)
    box: tuple[int, int, int, int] | None = None
    found = True
    if mode == "manual":
        box = _coerce_box(
            {
                key: parameters.get(key)
                for key in (
                    "x1",
                    "y1",
                    "x2",
                    "y2",
                    "left",
                    "top",
                    "right",
                    "bottom",
                    "width",
                    "height",
                )
            },
            parameters.get("content_box"),
            stage_input.full_size,
        )
        if box is None:
            mode = "auto"
    if mode == "disabled":
        box = whole
    elif mode == "auto":
        box = _detect_content(stage_input)
        if box is None:
            found = False
            box = whole
    assert box is not None
    left, top, right, bottom = box
    dpi_x, dpi_y = (max(1, value) for value in stage_input.dpi)
    return _transform_result(
        parameters,
        stage_input,
        scan_transform.crop(box, stage_input.full_size),
        {
            "content_mode": mode,
            "content_found": found,
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
            # PhysSizeCalc: stage coordinates are original pixels (rotation keeps lengths).
            "content_size_mm": {
                "width": round((right - left) * MM_PER_INCH / dpi_x, 1),
                "height": round((bottom - top) * MM_PER_INCH / dpi_y, 1),
            },
            "source_width": full_width,
            "source_height": full_height,
            "dpi": [dpi_x, dpi_y],
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


def _run_margins(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    source_width, source_height = stage_input.full_size

    parameters = dict(state.parameters or {})
    mode = str(parameters.get("margins_mode", "auto")).strip().lower()
    if mode not in {"auto", "manual"}:
        mode = "auto"

    default_margin = _coerce_int(parameters.get("margin"), 0, minimum=0)
    top = _coerce_int(parameters.get("top"), default_margin, minimum=0)
    right = _coerce_int(parameters.get("right"), default_margin, minimum=0)
    bottom = _coerce_int(parameters.get("bottom"), default_margin, minimum=0)
    left = _coerce_int(parameters.get("left"), default_margin, minimum=0)

    requested_width = _coerce_int(parameters.get("canvas_width"), source_width + left + right)
    requested_height = _coerce_int(parameters.get("canvas_height"), source_height + top + bottom)
    target_w = max(requested_width, source_width + left + right)
    target_h = max(requested_height, source_height + top + bottom)

    align_x = str(parameters.get("align_x", "center")).strip().lower()
    align_y = str(parameters.get("align_y", "center")).strip().lower()
    spare_x = max(0, target_w - (source_width + left + right))
    spare_y = max(0, target_h - (source_height + top + bottom))
    extra_x = 0 if align_x == "left" else spare_x if align_x == "right" else spare_x // 2
    extra_y = 0 if align_y == "top" else spare_y if align_y == "bottom" else spare_y // 2

    return _transform_result(
        parameters,
        stage_input,
        scan_transform.pad(
            (left + extra_x, top + extra_y), (target_w, target_h), stage_input.full_size
        ),
        {
            "margins_mode": mode,
            "margin_top": top,
            "margin_right": right,
            "margin_bottom": bottom,
            "margin_left": left,
            "align_x": align_x,
            "align_y": align_y,
            "resolved_canvas_width": target_w,
            "resolved_canvas_height": target_h,
        },
    )


DEWARP_MODES: Final[tuple[str, ...]] = ("off", "auto", "manual")
#: Upstream ``DewarpingView`` samples splines like this when drawing the model.
type Points = list[tuple[float, float]]


def _coerce_points(value: Any) -> Points | None:
    """``[[x, y], ...]`` with at least two finite points, or None."""
    if not isinstance(value, list | tuple) or len(value) < 2:
        return None
    points: Points = []
    for item in value:
        if not isinstance(item, list | tuple) or len(item) < 2:
            return None
        try:
            x, y = float(item[0]), float(item[1])
        except (TypeError, ValueError):
            return None
        if not (isfinite(x) and isfinite(y)):
            return None
        points.append((x, y))
    return points


def _depth_perception(parameters: dict[str, Any]) -> float:
    value = _coerce_float(
        parameters.get("depth_perception", cylindrical.DEFAULT_DEPTH_PERCEPTION),
        cylindrical.DEFAULT_DEPTH_PERCEPTION,
    )
    return min(cylindrical.MAX_DEPTH_PERCEPTION, max(cylindrical.MIN_DEPTH_PERCEPTION, value))


def _extend_to_edges(polyline: Points, width: float) -> Points:
    """Extend a baseline straight along its end segments to x = 0 and x = width."""
    points = sorted(polyline)
    if len(points) < 2:
        return points
    (x0, y0), (x1, y1) = points[0], points[1]
    (xa, ya), (xb, yb) = points[-2], points[-1]
    left = y0 + (0.0 - x0) * (y1 - y0) / (x1 - x0) if x1 != x0 else y0
    right = yb + (width - xb) * (yb - ya) / (xb - xa) if xb != xa else yb
    inner = [p for p in points if 0.0 < p[0] < width]
    return [(0.0, left), *inner, (width, right)]


def _spline_through(polyline: Points) -> cylindrical.XSpline:
    """Editor control points for a polyline: upstream's five-point spline (ends with
    tension 0) placed on the polyline at equal x steps. Upstream then fits the spline
    to the polyline (``spfit``, #195 step 6b)."""
    xs = np.array([p[0] for p in polyline])
    ys = np.array([p[1] for p in polyline])
    targets = np.linspace(xs[0], xs[-1], 5)
    points = [(float(x), float(np.interp(x, xs, ys))) for x in targets]
    return cylindrical.XSpline(points, [0.0, 1.0, 1.0, 1.0, 0.0])


def _auto_dewarp_curves(stage_input: StageInput) -> tuple[Points, Points] | None:
    """Bridge until the upstream detection is ported (#195 step 6b): the topmost and
    bottommost baselines found by the pre-port detector, extended to the page edges."""
    sx, sy = stage_input.scale
    try:
        baselines = curvature.detect_curves(stage_input.image)
    except ValueError:  # "no reliable text baselines found"
        return None
    if len(baselines) < 2:
        return None
    full = [[(float(x) / sx, float(y) / sy) for x, y in line] for line in baselines]
    full.sort(key=lambda line: float(np.mean([p[1] for p in line])))
    width = float(stage_input.full_size[0])
    return _extend_to_edges(full[0], width), _extend_to_edges(full[-1], width)


def _run_cylindrical_dewarp(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    """Dewarping (#195 step 6): upstream's cylindrical-surface model.

    Modes are upstream ``DewarpingMode``: off, auto and manual. The model is a top and
    a bottom X-spline (``top_curve`` / ``bottom_curve`` control points, stage input
    coordinates); the dewarping keeps the input's size, as Output keeps the page
    layout upstream. A model that upstream would reject (``DistortionModel::isValid``)
    or that fails to build leaves the page unchanged with a warning.
    """
    parameters = dict(state.parameters or {})
    mode = str(parameters.get("dewarp_mode", "auto")).strip().lower()
    if mode == "bypass":
        mode = "off"
    if mode not in DEWARP_MODES:
        mode = "auto"
    depth = _depth_perception(parameters)
    width, height = stage_input.full_size
    updates: dict[str, Any] = {
        "dewarp_mode": mode,
        "depth_perception": depth,
        "dewarp_warning": "",
        "output_width": width,
        "output_height": height,
    }
    for legacy in ("dewarp_model", "dewarp_points", "dewarp_curves", "curvature_curves"):
        parameters.pop(legacy, None)
    if mode == "off":
        for key in ("top_curve", "bottom_curve", "top_polyline", "bottom_polyline"):
            parameters.pop(key, None)
        return _transform_result(
            parameters, stage_input, scan_transform.IDENTITY, {**updates, "dewarp_applied": False}
        )

    top_spline: cylindrical.XSpline | None = None
    bottom_spline: cylindrical.XSpline | None = None
    trivial = False
    if mode == "manual":
        top_points = _coerce_points(parameters.get("top_curve"))
        bottom_points = _coerce_points(parameters.get("bottom_curve"))
        if top_points is not None and bottom_points is not None:
            top_spline = cylindrical.XSpline(top_points)
            bottom_spline = cylindrical.XSpline(bottom_points)
        else:
            mode = updates["dewarp_mode"] = "auto"
    if top_spline is None or bottom_spline is None:
        found = _auto_dewarp_curves(stage_input)
        trivial = found is None
        if found is None:
            top_line, bottom_line = cylindrical.trivial_curves(width, height)
            updates["dewarp_warning"] = "No text lines were found; the page is unchanged."
        else:
            top_line, bottom_line = found
        top_spline = _spline_through(top_line)
        bottom_spline = _spline_through(bottom_line)

    top = top_spline.to_polyline()
    bottom = bottom_spline.to_polyline()
    updates["top_curve"] = [[x, y] for x, y in top_spline.points]
    updates["bottom_curve"] = [[x, y] for x, y in bottom_spline.points]
    op: scan_transform.Op = scan_transform.IDENTITY
    if trivial:
        pass  # upstream's trivial model: the page stays as it is
    elif not cylindrical.model_is_valid(top, bottom):
        updates["dewarp_warning"] = (
            "The curves do not form a valid model (their ends must make a convex shape); "
            "the page is unchanged."
        )
    else:
        op = scan_transform.cylindrical_op(top, bottom, depth, (width, height))
        try:
            result = _transform_result(
                parameters, stage_input, op, {**updates, "dewarp_applied": True}
            )
        except scan_transform.TransformError as exc:
            updates["dewarp_warning"] = (
                f"The model could not be applied ({exc}); the page is unchanged."
            )
            op = scan_transform.IDENTITY
        else:
            return result
    return _transform_result(parameters, stage_input, op, {**updates, "dewarp_applied": False})


def _run_dewarping(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    return _run_cylindrical_dewarp(state, stage_input)


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


OUTPUT_TILE_EDGE: Final = 768


def _output_zones(parameters: dict[str, Any], size: tuple[int, int]) -> list[dict[str, Any]]:
    """Normalize authored lists, or replay legacy text_box/picture_box parameters."""
    if "zones" in parameters:
        entries = parameters["zones"]
        if not isinstance(entries, list) or len(entries) > output_zones.MAX_ZONES:
            raise ValueError("zones must be a list of at most 64 regions")
    else:
        entries = [
            {"kind": kind, "box": parameters.get(f"{kind}_box")}
            for kind in ("text", "picture")
            if parameters.get(f"{kind}_box") is not None
        ]
    zones = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("kind") not in {"text", "picture"}:
            raise ValueError("each zone needs kind text or picture and a box")
        box = _coerce_box({}, entry.get("box"), size)
        if box is None:
            raise ValueError("zone box must have positive area inside the input")
        zones.append({"kind": entry["kind"], "box": list(box), "source": "manual"})
    return zones


def _threshold_tiles(source: Image.Image, mode: str, parameters: dict[str, Any]) -> Image.Image:
    """Bound float-array scratch to a tile plus a ≤251-pixel local-window halo."""
    if mode == "copy":
        return source.copy()
    window = min(501, max(3, _coerce_int(parameters.get("window_size"), 35)))
    halo = _odd_window(window) // 2 + 2
    result = Image.new("L", source.size, 255)
    # Otsu is global, including when the output is tiled. Pillow's histogram uses
    # one byte per pixel, avoiding page-sized float/label arrays.
    cutoff = _pil_otsu_threshold(source) if mode == "otsu" else None
    for top in range(0, source.height, OUTPUT_TILE_EDGE):
        for left in range(0, source.width, OUTPUT_TILE_EDGE):
            right, bottom = (
                min(source.width, left + OUTPUT_TILE_EDGE),
                min(source.height, top + OUTPUT_TILE_EDGE),
            )
            box = (
                max(0, left - halo),
                max(0, top - halo),
                min(source.width, right + halo),
                min(source.height, bottom + halo),
            )
            tile = source.crop(box).convert("L")
            if cutoff is not None:
                processed = tile.point(lambda value: 255 if value > cutoff else 0)
                if _coerce_bool(parameters.get("invert"), False):
                    processed = ImageOps.invert(processed)
            else:
                processed = _binarize_with_cv2(
                    np.asarray(tile),
                    tile,
                    mode,
                    window_size=window,
                    k=_coerce_float(parameters.get("k"), 0.33),
                    r=_coerce_float(parameters.get("r"), 128.0),
                    invert=_coerce_bool(parameters.get("invert"), False),
                )
            result.paste(
                processed.crop((left - box[0], top - box[1], right - box[0], bottom - box[1])),
                (left, top),
            )
    return result


def _zones_to_full(zones: list[dict[str, Any]], stage_input: StageInput) -> list[dict[str, Any]]:
    """Auto-detected zones are found at working resolution; store them at full."""
    scaled = []
    for zone in zones:
        box = _box_to_full(tuple(int(value) for value in zone["box"]), stage_input)  # type: ignore[arg-type]
        scaled.append({**zone, "box": list(box)})
    return scaled


def _run_output(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    """Render the page once at full resolution from the original, then binarise it."""
    parameters = dict(state.parameters or {})
    mode = _coerce_output_mode(parameters.get("output_mode"), default="otsu")
    zone_mode = str(parameters.get("zone_mode", "auto")).strip().lower()
    if zone_mode not in {"auto", "manual", "off"}:
        zone_mode = "auto"
    full_width, full_height = stage_input.full_size
    if mode == "copy":
        return StageResult(
            image=stage_input.render_full(),
            full_size=stage_input.full_size,
            full_resolution=True,
            parameters={
                "output_mode": "copy",
                "output_applied": "copy",
                "output_width": full_width,
                "output_height": full_height,
                "resolved_zones": [],
                "output_warning": "",
            },
        )
    text_algorithm = _coerce_output_mode(parameters.get("text_algorithm"), default="sauvola")
    picture_algorithm = _coerce_output_mode(parameters.get("picture_algorithm"), default="copy")
    keep_color = _coerce_bool(parameters.get("keep_color"), False)
    if keep_color:
        picture_algorithm = "copy"
    zones: list[dict[str, Any]] = []
    warning = None
    source = stage_input.render_full()
    try:
        if zone_mode == "auto":
            zones = _zones_to_full(output_zones.detect_zones(stage_input.image), stage_input)
        elif zone_mode == "manual":
            zones = _output_zones(parameters, source.size)
        # With Keep color, unclassified areas remain source pixels too; only
        # identified/authored text regions are binarized.
        result = source.copy() if keep_color else _threshold_tiles(source, mode, parameters)
        # Copy means source pixels, even on a colour photo when keep_color is off.
        # Never destroy RGB first and then merely label the grayscale output RGB.
        preserve_rgb = keep_color or (
            source.mode == "RGB"
            and picture_algorithm == "copy"
            and any(z["kind"] == "picture" for z in zones)
        )
        if preserve_rgb:
            result = result.convert("RGB")
        # Pictures win overlaps, independently of the list's order.
        for kind, algorithm in (("text", text_algorithm), ("picture", picture_algorithm)):
            for zone in zones:
                if zone["kind"] != kind:
                    continue
                box = tuple(zone["box"])
                region = source.crop(box)
                processed = _threshold_tiles(region, algorithm, parameters)
                result.paste(processed, box)
        applied = "zoned" if zones else "whole_page"
    except Exception as exc:
        # Uncertain layout or processing must not silently destroy pictures.
        warning = str(exc)
        result = source.copy()
        applied = "copy_fallback"
    payload: dict[str, Any] = {
        "output_mode": mode,
        "output_applied": applied,
        "output_width": source.width,
        "output_height": source.height,
        "zone_mode": zone_mode,
        "resolved_zones": zones,
        "text_algorithm": text_algorithm,
        "picture_algorithm": picture_algorithm,
        "zone_detector_version": output_zones.DETECTOR_VERSION if zone_mode == "auto" else None,
        "output_warning": warning or "",
    }
    if zone_mode == "manual":
        payload["zones"] = zones
        for kind, legacy_box in _zone_rectangles(parameters, source.size).items():
            if legacy_box is not None:
                payload[f"{kind}_box"] = list(legacy_box)
    else:
        payload["zones"] = []
    return StageResult(
        image=result, full_size=source.size, parameters=payload, full_resolution=True
    )


_STAGE_RUNNERS: dict[str, dict[str, StageRunner]] = {
    ScanTailorStageState.Stage.DESKEW.value: {
        "copy": _run_copy,
        "skew_finder": _run_deskew,
        # Names recorded before #195 step 4; same runner.
        "projection_deskew": _run_deskew,
        "manual_deskew": _run_deskew,
    },
    ScanTailorStageState.Stage.SELECT_CONTENT.value: {
        "content_box_finder": _run_select_content,
        # Name recorded before #195 step 5; same runner.
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
        "cylindrical_dewarp": _run_dewarping,
        # Names recorded before #195 step 6; same runner.
        "baseline_curvature": _run_dewarping,
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


def _resolve_stage_algorithm(state: ScanTailorStageState) -> StageRunner:
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
    dispatch: bool = True,
) -> ProcessingJob:
    """Create and dispatch the worker job for ``state`` at its current revision.

    The job records the stage revision it was dispatched for (``stage_revision``) and
    only takes over the stage's ``worker_job_id`` while the row is still at that
    revision. If an edit bumped the revision in between, the job is closed as
    superseded right away and never dispatched, so it cannot replace the newer
    edit's artifact; :func:`process_stage_job` repeats the check on execution.
    """
    revision = state.revision
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.STAGE,
        state=ProcessingJob.JobState.QUEUED,
        details={
            "scan_job_id": str(state.scan_job_id),
            "scan_tailor_stage_state_id": state.id,
            "requested_by_user_id": requested_by_user_id,
            "stage": state.stage,
            "stage_revision": revision,
        },
    )
    algorithm = state.algorithm or _default_algorithm(state.stage)
    if state.stage in {"deskew", "select_content", "dewarping"}:
        # Pre-port algorithm names run the port now (#195 steps 4 to 6).
        algorithm = _default_algorithm(state.stage)
    algorithm_version = _default_algorithm_version(state.stage, algorithm)
    now = timezone.now()
    claimed = ScanTailorStageState.objects.filter(id=state.id, revision=revision).update(
        worker_job_id=str(job.job_id),
        status=ScanTailorStageState.StageStatus.QUEUED,
        algorithm=algorithm,
        algorithm_version=algorithm_version,
        last_error="",
        invalidated_reason="",
        updated_at=now,
    )
    if not claimed:
        _supersede_stage_job(job, dict(job.details), "stage revision changed before dispatch")
        return job
    state.worker_job_id = str(job.job_id)
    state.status = ScanTailorStageState.StageStatus.QUEUED
    state.last_error = ""
    state.invalidated_reason = ""
    state.algorithm = algorithm
    state.algorithm_version = algorithm_version
    state.updated_at = now
    if dispatch:
        dispatch_stage_job(str(job.job_id))
    return job


# Pages per batch job: big enough to amortise the per-job overhead, small enough that
# an interactive edit queued at the front never waits long behind a running chunk.
STAGE_BATCH_CHUNK: Final[int] = 8


def dispatch_stage_job(job_id: str) -> None:
    """Run one interactive stage job before any queued batch work (#195 §8.2 item 11).

    Upstream Scan Tailor processes the page the user is looking at first. Here a
    single-page edit goes to the front of the queue; batch chunks go to the back.
    """
    queue = get_queue()
    if queue is None:
        process_stage_job(job_id)
        return
    queue.enqueue(process_stage_job, job_id, at_front=True)


def process_stage_jobs(job_ids: list[str]) -> None:
    """Run a chunk of batch stage jobs; each keeps its own revision checks."""
    for job_id in job_ids:
        process_stage_job(job_id)


def dispatch_stage_batch(job_ids: Sequence[str]) -> int:
    """Queue batch stage jobs in chunks of :data:`STAGE_BATCH_CHUNK`; return the chunk count."""
    chunks = [
        list(job_ids[index : index + STAGE_BATCH_CHUNK])
        for index in range(0, len(job_ids), STAGE_BATCH_CHUNK)
    ]
    queue = get_queue()
    for chunk in chunks:
        if queue is None:
            process_stage_jobs(chunk)
        else:
            queue.enqueue(process_stage_jobs, chunk)
    return len(chunks)


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


def _upstream_chain(state: ScanTailorStageState) -> list[ScanTailorStageState]:
    """The stages feeding ``state``, first stage first."""
    chain: list[ScanTailorStageState] = []
    current = state.input_stage_state
    while current is not None and len(chain) < len(stage_order()):
        chain.append(current)
        current = current.input_stage_state
    chain.reverse()
    return chain


def _page_version(
    state: ScanTailorStageState, chain: list[ScanTailorStageState]
) -> ScanPageVersion:
    first = chain[0] if chain else state
    version = first.input_scan_page_version or state.scan_page.active_version()
    if version is None:
        raise RuntimeError("missing source artifact: the page has no active version")
    return version


def _stage_input(state: ScanTailorStageState, storage: LocalFilesystemStorage) -> StageInput:
    """The working-resolution input of ``state`` plus a full-resolution renderer.

    Stage 1 reads the page's working image; every later stage reads its predecessor's
    working-resolution output. ``render_full`` composes every upstream op on the
    untouched original, which only the Output stage needs.
    """
    chain = _upstream_chain(state)
    version = _page_version(state, chain)
    page_image = scan_images.ensure_page_image(version)
    ops: list[scan_transform.Op] = []
    if not chain:
        work_identifier = page_image.work_storage_identifier
        full_size = (page_image.full_width, page_image.full_height)
    else:
        for upstream in chain:
            op = (upstream.parameters or {}).get("transform")
            if not isinstance(op, dict):
                raise RuntimeError(
                    f"upstream stage {upstream.stage} has no recorded transform; re-run it"
                )
            ops.append(op)
        predecessor = chain[-1]
        work_identifier = predecessor.output_storage_identifier
        parameters = predecessor.parameters or {}
        try:
            full_size = (int(parameters["output_width"]), int(parameters["output_height"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"upstream stage {predecessor.stage} has no recorded size; re-run it"
            ) from exc
    if not work_identifier:
        raise RuntimeError("missing source artifact")
    work_path = storage.resolve_location(work_identifier)
    if not work_path.exists():
        raise RuntimeError(f"source missing: {work_identifier}")

    def render_full() -> Image.Image:
        original_path = filesystem_backend_for_location(version.storage_location).resolve_location(
            version.storage_identifier
        )
        original = _load_image(original_path)
        return scan_transform.apply_chain(original, ops, original.size)

    return StageInput(
        image=_load_image(work_path),
        full_size=full_size,
        render_full=render_full,
        dpi=(page_image.dpi_x, page_image.dpi_y),
        ops=tuple(ops),
        original_size=(page_image.full_width, page_image.full_height),
    )


def _write_stage_artifacts(
    result: StageResult,
    stage_input: StageInput,
    output: Path,
    preview: Path,
    thumbnail: Path | None = None,
) -> None:
    if result.full_resolution:
        _save_png(result.image, output, stage_input.dpi)
        sx, sy = stage_input.scale
        preview_size = (
            max(1, round(result.image.width * sx)),
            max(1, round(result.image.height * sy)),
        )
        preview_image = scan_images.downscale(result.image, preview_size)
        scan_images.save_work_png(preview_image, preview)
    else:
        preview_image = result.image
        scan_images.save_work_png(result.image, output)
        scan_images.save_preview_jpeg(result.image, preview)
    if thumbnail is not None:
        # Small image for the workspace page strip (#195 step 3).
        scan_images.save_preview_jpeg(
            _thumbnail(preview_image, scan_images.THUMBNAIL_EDGE), thumbnail
        )


STAGE_THUMBNAIL_NAME: Final[str] = "thumbnail.jpg"


def stage_thumbnail_identifier(state: ScanTailorStageState) -> str:
    """The strip thumbnail of a published stage: next to its output, same revision."""
    if not state.output_storage_identifier:
        return ""
    parent = state.output_storage_identifier.rsplit("/", 1)[0]
    return f"{parent}/{STAGE_THUMBNAIL_NAME}"


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

    # A job only ever produces the artifact of the revision it was queued for. A late
    # job (e.g. one overtaken by a manual edit) is a no-op.
    dispatched_revision = details.get("stage_revision")
    if isinstance(dispatched_revision, int) and dispatched_revision != state.revision:
        _supersede_stage_job(job, details, "stage revision changed after dispatch")
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
        storage = _scan_storage()
        storage_location = _scan_storage_location()
        artifact_identifier = output_identifier(state)
        preview_identifier = output_identifier(state, preview=True)

        runner = _resolve_stage_algorithm(state)
        # Per-phase timings for the structured log (#195 section 8.3).
        timer_start = time.perf_counter()
        stage_input = _stage_input(state, storage)
        input_done = time.perf_counter()
        result = runner(state, stage_input)
        run_done = time.perf_counter()
        with tempfile.TemporaryDirectory(prefix="stages-") as tempdir:
            staged_output = Path(tempdir) / _build_output_name(state)
            staged_preview = Path(tempdir) / _build_output_name(state, preview=True)
            staged_thumbnail = Path(tempdir) / STAGE_THUMBNAIL_NAME
            _write_stage_artifacts(
                result, stage_input, staged_output, staged_preview, staged_thumbnail
            )
            encode_done = time.perf_counter()
            merged = dict(state.parameters or {})
            merged.update(result.parameters)
            merged.update(
                {
                    "input_width": stage_input.full_size[0],
                    "input_height": stage_input.full_size[1],
                    "output_width": result.full_size[0],
                    "output_height": result.full_size[1],
                }
            )
            state.parameters = merged
            output_size = staged_output.stat().st_size
            output_sha = _compute_sha256(staged_output)
            hash_done = time.perf_counter()
            if not _publish_stage_artifacts(
                state,
                started_revision,
                storage,
                storage_location,
                staged_output,
                staged_preview,
                artifact_identifier,
                preview_identifier,
                output_size,
                output_sha,
                thumbnail=staged_thumbnail,
            ):
                _supersede_stage_job(job, details, "stage edited while job was running")
                return
            publish_done = time.perf_counter()

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
            duration_ms=round((time.perf_counter() - timer_start) * 1000, 1),
            input_ms=round((input_done - timer_start) * 1000, 1),
            run_ms=round((run_done - input_done) * 1000, 1),
            encode_ms=round((encode_done - run_done) * 1000, 1),
            hash_ms=round((hash_done - encode_done) * 1000, 1),
            store_db_ms=round((publish_done - hash_done) * 1000, 1),
        )
    except Exception as exc:
        with transaction.atomic():
            if _locked_if_current(state.id, started_revision) is not None:
                state.status = ScanTailorStageState.StageStatus.FAILED
                state.last_error = str(exc)
                state.finished_at = timezone.now()
                state.save(update_fields=["status", "last_error", "finished_at", "updated_at"])
        _fail_stage_job(job, str(exc))


def _publish_stage_artifacts(
    state: ScanTailorStageState,
    revision: int,
    storage: LocalFilesystemStorage,
    location: StorageLocation,
    output: Path,
    preview: Path,
    output_identifier: str,
    preview_identifier: str,
    output_size: int,
    output_sha: str,
    thumbnail: Path | None = None,
) -> bool:
    """Copy privately, then promote under the same row lock used by retirement.

    Slow I/O and hash verification stay outside the lock. A late worker never
    recreates a final path after its revision was superseded or quarantined.
    """
    token = uuid.uuid4().hex
    private_output = f"{_output_namespace(state)}/.{token}.output.partial"
    private_preview = f"{_output_namespace(state)}/.{token}.preview.partial"
    private_thumbnail = f"{_output_namespace(state)}/.{token}.thumbnail.partial"
    thumbnail_identifier = (
        f"{output_identifier.rsplit('/', 1)[0]}/{STAGE_THUMBNAIL_NAME}" if thumbnail else ""
    )
    try:
        storage.store_atomic(str(output), private_output)
        storage.store_atomic(str(preview), private_preview)
        if thumbnail is not None:
            storage.store_atomic(str(thumbnail), private_thumbnail)
        if storage.hash(private_output) != output_sha or storage.hash(
            private_preview
        ) != _compute_sha256(preview):
            raise RuntimeError("staged processing artifact checksum mismatch")
        with transaction.atomic():
            if _locked_if_current(state.id, revision) is None:
                return False
            storage.commit_staged(private_output, output_identifier)
            storage.commit_staged(private_preview, preview_identifier)
            if thumbnail is not None:
                storage.commit_staged(private_thumbnail, thumbnail_identifier)
            state.output_storage_location = location
            state.preview_storage_location = location
            state.output_storage_identifier = output_identifier
            state.preview_storage_identifier = preview_identifier
            state.output_size_bytes = output_size
            state.output_sha256 = output_sha
            state.status = ScanTailorStageState.StageStatus.SUCCESS
            state.finished_at = timezone.now()
            state.last_error = ""
            state.algorithm_version = _default_algorithm_version(state.stage, state.algorithm)
            state.save(
                update_fields=[
                    "algorithm_version",
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
                ]
            )
        return True
    finally:
        for identifier in (private_output, private_preview, private_thumbnail):
            try:
                storage.discard_staged(identifier)
            except OSError:
                logger.warning("scantailor_private_cleanup_failed", stage_state_id=state.pk)


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
