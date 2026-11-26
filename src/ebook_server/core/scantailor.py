"""ScanTailor-Web worker orchestration helpers."""

from __future__ import annotations

import hashlib
import tempfile
import time
import uuid
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from math import ceil, floor, isfinite
from pathlib import Path
from typing import Any, Final, Literal

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
    page_layout,
    scan_artifact_pruning,
    scan_images,
    scan_lifecycle,
    scan_transform,
)
from .models import (
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanPageDispositionEvent,
    ScanPageVersion,
    ScanTailorRun,
    ScanTailorStageState,
    StorageLocation,
)
from .stage_cancel import StageSuperseded, cancellable, checkpoint
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
    "select_content": "contentboxfinder-v4",
    # v3 (#208): port of upstream page_layout: one page size for all, margins in mm.
    "margins": "pagelayout-v3",
    # v2: the output canvas keeps the whole warped input instead of cropping to the quad.
    # v3: auto mode derives the quad from the text lines.
    # v4: transform op, detection at working resolution (#195 step 2).
    # v5 (#195 step 6a): upstream's cylindrical-surface model (CylindricalSurfaceDewarper).
    "dewarping": "cylindrical-v5",
    # v4: Wolf collects page-wide statistics before thresholding tiles.
    "output": "opencv-zones-v4",
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
    if state.stage == ScanTailorStageState.Stage.OUTPUT.value or is_blank_margins(state):
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

    # The common page size (#208) is part of every aligned Margins row's fingerprint:
    # computed once for the job here, not once per page.
    aggregate = margins_aggregate(scan_job.pk)
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
        # Derived state (#205) follows the input relations; resolve them in memory.
        link_page_states(page_states, active_versions.get(page.id), aggregate)
    return by_page


# Settings that determine a stage's result (#205): the editor's EDITABLE_KEYS plus
# legacy inputs the runners still read. Everything else in ``parameters`` is recorded
# by the worker (sizes, transform op, detection results). Edited settings are compared
# on these keys only, so resubmitting what the editor shows after a run is no change.
STAGE_SETTING_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "deskew": ("manual_angle",),
    "select_content": (
        "content_mode",
        "content_box",
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
    ),
    "dewarping": ("dewarp_mode", "depth_perception", "top_curve", "bottom_curve"),
    "margins": (
        "margins_mode",
        # Millimetres and "align with other pages" (#208).
        *page_layout.MARGIN_KEYS_MM,
        "align_with_others",
        # Pixel settings written before #208 (converted with the page dpi).
        "margin",
        "top",
        "right",
        "bottom",
        "left",
        "canvas_width",
        "canvas_height",
        "align_x",
        "align_y",
    ),
    "output": (
        "output_mode",
        "keep_color",
        "invert",
        "zone_mode",
        "zones",
        "text_box",
        "picture_box",
        "text_algorithm",
        "picture_algorithm",
        "window_size",
        "k",
        "r",
    ),
}


def stage_settings(stage: str, parameters: object) -> dict[str, Any]:
    """The settings part of ``parameters``: what decides the stage's result."""
    if not isinstance(parameters, dict):
        return {}
    keys = STAGE_SETTING_KEYS.get(stage)
    if keys is None:
        return dict(parameters)
    settings = {key: parameters[key] for key in keys if key in parameters}
    if stage == "output" and str(parameters.get("zone_mode", "")).strip().lower() != "manual":
        # Detected zones are a result; only a manual list is a setting.
        settings.pop("zones", None)
    return settings


def stage_settings_applied(state: ScanTailorStageState) -> bool:
    """Whether the stage's result was computed with its current settings (#205)."""
    if state.applied_revision is None:
        return False
    if state.applied_revision == state.revision:
        return True
    return stage_settings(state.stage, state.parameters) == stage_settings(
        state.stage, state.applied_parameters
    )


def stage_input_fingerprint(state: ScanTailorStageState) -> str:
    """Fingerprint of the input ``state`` would be computed from now (#205).

    The predecessor's ``output_sha256`` (its last result), or for the first stage the
    checksum of the page version it reads. A successful run records this value as
    ``applied_input_sha256``; a mismatch means "input changed". Anything else a stage's
    result depends on belongs in this function: for the Margins stage of a page aligned
    with the others, that is the job's common page size (#208,
    :func:`margins_input_fingerprint`), so a change of the aggregate makes every
    aligned page's Margins stale through this same mechanism.
    """
    if is_blank_margins(state):
        version = state.input_scan_page_version
        return margins_input_fingerprint(
            version.checksum_sha256 if version is not None else "",
            job_margins_aggregate(state).fingerprint,
        )
    if previous_stage(state.stage) is None:
        version = state.input_scan_page_version
        if version is None:
            version = state.scan_page.active_version()
        return version.checksum_sha256 if version is not None else ""
    predecessor = state.input_stage_state
    if predecessor is None or predecessor.status != ScanTailorStageState.StageStatus.SUCCESS:
        return ""
    if margins_depends_on_aggregate(state):
        return margins_input_fingerprint(
            predecessor.output_sha256, job_margins_aggregate(state).fingerprint
        )
    return predecessor.output_sha256


def margins_depends_on_aggregate(state: ScanTailorStageState) -> bool:
    """Margins of a page aligned with the others reads the job's aggregate (#208).

    A result of the per-page padding before #208 (algorithm version ``padding-*``, or
    none recorded) did not read it: like any result of an older algorithm version it
    stays current (#205) until the stage runs again, so migration 0053's fingerprints
    and finished jobs' published results stay valid. Queuing the stage records the
    current version first, so every new run reads the aggregate.
    """
    if state.stage != ScanTailorStageState.Stage.MARGINS.value:
        return False
    version = state.algorithm_version or ""
    if not version or version.startswith(_PRE_LAYOUT_MARGINS_VERSION_PREFIX):
        return False
    return is_blank_margins(state) or page_layout.aligns_with_others(state.parameters or {})


def is_blank_margins(state: ScanTailorStageState) -> bool:
    return (
        state.stage == ScanTailorStageState.Stage.MARGINS
        and (state.parameters or {}).get("declared_blank") is True
    )


def is_declared_blank_stage(state: ScanTailorStageState) -> bool:
    return state.status == ScanTailorStageState.StageStatus.SKIPPED or is_blank_margins(state)


#: Margins algorithm versions before #208's page layout port ("padding-v2" and older).
_PRE_LAYOUT_MARGINS_VERSION_PREFIX: Final[str] = "padding"


def margins_input_fingerprint(predecessor_sha256: str, aggregate_fingerprint: str) -> str:
    """Input fingerprint of an aligned page's Margins: its input plus the common size.

    One sha256, so it fits ``applied_input_sha256``. The editor cannot recompute it;
    rows carry it as ``input_sha256`` with the predecessor output it was built from.
    """
    payload = f"{predecessor_sha256}+aggregate:{aggregate_fingerprint}"
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


#: Attribute holding a job aggregate attached to a loaded Margins row (#208).
_AGGREGATE_ATTRIBUTE: Final[str] = "_margins_aggregate"


def attach_margins_aggregate(
    states: Iterable[ScanTailorStageState], aggregate: page_layout.Aggregate
) -> None:
    """Give loaded Margins rows the job aggregate computed once for them (#208).

    The aggregate reads all of the job's Select Content and Margins rows. Code that
    derives the state of many pages computes it once per request and attaches it, so
    :func:`stage_input_fingerprint` does not query it again for every page.
    """
    for state in states:
        if state.stage == ScanTailorStageState.Stage.MARGINS.value:
            setattr(state, _AGGREGATE_ATTRIBUTE, aggregate)


def page_margins_aggregate(
    states: dict[str, ScanTailorStageState],
) -> page_layout.Aggregate | None:
    """The job aggregate for one page's rows, only if its Margins result needs it.

    For :func:`link_page_states` on a single page (editor, thumbnail): one query when
    the page has a Margins result aligned with the others, none otherwise.
    """
    state = states.get(ScanTailorStageState.Stage.MARGINS.value)
    if state is None or state.status != ScanTailorStageState.StageStatus.SUCCESS:
        return None
    if not margins_depends_on_aggregate(state):
        return None
    return margins_aggregate(state.scan_job_id)


def attach_margins_aggregate_if_needed(states: Sequence[ScanTailorStageState]) -> None:
    """Attach the job aggregate to loaded rows, computed at most once (#208).

    For code serializing many rows of one job (the live-update feeds): one query when
    any of them is an aligned Margins result, none otherwise.
    """
    needing = [
        state
        for state in states
        if state.status == ScanTailorStageState.StageStatus.SUCCESS
        and margins_depends_on_aggregate(state)
        and not isinstance(getattr(state, _AGGREGATE_ATTRIBUTE, None), page_layout.Aggregate)
    ]
    if needing:
        attach_margins_aggregate(needing, margins_aggregate(needing[0].scan_job_id))


def job_margins_aggregate(state: ScanTailorStageState) -> page_layout.Aggregate:
    """The aggregate attached to ``state``, else freshly computed (one query)."""
    attached = getattr(state, _AGGREGATE_ATTRIBUTE, None)
    if isinstance(attached, page_layout.Aggregate):
        return attached
    return margins_aggregate(state.scan_job_id)


def stage_result_is_current(state: ScanTailorStageState) -> bool:
    """True iff ``state`` has a successful result matching its inputs and settings (#205).

    Current means: the last run succeeded, it used the stage's current settings, its
    recorded input fingerprint equals :func:`stage_input_fingerprint`, and the
    predecessor is itself current (recursively up to the first stage).
    """
    if state.status == ScanTailorStageState.StageStatus.SKIPPED:
        return True
    if state.status != ScanTailorStageState.StageStatus.SUCCESS:
        return False
    if not stage_settings_applied(state):
        return False
    if previous_stage(state.stage) is not None and not is_blank_margins(state):
        predecessor = state.input_stage_state
        if predecessor is None or not stage_result_is_current(predecessor):
            return False
    return stage_input_fingerprint(state) == state.applied_input_sha256


def stage_waiting_for(state: ScanTailorStageState) -> str | None:
    """The first predecessor (pipeline order) without a current result, else None."""
    if is_blank_margins(state) or state.status == ScanTailorStageState.StageStatus.SKIPPED:
        return None
    chain: list[ScanTailorStageState] = []
    current = state
    while (predecessor_name := previous_stage(current.stage)) is not None:
        predecessor = current.input_stage_state
        if predecessor is None:
            # No linked row for an earlier stage: nothing before it can feed this one.
            return predecessor_name
        chain.append(predecessor)
        current = predecessor
    for upstream in reversed(chain):
        if not stage_result_is_current(upstream):
            return upstream.stage
    return None


def stage_stale_reason(state: ScanTailorStageState) -> str:
    """Why a successful result is not current (#205), or "" when it is current or
    there is no successful result: "settings changed, not applied" or "input changed".
    """
    if state.status != ScanTailorStageState.StageStatus.SUCCESS:
        return ""
    if not stage_settings_applied(state):
        return "settings changed, not applied"
    if not stage_result_is_current(state):
        if _common_size_changed(state):
            return COMMON_SIZE_CHANGED
        return "input changed"
    return ""


#: Stale reason of an aligned page's Margins after another page changed the size (#208).
COMMON_SIZE_CHANGED: Final[str] = "common page size changed"


def _common_size_changed(state: ScanTailorStageState) -> bool:
    """The Margins input is unchanged except for the job's common page size."""
    if not margins_depends_on_aggregate(state):
        return False
    if is_blank_margins(state):
        return True
    predecessor = state.input_stage_state
    if predecessor is None or not stage_result_is_current(predecessor):
        return False
    recorded = (state.parameters or {}).get("aggregate_fingerprint")
    if not isinstance(recorded, str) or not recorded:
        return False
    return (
        margins_input_fingerprint(predecessor.output_sha256, recorded) == state.applied_input_sha256
    )


def stage_idle_status(state: ScanTailorStageState) -> str:
    """Status of a stage without a job: SUCCESS if it holds a result, else UNKNOWN."""
    if state.applied_revision is not None and state.output_storage_identifier:
        return ScanTailorStageState.StageStatus.SUCCESS
    return ScanTailorStageState.StageStatus.UNKNOWN


def link_page_states(
    states: dict[str, ScanTailorStageState],
    active_version: ScanPageVersion | None = None,
    aggregate: page_layout.Aggregate | None = None,
) -> None:
    """Point each row's input relations at the given objects (no queries, no writes).

    The derived-state functions above follow ``input_stage_state`` and
    ``input_scan_page_version``; for one page's rows loaded together this avoids a
    query per row. Only relations whose ids already match are set. ``aggregate`` is
    the job's :func:`margins_aggregate`, computed once by a caller deriving many pages
    (:func:`attach_margins_aggregate`); without it the Margins row queries it itself.
    """
    if aggregate is not None:
        attach_margins_aggregate(states.values(), aggregate)
    for stage, state in states.items():
        predecessor_name = previous_stage(stage)
        if predecessor_name is None:
            if active_version is not None and state.input_scan_page_version_id == active_version.id:
                state.input_scan_page_version = active_version
            continue
        predecessor = states.get(predecessor_name)
        if predecessor is not None and state.input_stage_state_id == predecessor.id:
            state.input_stage_state = predecessor


def mark_state_stale(
    *,
    scan_job_id: str,
    page_id: str,
    reason: str,
) -> int:
    """The page's source changed (a replacement scan): point the first stage at it.

    Staleness is derived (#205): rebinding the first stage to the new active version
    changes its input fingerprint, which makes it and every later stage stale. Every
    row of the page is touched so live clients re-read it.
    """
    page = ScanPage.objects.get(pk=page_id)
    active_version = page.active_version()
    rows = ScanTailorStageState.objects.filter(scan_job_id=scan_job_id, scan_page_id=page_id)
    now = timezone.now()
    if page.declared_blank and active_version is not None:
        # A rescan replaced a page declared blank (#219): the new image may have
        # content, so the declaration never silently survives it. The stages run again.
        states = list(rows.select_for_update().order_by("pk"))
        margins = next((state for state in states if is_blank_margins(state)), None)
        declared_version = margins.input_scan_page_version if margins is not None else None
        set_page_blank(
            page,
            states,
            version=declared_version or active_version,
            blank=False,
            reason=DISPOSITION_REASON_REPLACED,
        )
        transaction.on_commit(lambda: queue_after_blank_edit(scan_job_id, page_id))
    if active_version is not None:
        rows.filter(stage=stage_order()[0]).update(
            input_scan_page_version=active_version,
            input_storage_identifier=active_version.storage_identifier,
        )
    logger.info("scantailor_page_source_changed", scan_page_id=page_id, reason=reason)
    return int(rows.update(updated_at=now))


#: ``ScanPageDispositionEvent.reason`` values (#219).
DISPOSITION_REASON_DECLARED: Final[str] = "declared"
DISPOSITION_REASON_REPLACED: Final[str] = "raw_version_replaced"


def set_page_blank(
    page: ScanPage,
    states: list[ScanTailorStageState],
    *,
    version: ScanPageVersion,
    blank: bool,
    actor_id: int | None = None,
    reason: str = DISPOSITION_REASON_DECLARED,
) -> None:
    """Change a declaration under the same row locks as stage edits and page replacement.

    Declaring or clearing is an audited event (#219). Every stage row gets a new
    revision, so a concurrent editor's stale save conflicts, and live clients see the
    change through the workspace events. Blank: Deskew, Select Content, Dewarping and
    Output become ``skipped`` (reason ``declared_blank``); Margins renders a white page
    of the common size. Clearing re-queues the stages from the first one.
    """
    disposition = ScanPage.Disposition.BLANK if blank else ScanPage.Disposition.NORMAL
    ScanPageDispositionEvent.objects.create(
        page=page,
        version=version,
        actor_id=actor_id,
        previous_disposition=page.page_disposition,
        disposition=disposition,
        reason=reason,
    )
    page.page_disposition = disposition
    page.save(update_fields=["page_disposition", "updated_at"])
    for state in states:
        status = ScanTailorStageState.StageStatus
        state.revision += 1
        _drop_queued_stage_job(state.worker_job_id)
        state.worker_job_id = ""
        state.status = status.UNKNOWN
        state.last_error = ""
        state.applied_revision = None
        state.applied_parameters = {}
        state.applied_input_sha256 = ""
        state.output_storage_identifier = ""
        state.preview_storage_identifier = ""
        state.output_sha256 = ""
        state.output_size_bytes = None
        state.started_at = None
        state.finished_at = None
        state.parameters = dict(state.parameters or {})
        state.parameters.pop("declared_blank", None)
        if state.stage == ScanTailorStageState.Stage.MARGINS:
            state.input_scan_page_version = version if blank else None
            state.input_storage_identifier = version.storage_identifier if blank else ""
            if blank:
                state.parameters["declared_blank"] = True
        elif blank:
            state.status = status.SKIPPED
        state.save()


def queue_after_blank_edit(scan_job_id: str, page_id: str, actor_id: int | None = None) -> None:
    page = ScanPage.objects.get(pk=page_id)
    stage = ScanTailorStageState.Stage.MARGINS if page.declared_blank else stage_order()[0]
    state = page.stage_states.get(stage=stage)
    ensure_stage_result(state, requested_by_user_id=actor_id)
    _requeue_after_layout_change(state)


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
    left, top, right, bottom = _pad_content_box(
        found, (gray150.shape[1], gray150.shape[0]), page_mask
    )
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


CONTENT_PADDING_AT_WORK_DPI: Final[int] = 5


def _pad_content_box(box: Box, size: tuple[int, int], page_mask: np.ndarray | None = None) -> Box:
    """Pad an inclusive detector rectangle in 150 dpi coordinates."""
    left, top, right, bottom = box
    width, height = size
    distance = CONTENT_PADDING_AT_WORK_DPI
    if page_mask is None:
        return (
            max(0, left - distance),
            max(0, top - distance),
            min(width - 1, right + distance),
            min(height - 1, bottom + distance),
        )
    # Never grow into the synthetic corner triangles introduced by Deskew.
    for _ in range(distance):
        if left > 0 and page_mask[top : bottom + 1, left - 1].all():
            left -= 1
        if right + 1 < width and page_mask[top : bottom + 1, right + 1].all():
            right += 1
        if top > 0 and page_mask[top - 1, left : right + 1].all():
            top -= 1
        if bottom + 1 < height and page_mask[bottom + 1, left : right + 1].all():
            bottom += 1
    return left, top, right, bottom


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


def _select_content_result(state: ScanTailorStageState) -> dict[str, Any]:
    """The recorded Select Content result upstream of ``state`` (empty when unknown)."""
    current = state.input_stage_state
    for _ in range(len(stage_order())):
        if current is None:
            return {}
        if current.stage == ScanTailorStageState.Stage.SELECT_CONTENT.value:
            return dict(current.parameters or {})
        current = current.input_stage_state
    return {}


def _run_margins(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    """Margins (#208): upstream ``page_layout::Task`` with the ``Utils`` port.

    Every page aligned with the others gets the job's aggregate hard size (the largest
    content plus hard margins, :func:`margins_aggregate`); its own hard margins are a
    minimum and the rest is split by its alignment. The aggregate and its fingerprint
    are recorded with the result; :func:`requeue_stale_margins` re-runs pages whose
    recorded fingerprint is no longer current.
    """
    parameters = dict(state.parameters or {})
    mode = str(parameters.get("margins_mode", "auto")).strip().lower()
    if mode not in {"auto", "manual"}:
        mode = "auto"
    dpi = (float(max(1, stage_input.dpi[0])), float(max(1, stage_input.dpi[1])))
    settings = page_layout.settings_from_parameters(parameters, dpi)
    content_found = _select_content_result(state).get("content_found") is not False
    content_px = stage_input.full_size if content_found else (0, 0)
    own_mm = page_layout.hard_size_mm(content_px, settings, dpi)
    if state.scan_job_id is not None:
        # The aggregate this run's input fingerprint was taken with (process_stage_job).
        aggregate = job_margins_aggregate(state)
    else:
        aggregate = page_layout.aggregate_hard_size(
            [page_layout.PageEntry("", own_mm, settings.align_with_others)]
        )
    # Settings::getAggregateHardSizeMM(page_id, hard_size, alignment): this page's
    # current hard size counts even if its recorded content size is older.
    aggregate_mm = (
        (max(aggregate.width_mm, own_mm[0]), max(aggregate.height_mm, own_mm[1]))
        if settings.align_with_others
        else aggregate.size_mm
    )
    rect = page_layout.page_rect(stage_input.full_size, content_found, settings, dpi, aggregate_mm)

    def sides(values: tuple[int, int, int, int]) -> dict[str, int]:
        return dict(zip(("left", "top", "right", "bottom"), values, strict=True))

    def sides_mm(values: tuple[int, int, int, int]) -> dict[str, float]:
        scale = (MM_PER_INCH / dpi[0], MM_PER_INCH / dpi[1])
        return {
            key: round(value * scale[index % 2], 2)
            for index, (key, value) in enumerate(sides(values).items())
        }

    # Explicit pixel canvases and the pixel margins recorded before #208 are replaced
    # by the common page size and the millimetre values below.
    for legacy in (
        "canvas_width",
        "canvas_height",
        "margin_top",
        "margin_right",
        "margin_bottom",
        "margin_left",
    ):
        parameters.pop(legacy, None)
    return _transform_result(
        parameters,
        stage_input,
        scan_transform.pad(rect.offset, rect.canvas, stage_input.full_size),
        {
            "margins_mode": mode,
            "hard_margins_mm": settings.hard_margins_mm.as_dict(2),
            "hard_margins_px": sides(rect.hard_px),
            "soft_margins_px": sides(rect.soft_px),
            "soft_margins_mm": sides_mm(rect.soft_px),
            "align_x": settings.align_x,
            "align_y": settings.align_y,
            "align_with_others": settings.align_with_others,
            "content_found": content_found,
            "content_size_mm": {
                "width": round(content_px[0] * MM_PER_INCH / dpi[0], 2),
                "height": round(content_px[1] * MM_PER_INCH / dpi[1], 2),
            },
            "hard_size_mm": {"width": round(own_mm[0], 2), "height": round(own_mm[1], 2)},
            "aggregate_size_mm": {
                "width": round(aggregate_mm[0], 3),
                "height": round(aggregate_mm[1], 3),
            },
            # Only aligned pages depend on the aggregate (input of #205's fingerprint).
            "aggregate_fingerprint": aggregate.fingerprint if settings.align_with_others else "",
            "page_size_mm": {
                "width": round(rect.canvas[0] * MM_PER_INCH / dpi[0], 2),
                "height": round(rect.canvas[1] * MM_PER_INCH / dpi[1], 2),
            },
            "dpi": [dpi[0], dpi[1]],
            "resolved_canvas_width": rect.canvas[0],
            "resolved_canvas_height": rect.canvas[1],
        },
    )


def blank_page_size(
    aggregate: page_layout.Aggregate, dpi: tuple[int, int], original_size: tuple[int, int]
) -> tuple[int, int]:
    if aggregate.width_mm and aggregate.height_mm:
        return (
            max(1, round(aggregate.width_mm * dpi[0] / MM_PER_INCH)),
            max(1, round(aggregate.height_mm * dpi[1] / MM_PER_INCH)),
        )
    # All-blank jobs and jobs whose normal pages have yet to reach Select Content.
    return original_size


def blank_canvas(size: tuple[int, int]) -> Image.Image:
    if Image.MAX_IMAGE_PIXELS is not None and size[0] * size[1] > Image.MAX_IMAGE_PIXELS:
        raise ImageLoadError("blank page canvas exceeds image size limit")
    return Image.new("L", size, color=255)


def _run_blank_margins(state: ScanTailorStageState, stage_input: StageInput) -> StageResult:
    aggregate = job_margins_aggregate(state)
    size = blank_page_size(aggregate, stage_input.dpi, stage_input.full_size)
    return StageResult(
        image=blank_canvas(size),
        full_size=size,
        parameters={
            "declared_blank": True,
            "aggregate_fingerprint": aggregate.fingerprint,
            "common_page_width_mm": aggregate.width_mm,
            "common_page_height_mm": aggregate.height_mm,
        },
        full_resolution=True,
    )


type _MarginsRow = tuple[int, int, str, dict[str, Any]]

#: Run states in which a row still holds its last successful result (#205).
_RESULT_KEPT_STATUSES: Final[frozenset[str]] = frozenset(
    {
        ScanTailorStageState.StageStatus.SUCCESS,
        ScanTailorStageState.StageStatus.QUEUED,
        ScanTailorStageState.StageStatus.RUNNING,
    }
)


def _margins_page_entries(
    scan_job_id: Any,
) -> tuple[list[page_layout.PageEntry], list[_MarginsRow]]:
    """Every page's contribution to the aggregate, plus the job's Margins rows.

    One query over the job's Select Content and Margins rows. A page contributes once
    its Select Content result is recorded: its content size in pixels, the page dpi
    and whether content was found, with the hard margins and alignment of its Margins
    settings. With #205 a row keeps its last result while it is queued or re-run, and
    that result keeps counting until the new one replaces it, so a re-run of one
    page's Select Content does not make every other page stale meanwhile (upstream
    drops the content size instead, ``invalidateContentSize``). Returns the entries
    and ``(id, revision, status, parameters)`` of each Margins row.
    """
    content: dict[Any, dict[str, Any]] = {}
    margins: dict[Any, _MarginsRow] = {}
    rows = ScanTailorStageState.objects.filter(
        scan_job_id=scan_job_id,
        stage__in=(
            ScanTailorStageState.Stage.SELECT_CONTENT.value,
            ScanTailorStageState.Stage.MARGINS.value,
        ),
    ).values_list(
        "id", "scan_page_id", "stage", "status", "revision", "parameters", "applied_revision"
    )
    for state_id, page_id, stage, status, revision, parameters, applied_revision in rows:
        values = parameters if isinstance(parameters, dict) else {}
        if stage == ScanTailorStageState.Stage.MARGINS.value:
            margins[page_id] = (state_id, revision, status, values)
        elif status == ScanTailorStageState.StageStatus.SUCCESS or (
            applied_revision is not None and status in _RESULT_KEPT_STATUSES
        ):
            content[page_id] = values
    entries: list[page_layout.PageEntry] = []
    for page_id, values in content.items():
        try:
            width = int(values["output_width"])
            height = int(values["output_height"])
        except (KeyError, TypeError, ValueError):
            continue
        raw_dpi = values.get("dpi")
        dpi = (float(DEFAULT_DPI), float(DEFAULT_DPI))
        if isinstance(raw_dpi, list | tuple) and len(raw_dpi) >= 2:
            dpi = (
                max(1.0, _coerce_float(raw_dpi[0], DEFAULT_DPI)),
                max(1.0, _coerce_float(raw_dpi[1], DEFAULT_DPI)),
            )
        found = values.get("content_found") is not False
        margin_parameters = margins[page_id][3] if page_id in margins else {}
        settings = page_layout.settings_from_parameters(margin_parameters, dpi)
        entries.append(
            page_layout.PageEntry(
                str(page_id),
                page_layout.hard_size_mm((width, height) if found else (0, 0), settings, dpi),
                settings.align_with_others,
            )
        )
    return entries, list(margins.values())


def margins_aggregate(job: ScanJob | Any) -> page_layout.Aggregate:
    """The job's aggregate hard size in mm and its fingerprint (#208).

    ``Settings::getAggregateHardSizeMM``: the largest content-plus-hard-margins width
    and height over the pages aligned with the others. ``job`` is a ScanJob or its
    id. Every Margins run records the fingerprint it used
    (``parameters["aggregate_fingerprint"]``); when the current one differs, the
    aligned pages' results are stale (:func:`requeue_stale_margins`).
    """
    scan_job_id = job.pk if isinstance(job, ScanJob) else job
    entries, _ = _margins_page_entries(scan_job_id)
    return page_layout.aggregate_hard_size(entries)


#: ``ProcessingJob.details["trigger"]`` of a Margins re-run caused by another page.
AGGREGATE_TRIGGER: Final[str] = "margins_aggregate_changed"


def requeue_stale_margins(job: ScanJob | Any) -> int:
    """Re-run the aligned pages' Margins results that used another common size (#208).

    Upstream reports ``AGGREGATE_SIZE_CHANGED`` and invalidates every page. Here the
    aggregate is part of an aligned page's Margins input fingerprint
    (:func:`stage_input_fingerprint`), so those results are already stale (reason
    :data:`COMMON_SIZE_CHANGED`) and their Output follows by itself. #205's chaining
    only runs a page's own next stage, while a change on page A must re-run Margins on
    pages B..N: this queues them, through :func:`request_stage_job_if_ready`, as one
    batch at the back of the queue. A page whose earlier stages are not current waits
    and is chained later; nothing is written as failed. The aggregate is computed once
    per call. Returns the number of pages queued.
    """
    scan_job_id = job.pk if isinstance(job, ScanJob) else job
    if ScanJob.objects.filter(pk=scan_job_id).values_list("state", flat=True).first() not in (
        scan_lifecycle.PROCESSING_STATES
    ):
        return 0
    entries, rows = _margins_page_entries(scan_job_id)
    aggregate = page_layout.aggregate_hard_size(entries)
    # Cheap pre-filter on the recorded fingerprint; the derived state below decides.
    candidates = {
        state_id
        for state_id, _revision, status, parameters in rows
        if status == ScanTailorStageState.StageStatus.SUCCESS
        and (parameters.get("declared_blank") is True or page_layout.aligns_with_others(parameters))
        and parameters.get("aggregate_fingerprint") != aggregate.fingerprint
    }
    if not candidates:
        return 0
    # The candidates' pages, every stage at once, linked in memory: two queries.
    page_ids = ScanTailorStageState.objects.filter(id__in=candidates).values_list(
        "scan_page_id", flat=True
    )
    by_page: dict[Any, dict[str, ScanTailorStageState]] = {}
    for state in ScanTailorStageState.objects.filter(
        scan_job_id=scan_job_id, scan_page_id__in=list(page_ids)
    ).select_related("input_scan_page_version"):
        by_page.setdefault(state.scan_page_id, {})[state.stage] = state
    job_ids: list[str] = []
    for page_states in by_page.values():
        link_page_states(page_states, aggregate=aggregate)
        margins_state = page_states.get(ScanTailorStageState.Stage.MARGINS.value)
        if margins_state is None or margins_state.id not in candidates:
            continue
        if margins_state.status != ScanTailorStageState.StageStatus.SUCCESS:
            continue  # queued or running meanwhile: that run reads the new aggregate
        processing_job, _waiting = request_stage_job_if_ready(
            state=margins_state,
            dispatch=False,
            batch=True,
            extra_details={
                "trigger": AGGREGATE_TRIGGER,
                "aggregate_fingerprint": aggregate.fingerprint,
            },
        )
        if processing_job is not None and not (processing_job.details or {}).get("superseded"):
            job_ids.append(str(processing_job.job_id))
    logger.info(
        "scantailor_margins_requeued",
        scan_job_id=str(scan_job_id),
        pages=len(job_ids),
        aggregate_width_mm=round(aggregate.width_mm, 2),
        aggregate_height_mm=round(aggregate.height_mm, 2),
    )
    dispatch_stage_batch(job_ids)
    return len(job_ids)


DEWARP_MODES: Final[tuple[str, ...]] = ("off", "auto", "manual")
#: Manual curves are bounded (#171: no runaway worker on hostile or mistyped geometry):
#: at most this many control points per curve ...
MAX_DEWARP_CONTROL_POINTS: Final[int] = 64
#: ... each within this many page sizes of the page (upstream's editor keeps the
#: points near the image; spline sampling time grows with the curve length).
DEWARP_POINT_REACH: Final[float] = 1.0
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


def _manual_curves_problem(curves: tuple[Points, Points], size: tuple[int, int]) -> str:
    """Why manual curves are refused before the model is built, or ``""``."""
    width, height = size
    reach_x, reach_y = DEWARP_POINT_REACH * width, DEWARP_POINT_REACH * height
    for points in curves:
        if len(points) > MAX_DEWARP_CONTROL_POINTS:
            return f"A curve has more than {MAX_DEWARP_CONTROL_POINTS} control points"
        for x, y in points:
            if not (-reach_x <= x <= width + reach_x and -reach_y <= y <= height + reach_y):
                return "A control point lies too far outside the page"
    return ""


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
            problem = _manual_curves_problem((top_points, bottom_points), (width, height))
            if problem:
                # Keep the reviewer's points so the editor shows what is wrong.
                updates["top_curve"] = [[x, y] for x, y in top_points]
                updates["bottom_curve"] = [[x, y] for x, y in bottom_points]
                updates["dewarp_warning"] = f"{problem}; the page is unchanged."
                return _transform_result(
                    parameters,
                    stage_input,
                    scan_transform.IDENTITY,
                    {**updates, "dewarp_applied": False},
                )
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


def _local_statistics(gray: np.ndarray, block: int) -> tuple[np.ndarray, np.ndarray]:
    mean = cv2.blur(gray, (block, block))
    mean_square = cv2.blur(gray * gray, (block, block))
    return mean, np.sqrt(np.maximum(mean_square - mean * mean, 0.0))


def _binarize_with_cv2(
    grayscale: Any,
    source: Image.Image,
    mode: str,
    *,
    window_size: int,
    k: float,
    r: float,
    invert: bool,
    wolf_statistics: tuple[float, float] | None = None,
    observe_threshold: Callable[[float | np.ndarray], None] | None = None,
) -> Image.Image:
    if cv2 is None or np is None:
        raise RuntimeError("opencv is not available")

    gray = grayscale
    if not isinstance(gray, np.ndarray):
        gray = np.array(source.convert("L"))

    threshold: float | np.ndarray
    if mode == "otsu":
        threshold, output = cv2.threshold(
            gray,
            0,
            255,
            cv2.THRESH_BINARY + cv2.THRESH_OTSU,
        )
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
        threshold = local_mean - offset
        output = (gray_f > threshold).astype(np.uint8) * 255
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
        local_mean, local_std = _local_statistics(gray_f, block)
        minimum, maximum_deviation = (
            wolf_statistics
            if wolf_statistics is not None
            else (float(gray.min()), float(local_std.max()))
        )
        max_std = max(maximum_deviation, 1e-3)
        a_coeff = min(max(k, 0.0), 1.0)
        threshold = (
            (1.0 - a_coeff) * local_mean
            + a_coeff * minimum
            + a_coeff * (local_std / max_std) * (local_mean - minimum)
        )
        black = (gray < 1) | ((gray <= 254) & (gray_f < threshold))
        output = (~black).astype(np.uint8) * 255
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
        threshold, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        output = cv2.bitwise_xor(binary, gradient_mask)
    else:
        raise RuntimeError(f"unsupported output mode '{mode}'")

    if observe_threshold is not None:
        observe_threshold(threshold)
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


def _threshold_tile_regions(
    source: Image.Image, halo: int
) -> Iterable[tuple[Box, Box, tuple[int, int]]]:
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
            yield (
                box,
                (left - box[0], top - box[1], right - box[0], bottom - box[1]),
                (left, top),
            )


def _wolf_page_statistics(source: Image.Image, block: int, halo: int) -> tuple[float, float]:
    minimum, maximum_deviation = 255.0, 0.0
    for box, (left, top, right, bottom), _ in _threshold_tile_regions(source, halo):
        checkpoint()
        tile = np.asarray(source.crop(box).convert("L"), dtype=np.float32)
        _, deviation = _local_statistics(tile, block)
        minimum = min(minimum, float(tile[top:bottom, left:right].min()))
        maximum_deviation = max(maximum_deviation, float(deviation[top:bottom, left:right].max()))
    return minimum, maximum_deviation


def _threshold_tiles(source: Image.Image, mode: str, parameters: dict[str, Any]) -> Image.Image:
    """Bound float-array scratch to a tile plus a ≤252-pixel local-window halo."""
    if mode == "copy":
        return source.copy()
    window = min(501, max(3, _coerce_int(parameters.get("window_size"), 35)))
    block = _odd_window(window)
    halo = block // 2 + 2
    result = Image.new("L", source.size, 255)
    cutoff = _pil_otsu_threshold(source) if mode == "otsu" else None
    wolf_statistics = _wolf_page_statistics(source, block, halo) if mode == "wolf" else None
    invert = _coerce_bool(parameters.get("invert"), False)
    for box, core, origin in _threshold_tile_regions(source, halo):
        checkpoint()
        tile = source.crop(box).convert("L")
        if cutoff is not None:
            processed = tile.point(lambda value: 255 if value > cutoff else 0)
            if invert:
                processed = ImageOps.invert(processed)
        else:
            processed = _binarize_with_cv2(
                np.asarray(tile),
                tile,
                mode,
                window_size=window,
                k=_coerce_float(parameters.get("k"), 0.33),
                r=_coerce_float(parameters.get("r"), 128.0),
                invert=invert,
                wolf_statistics=wolf_statistics,
            )
        result.paste(processed.crop(core), origin)
    return result


def _zones_to_full(zones: list[dict[str, Any]], stage_input: StageInput) -> list[dict[str, Any]]:
    """Auto-detected zones are found at working resolution; store them at full."""
    scaled = []
    for zone in zones:
        box = _box_to_full(tuple(int(value) for value in zone["box"]), stage_input)  # type: ignore[arg-type]
        scaled.append({**zone, "box": list(box)})
    return scaled


def _output_histogram(
    stage_input: StageInput, mode: str, parameters: dict[str, Any], *, zoned: bool = False
) -> dict[str, Any]:
    """Calculate the preview's grayscale histogram and the effective cutoff distribution."""
    preview = _thumbnail(stage_input.image, 960).convert("L")
    result: dict[str, Any] = {"bins": preview.histogram(), "mode": mode, "zoned": zoned}
    if mode == "copy":
        return result
    if mode == "otsu":
        result["threshold"] = {"median": _pil_otsu_threshold(preview), "kind": "global"}
        return result

    def record(threshold: float | np.ndarray) -> None:
        if isinstance(threshold, np.ndarray):
            low, median, high = np.percentile(threshold, [10, 50, 90]).tolist()
            result["threshold"] = {"low": low, "median": median, "high": high, "kind": "local"}
        else:
            result["threshold"] = {"median": threshold, "kind": "intensity"}

    scale = max(preview.size) / max(stage_input.full_size)
    window = min(501, max(3, _coerce_int(parameters.get("window_size"), 35)))
    preview_window = max(3, round(_odd_window(window) * scale))
    _binarize_with_cv2(
        np.asarray(preview),
        preview,
        mode,
        window_size=preview_window,
        k=_coerce_float(parameters.get("k"), 0.33),
        r=_coerce_float(parameters.get("r"), 128.0),
        invert=False,
        observe_threshold=record,
    )
    return result


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
                "output_histogram": _output_histogram(stage_input, mode, parameters),
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
    checkpoint(force=True)
    try:
        if zone_mode == "auto":
            zones = _zones_to_full(output_zones.detect_zones(stage_input.image), stage_input)
            checkpoint(force=True)
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
    except StageSuperseded:
        raise
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
        "output_histogram": _output_histogram(
            stage_input, "copy" if keep_color or warning else mode, parameters, zoned=bool(zones)
        ),
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
    if is_blank_margins(state):
        return _run_blank_margins
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


def _active_stage_job(state: ScanTailorStageState) -> ProcessingJob | None:
    """The queued or running job of ``state``'s current revision, if there is one."""
    try:
        job_id = uuid.UUID(state.worker_job_id)
    except ValueError:
        return None
    job = ProcessingJob.objects.filter(
        job_id=job_id,
        state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
    ).first()
    if job is None:
        return None
    details = job.details if isinstance(job.details, dict) else {}
    return job if details.get("stage_revision") == state.revision else None


@dataclass(frozen=True)
class EnsureResult:
    """What :func:`ensure_stage_result` found or did for one page and stage (#209).

    ``outcome`` is ``current`` (the result matches the settings and input, #205),
    ``waiting`` (an earlier stage has no current result: ``waiting_for`` names it),
    ``queued`` or ``running`` (a job of the current revision exists, or was queued now;
    ``job`` is it), or ``failed`` (the current settings already failed on this input;
    editing them, or a new result of the predecessor, runs the stage again).
    """

    outcome: str
    state: ScanTailorStageState
    waiting_for: str | None = None
    job: ProcessingJob | None = None


def _linked_page_state(state_id: int, *, lock: bool) -> ScanTailorStageState:
    """Read a stage row linked with its page's other rows (#205, #208).

    With ``lock`` the row is locked (inside ``transaction.atomic()``). The derived-state
    functions then follow the linked rows in memory, and the job's common page size is
    computed at most once (:func:`page_margins_aggregate`), not once per call.
    """
    rows = ScanTailorStageState.objects.select_related("input_scan_page_version")
    if lock:
        rows = rows.select_for_update(of=("self",))
    row = rows.get(pk=state_id)
    states = {
        other.stage: other
        for other in ScanTailorStageState.objects.filter(
            scan_job_id=row.scan_job_id, scan_page_id=row.scan_page_id
        )
        .exclude(pk=row.pk)
        .select_related("input_scan_page_version")
    }
    states[row.stage] = row
    link_page_states(states, aggregate=page_margins_aggregate(states))
    return row


def ensure_stage_result(
    state: ScanTailorStageState,
    *,
    requested_by_user_id: int | None = None,
) -> EnsureResult:
    """Make sure ``state`` gets a current result, queueing its stage at most once (#209).

    On #205's derived state: nothing is queued when the result is current, when an
    earlier stage has no current result (the predecessor's successful run queues this
    stage, :func:`queue_ready_successor`), when a job of the current revision is
    already queued or running (that job is returned), or when the current settings
    already failed. Otherwise the stage is queued through
    :func:`request_stage_job_if_ready` at the front of the queue: the page someone has
    open runs first. The row lock makes concurrent calls (two editors opening the same
    page, an autosave and an open) queue one job.
    """
    with transaction.atomic():
        locked = _linked_page_state(state.pk, lock=True)
        if stage_result_is_current(locked):
            return EnsureResult("current", locked)
        waiting_for = stage_waiting_for(locked)
        if waiting_for is not None:
            return EnsureResult("waiting", locked, waiting_for=waiting_for)
        active = _active_stage_job(locked)
        if active is not None:
            running = locked.status == ScanTailorStageState.StageStatus.RUNNING
            return EnsureResult("running" if running else "queued", locked, job=active)
        if locked.status == ScanTailorStageState.StageStatus.FAILED and _failed_on_current_input(
            locked
        ):
            return EnsureResult("failed", locked)
        job, waiting_for = request_stage_job_if_ready(
            state=locked, requested_by_user_id=requested_by_user_id, dispatch=False
        )
    if job is None:  # checked above under the same lock; kept for the type checker
        return EnsureResult("waiting" if waiting_for else "current", locked, waiting_for)
    if (job.details or {}).get("superseded"):
        return EnsureResult("queued", locked, job=job)
    dispatch_stage_job(str(job.job_id))
    # Without a queue the job ran inline: report the row as it is now.
    return EnsureResult("queued", _linked_page_state(locked.pk, lock=False), job=job)


def _failed_on_current_input(state: ScanTailorStageState) -> bool:
    """Whether ``state``'s failed run read the input the stage would read now (#209).

    The failed job records the input fingerprint it ran on. A failure on an older input
    (a replacement scan, a predecessor that changed without chaining here) is worth
    running again; the same failure on the same input is not. A failure without a
    recorded fingerprint counts as current, so opening a page never loops on it.
    """
    try:
        job_id = uuid.UUID(state.worker_job_id)
    except ValueError:
        return True
    details = ProcessingJob.objects.filter(job_id=job_id).values_list("details", flat=True).first()
    if not isinstance(details, dict) or details.get("stage_revision") != state.revision:
        return True
    recorded = details.get("input_fingerprint")
    if not isinstance(recorded, str):
        return True
    return recorded == stage_input_fingerprint(state)


def request_stage_job(
    *,
    state: ScanTailorStageState,
    requested_by_user_id: int | None = None,
    dispatch: bool = True,
    batch: bool = False,
    extra_details: dict[str, Any] | None = None,
) -> ProcessingJob:
    """Create and dispatch the worker job for ``state`` at its current revision.

    The job records the stage revision it was dispatched for (``stage_revision``) and
    only takes over the stage's ``worker_job_id`` while the row is still at that
    revision. If an edit bumped the revision in between, the job is closed as
    superseded right away and never dispatched, so it cannot replace the newer
    edit's artifact; :func:`process_stage_job` repeats the check on execution.
    ``batch`` marks work from "apply to pages"; stages it chains to queue at the back.

    The row's ``worker_job_id`` names the one job that may run and commit the stage.
    The job it replaces is closed as superseded right away if it is still waiting in
    the queue (#209); if it is already running it stops at its next cancellation
    checkpoint (:func:`process_stage_job`).
    """
    revision = state.revision
    previous_job_id = state.worker_job_id
    details: dict[str, Any] = {
        "scan_job_id": str(state.scan_job_id),
        "scan_tailor_stage_state_id": state.id,
        "requested_by_user_id": requested_by_user_id,
        "stage": state.stage,
        "stage_revision": revision,
    }
    if batch:
        details["batch"] = True
    if extra_details:
        details.update(extra_details)
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.STAGE,
        state=ProcessingJob.JobState.QUEUED,
        details=details,
    )
    algorithm = state.algorithm or _default_algorithm(state.stage)
    if state.stage in {"deskew", "select_content", "dewarping"}:
        # Pre-port algorithm names run the port now (#195 steps 4 to 6).
        algorithm = _default_algorithm(state.stage)
    algorithm_version = _default_algorithm_version(state.stage, algorithm)
    if is_blank_margins(state):
        algorithm_version = "declared-blank-v1"
    now = timezone.now()
    claimed = ScanTailorStageState.objects.filter(id=state.id, revision=revision).update(
        worker_job_id=str(job.job_id),
        status=ScanTailorStageState.StageStatus.QUEUED,
        algorithm=algorithm,
        algorithm_version=algorithm_version,
        last_error="",
        updated_at=now,
    )
    if not claimed:
        _supersede_stage_job(job, dict(job.details), "stage revision changed before dispatch")
        return job
    if previous_job_id and previous_job_id != str(job.job_id):
        _drop_queued_stage_job(previous_job_id)
    state.worker_job_id = str(job.job_id)
    state.status = ScanTailorStageState.StageStatus.QUEUED
    state.last_error = ""
    state.algorithm = algorithm
    state.algorithm_version = algorithm_version
    state.updated_at = now
    if dispatch:
        dispatch_stage_job(str(job.job_id), at_front=not batch)
    return job


def request_stage_job_if_ready(
    *,
    state: ScanTailorStageState,
    requested_by_user_id: int | None = None,
    dispatch: bool = True,
    batch: bool = False,
    extra_details: dict[str, Any] | None = None,
) -> tuple[ProcessingJob | None, str | None]:
    """Queue ``state`` only if it can run and its result is not current (#205).

    Returns ``(job, waiting_for)``. A stage whose predecessors are not all successful
    and current gets no job: ``waiting_for`` names the first such stage, and the stage
    is queued by :func:`queue_ready_successor` once that predecessor chain completes.
    A stage whose result already matches its settings and input needs no job either.
    """
    waiting = stage_waiting_for(state)
    if waiting is not None:
        return None, waiting
    if stage_result_is_current(state):
        return None, None
    job = request_stage_job(
        state=state,
        requested_by_user_id=requested_by_user_id,
        dispatch=dispatch,
        batch=batch,
        extra_details=extra_details,
    )
    return job, None


def queue_ready_successor(
    state: ScanTailorStageState,
    *,
    requested_by_user_id: int | None = None,
    batch: bool = False,
) -> ProcessingJob | None:
    """After ``state`` succeeded, queue the next stage of its page if it is stale (#205).

    The next stage runs with its saved settings (defaults if never edited) when its
    result is not current and every stage before it is. A stage already queued keeps
    its pending job, which reads the new input when it runs.
    """
    next_name = next(iter(downstream_stages(state.stage)), None)
    if next_name is None or not stage_result_is_current(state):
        return None
    if ScanJob.objects.filter(pk=state.scan_job_id).values_list("state", flat=True).first() not in (
        scan_lifecycle.PROCESSING_STATES
    ):
        return None
    if batch:
        # A stage-specific batch re-runs only existing results made stale by this
        # page's new output. Workspace initialization also creates never-run rows.
        successor = ScanTailorStageState.objects.filter(
            scan_page_id=state.scan_page_id, stage=next_name, applied_revision__isnull=False
        ).first()
        if successor is None:
            return None
    else:
        successor = get_or_create_stage_state(
            scan_job_id=str(state.scan_job_id), page_id=str(state.scan_page_id), stage=next_name
        )
    if successor.input_stage_state_id != state.id:
        return None
    successor.input_stage_state = state
    if successor.status == ScanTailorStageState.StageStatus.QUEUED:
        return None
    job, _ = request_stage_job_if_ready(
        state=successor,
        requested_by_user_id=requested_by_user_id,
        batch=batch,
    )
    return job


# Pages per batch job: big enough to amortise the per-job overhead, small enough that
# an interactive edit queued at the front never waits long behind a running chunk.
STAGE_BATCH_CHUNK: Final[int] = 8


def _drop_queued_stage_job(job_id: str) -> None:
    """Close an older, not yet started job of a stage as superseded (#209)."""
    try:
        key = uuid.UUID(job_id)
    except ValueError:
        return
    older = ProcessingJob.objects.filter(job_id=key, state=ProcessingJob.JobState.QUEUED).first()
    if older is not None:
        details = older.details if isinstance(older.details, dict) else {}
        _supersede_stage_job(older, details, "replaced by a newer run of the stage")


def dispatch_stage_job(job_id: str, *, at_front: bool = True) -> None:
    """Run one interactive stage job before any queued batch work (#195 §8.2 item 11).

    Upstream Scan Tailor processes the page the user is looking at first. Here a
    single-page edit goes to the front of the queue; batch chunks go to the back, and
    so do the stages a batch job chains to (``at_front=False``).
    """
    queue = get_queue()
    if queue is None:
        process_stage_job(job_id)
        return
    queue.enqueue(process_stage_job, job_id, at_front=at_front)


def process_stage_jobs(job_ids: list[str]) -> None:
    """Run a chunk of batch stage jobs; each keeps its own revision checks."""
    for job_id in job_ids:
        process_stage_job(job_id)


def run_pipeline_step(
    run: ScanTailorRun, page_id: str, stage: str
) -> Literal["current", "failed", "busy"]:
    """Use the current authored settings and finish one ready, unprocessed step."""
    if not ScanPage.objects.filter(pk=page_id, scan_job_id=run.scan_job_id).exists():
        return "current"
    state = get_or_create_stage_state(scan_job_id=run.scan_job_id, page_id=page_id, stage=stage)
    with transaction.atomic():
        if (
            not ScanTailorRun.objects.select_for_update()
            .filter(pk=run.pk, state=ScanTailorRun.State.RUNNING)
            .exists()
        ):
            return "failed"
        state = _linked_page_state(state.pk, lock=True)
        if stage_result_is_current(state):
            return "current"
        if stage_waiting_for(state) is not None:
            return "failed"
        active = _active_stage_job(state)
        if active is not None:
            age = (timezone.now() - (active.started_at or active.scheduled_at)).total_seconds()
            return (
                "busy" if age < django_settings.BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS else "failed"
            )
        job = request_stage_job(
            state=state,
            requested_by_user_id=run.requested_by_id,
            dispatch=False,
            batch=True,
            extra_details={"pipeline_run_id": run.pk},
        )
    process_stage_job(str(job.job_id))
    return (
        "current" if stage_result_is_current(_linked_page_state(state.pk, lock=False)) else "failed"
    )


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


def _result_parameters(state: ScanTailorStageState) -> dict[str, Any]:
    """Parameters recorded by the run that produced ``state``'s result (#205)."""
    return dict(state.applied_parameters or state.parameters or {})


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
    chain = [] if is_blank_margins(state) else _upstream_chain(state)
    version = _page_version(state, chain)
    page_image = scan_images.ensure_page_image(version)
    ops: list[scan_transform.Op] = []
    if not chain:
        work_identifier = page_image.work_storage_identifier
        full_size = (page_image.full_width, page_image.full_height)
    else:
        for upstream in chain:
            # What the upstream result was computed with (#205), not settings that
            # were edited since: only current upstream results are read.
            op = _result_parameters(upstream).get("transform")
            if not isinstance(op, dict):
                raise RuntimeError(
                    f"upstream stage {upstream.stage} has no recorded transform; re-run it"
                )
            ops.append(op)
        predecessor = chain[-1]
        work_identifier = predecessor.output_storage_identifier
        parameters = _result_parameters(predecessor)
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
    pipeline_run_id = details.get("pipeline_run_id")
    if isinstance(pipeline_run_id, int) and not _pipeline_run_is_current(pipeline_run_id):
        release_pipeline_stage(state, job_id)
        _supersede_stage_job(job, details, "Run all cancelled")
        return

    # A job only ever produces the artifact of the revision it was queued for. A late
    # job (e.g. one overtaken by a manual edit) is a no-op.
    dispatched_revision = details.get("stage_revision")
    if isinstance(dispatched_revision, int) and dispatched_revision != state.revision:
        _supersede_stage_job(job, details, "stage revision changed after dispatch")
        return

    # A predecessor changed after this job was queued (#205): this is not a failure.
    # The stage waits; the predecessor's successful run queues it again.
    waiting = stage_waiting_for(state)
    if waiting is not None:
        _park_waiting_stage_job(job, details, state, waiting)
        return

    now = timezone.now()
    job.state = ProcessingJob.JobState.RUNNING
    job.started_at = now
    started_revision = state.revision
    claimed = ScanTailorStageState.objects.filter(
        id=state.id,
        revision=started_revision,
        worker_job_id=str(job.job_id),
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

    def still_current() -> bool:
        # The same rule as the commit (_locked_if_current): a newer edit bumps the
        # revision, and a newer job for the same revision (#205) takes over
        # ``worker_job_id``. Either way this run's result would be discarded (#209).
        if isinstance(pipeline_run_id, int) and not _pipeline_run_is_current(pipeline_run_id):
            return False
        return ScanTailorStageState.objects.filter(
            id=state.id,
            revision=started_revision,
            status=ScanTailorStageState.StageStatus.RUNNING,
            worker_job_id=str(job.job_id),
        ).exists()

    input_fingerprint: str | None = None
    try:
        storage = _scan_storage()
        storage_location = _scan_storage_location()
        artifact_identifier = output_identifier(state)
        preview_identifier = output_identifier(state, preview=True)

        runner = _resolve_stage_algorithm(state)
        # Per-phase timings for the structured log (#195 section 8.3).
        timer_start = time.perf_counter()
        # Recorded with the result (#205): what this run reads, taken before reading it.
        # Margins computes with the same job aggregate its fingerprint records (#208).
        if state.stage == ScanTailorStageState.Stage.MARGINS.value:
            attach_margins_aggregate([state], margins_aggregate(state.scan_job_id))
        input_fingerprint = stage_input_fingerprint(state)
        # Cooperative cancellation (#209): between the steps below, and between tiles
        # or column blocks inside long steps, a superseded run stops.
        with cancellable(still_current):
            stage_input = _stage_input(state, storage)
            input_done = time.perf_counter()
            checkpoint(force=True)
            result = runner(state, stage_input)
            run_done = time.perf_counter()
            checkpoint(force=True)
        with tempfile.TemporaryDirectory(prefix="stages-") as tempdir:
            staged_output = Path(tempdir) / _build_output_name(state)
            staged_preview = Path(tempdir) / _build_output_name(state, preview=True)
            staged_thumbnail = Path(tempdir) / STAGE_THUMBNAIL_NAME
            _write_stage_artifacts(
                result, stage_input, staged_output, staged_preview, staged_thumbnail
            )
            encode_done = time.perf_counter()
            if not still_current():
                raise StageSuperseded("stage edited during encoding")
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
            if not still_current():
                raise StageSuperseded("stage edited during hashing")
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
                input_fingerprint=input_fingerprint,
                job_id=str(job.job_id),
            ):
                _supersede_stage_job(job, details, "stage edited while job was running")
                return
            publish_done = time.perf_counter()
        # The committed revision replaces the earlier ones on disk (#204).
        _prune_quietly(scan_artifact_pruning.prune_superseded_revisions, state)

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
    except StageSuperseded as exc:
        # Stopped early for a newer revision (#209); the temporary files are gone and
        # nothing was staged in scan storage yet. The stage row belongs to the new run.
        if isinstance(pipeline_run_id, int):
            release_pipeline_stage(state, job_id)
        _supersede_stage_job(job, details, str(exc))
    except Exception as exc:
        with transaction.atomic():
            if _locked_if_current(state.id, started_revision, str(job.job_id)) is not None:
                state.status = ScanTailorStageState.StageStatus.FAILED
                state.last_error = str(exc)
                state.finished_at = timezone.now()
                state.save(update_fields=["status", "last_error", "finished_at", "updated_at"])
        _fail_stage_job(job, str(exc), input_fingerprint=input_fingerprint)
        return

    if isinstance(pipeline_run_id, int):
        # Run all advances by stage across the book. This lets Margins see the
        # settled common size and leaves room for interactive jobs between chunks.
        return

    # Chaining (#205): the next stage of the page runs now if its result is stale.
    requested_by = details.get("requested_by_user_id")
    queue_ready_successor(
        state,
        requested_by_user_id=requested_by if isinstance(requested_by, int) else None,
        batch=bool(details.get("batch")),
    )
    if state.stage in _AGGREGATE_INPUT_STAGES:
        # The one cross-page dependency (#208): other pages' Margins may be stale now.
        _requeue_after_layout_change(state)


#: Stages whose results feed the Margins aggregate: content size and margin settings.
_AGGREGATE_INPUT_STAGES: Final[frozenset[str]] = frozenset(
    {ScanTailorStageState.Stage.SELECT_CONTENT.value, ScanTailorStageState.Stage.MARGINS.value}
)


def _requeue_after_layout_change(state: ScanTailorStageState) -> None:
    """After a Select Content or Margins result: re-run Margins results now stale (#208).

    A failure here is logged and never fails the stage that just succeeded.
    """
    try:
        requeue_stale_margins(state.scan_job_id)
    except Exception:
        logger.exception(
            "scantailor_margins_requeue_failed",
            scan_job_id=str(state.scan_job_id),
            stage=state.stage,
        )


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
    input_fingerprint: str = "",
    job_id: str | None = None,
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
            if _locked_if_current(state.id, revision, job_id) is None:
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
            # The result's fingerprint (#205): input, settings as run, revision.
            state.applied_input_sha256 = input_fingerprint
            state.applied_parameters = dict(state.parameters or {})
            state.applied_revision = revision
            # A chained rerun (#205) reads the predecessor's newest output; record that
            # file as this row's input so the superseded upstream revision is no longer
            # referenced and can be pruned (#204).
            if state.input_stage_state_id is not None and input_fingerprint:
                used_input = (
                    ScanTailorStageState.objects.filter(
                        pk=state.input_stage_state_id, output_sha256=input_fingerprint
                    )
                    .values_list("output_storage_identifier", flat=True)
                    .first()
                )
                if used_input:
                    state.input_storage_identifier = used_input
            state.save(
                update_fields=[
                    "input_storage_identifier",
                    "applied_input_sha256",
                    "applied_parameters",
                    "applied_revision",
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


def _locked_if_current(
    state_id: int, revision: int, job_id: str | None = None
) -> ScanTailorStageState | None:
    """Lock the stage row; return it only if this job's run is still the current one.

    A user edit bumps ``revision``; a newer job for the same revision (queued when the
    predecessor's result changed, #205) takes over ``worker_job_id`` and the status.
    In either case this job's result is stale and must not overwrite the newer
    parameters/status.
    """
    with transaction.atomic():
        fresh = ScanTailorStageState.objects.select_for_update().filter(id=state_id).first()
        if fresh is None:
            return None
        if fresh.revision != revision:
            return None
        if fresh.status != ScanTailorStageState.StageStatus.RUNNING:
            return None
        if job_id is not None and fresh.worker_job_id != job_id:
            return None
        return fresh


def _park_waiting_stage_job(
    job: ProcessingJob,
    details: dict[str, Any],
    state: ScanTailorStageState,
    waiting_for: str,
) -> None:
    """Close a job whose predecessor is not current any more, without failing the stage.

    If the row still waits on this job, it returns to its idle status (its previous
    result, if any, stays as a stale result). The predecessor's next successful run
    queues the stage again (:func:`queue_ready_successor`).

    That run may have finished between this job's readiness check and the park: its
    chaining then saw the row still QUEUED and skipped it. So after parking, readiness
    is checked once more against fresh rows; a stage that can run now is queued here
    instead of waiting for a wake-up that already happened.
    """
    parked = False
    with transaction.atomic():
        fresh = ScanTailorStageState.objects.select_for_update().filter(id=state.id).first()
        if (
            fresh is not None
            and fresh.worker_job_id == str(job.job_id)
            and fresh.status == ScanTailorStageState.StageStatus.QUEUED
        ):
            fresh.status = stage_idle_status(fresh)
            fresh.save(update_fields=["status", "updated_at"])
            parked = True
    _supersede_stage_job(job, {**details, "waiting_for": waiting_for}, f"waiting for {waiting_for}")
    if not parked or isinstance(details.get("pipeline_run_id"), int):
        return
    current = ScanTailorStageState.objects.filter(id=state.id).first()
    if current is None or current.status == ScanTailorStageState.StageStatus.QUEUED:
        return
    if ScanJob.objects.filter(pk=current.scan_job_id).values_list(
        "state", flat=True
    ).first() not in (scan_lifecycle.PROCESSING_STATES):
        return
    requested_by = details.get("requested_by_user_id")
    request_stage_job_if_ready(
        state=current,
        requested_by_user_id=requested_by if isinstance(requested_by, int) else None,
        batch=bool(details.get("batch")),
    )


def _pipeline_run_is_current(run_id: int) -> bool:
    return ScanTailorRun.objects.filter(
        pk=run_id,
        state=ScanTailorRun.State.RUNNING,
        scan_job__state__in=scan_lifecycle.PROCESSING_STATES,
    ).exists()


def release_pipeline_stage(state: ScanTailorStageState, job_id: str) -> None:
    ScanTailorStageState.objects.filter(
        pk=state.pk,
        revision=state.revision,
        worker_job_id=job_id,
        status__in=[
            ScanTailorStageState.StageStatus.QUEUED,
            ScanTailorStageState.StageStatus.RUNNING,
        ],
    ).update(status=stage_idle_status(state), worker_job_id="", updated_at=timezone.now())


def _prune_quietly(prune: Callable[..., int], *args: object) -> None:
    """Artifact cleanup (#204) never changes a stage job's outcome."""
    try:
        prune(*args)
    except Exception:
        logger.exception("scantailor_artifact_prune_failed", prune=prune.__name__)


def _discard_job_artifacts(details: dict[str, Any]) -> None:
    """A closed job's files of its own revision go, unless a stage row uses them (#204)."""
    _prune_quietly(
        scan_artifact_pruning.discard_revision_artifacts,
        details.get("scan_tailor_stage_state_id"),
        details.get("stage_revision"),
    )


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
    _discard_job_artifacts(details)


def _fail_stage_job(
    job: ProcessingJob, message: str, *, input_fingerprint: str | None = None
) -> None:
    details = job.details if isinstance(job.details, dict) else {}
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = {**details, "error": message}
    if input_fingerprint is not None:
        # What the failed run read: opening the stage retries it on a new input (#209).
        job.details["input_fingerprint"] = input_fingerprint
    job.save(update_fields=["state", "finished_at", "details"])
    logger.error(
        "scantailor_stage_failed",
        processing_job_id=str(job.job_id),
        stage=details.get("stage"),
    )
    _discard_job_artifacts(details)
