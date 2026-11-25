"""Delete ScanTailor-Web stage artifacts nothing refers to any more (#204).

Every saved edit gives a stage a new revision, and every revision writes its own
``scan-jobs/<job>/<page>/<stage>/v<revision>/`` folder (working image or full
output, preview, strip thumbnail). Those files are derived and regenerable. Only
the revision a ``ScanTailorStageState`` row points at is still needed; this module
removes the rest:

* :func:`prune_superseded_revisions` -- after a stage run committed, the files of
  the stage's earlier revisions.
* :func:`discard_revision_artifacts` -- when a stage job is closed as superseded or
  failed, the files it left in its own revision folder.
* :func:`sweep` -- the periodic sweep (``manage.py prune_scan_artifacts``) over the
  whole ``scan-jobs/`` tree.
* :func:`prune_scan_job_stage_artifacts` -- all stage artifacts of a scan job that
  finished or was cancelled.
* :func:`low_space_message` -- the free-space guard checked before a stage job is
  queued.

A file is kept whenever any stage row (of any stage) names it in
``input_storage_identifier``, ``output_storage_identifier`` or
``preview_storage_identifier``; a strip thumbnail is kept with the output next to
it. Only files in the stage layout are ever touched: the raw scanner pages, covers
and logs that share ``scan-jobs/<job>/`` are originals and never match it.
Deletion goes through the storage backend; a failure is logged, never raised.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

import structlog
from django.conf import settings as django_settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ebook_server.storage import LocalFilesystemStorage, StorageEntry, StorageUnavailableError

from .models import ScanJob, ScanTailorStageState
from .scan_job_ids import normalize_scan_job_id

logger = structlog.get_logger(__name__)

STAGE_NAMESPACE: Final[str] = "scan-jobs"
STAGE_ARTIFACT_NAMES: Final[frozenset[str]] = frozenset(
    {"work.png", "preview.jpg", "output.png", "preview.png", "thumbnail.jpg"}
)
THUMBNAIL_NAME: Final[str] = "thumbnail.jpg"
# Partial copies of a worker that died before its commit. A live worker renames or
# discards its own within seconds, so only old ones are swept.
PARTIAL_NAME: Final = re.compile(r"^\.[0-9a-f]{32}(\.(output|preview|thumbnail))?\.partial$")
STAGE_FILE: Final = re.compile(
    r"^scan-jobs/(?P<job>[^/]+)/(?P<page>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{12})/(?P<stage>[a-z_]+)/v(?P<revision>[1-9][0-9]*)/(?P<name>[^/]+)$"
)
DEFAULT_SWEEP_MIN_AGE_SECONDS: Final[int] = 3600
DEFAULT_SWEEP_LIMIT: Final[int] = 2_000_000
JOB_END_REASON: Final[str] = "stage files removed when the scan job ended"


def _storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(str(Path(django_settings.APP_DATA_DIR) / "scan-artifacts"))


def _stages() -> frozenset[str]:
    return frozenset(stage.value for stage in ScanTailorStageState.Stage)


@dataclass(frozen=True)
class StageFile:
    identifier: str
    job: str
    page: str
    stage: str
    revision: int
    name: str

    @property
    def folder(self) -> str:
        return self.identifier.rsplit("/", 1)[0]

    @property
    def partial(self) -> bool:
        return PARTIAL_NAME.match(self.name) is not None


def parse_stage_file(identifier: str) -> StageFile | None:
    """The stage-layout parts of ``identifier``; ``None`` for anything else."""
    match = STAGE_FILE.match(identifier)
    if match is None or match["stage"] not in _stages():
        return None
    name = match["name"]
    if name not in STAGE_ARTIFACT_NAMES and PARTIAL_NAME.match(name) is None:
        return None
    return StageFile(
        identifier,
        match["job"],
        match["page"],
        match["stage"],
        int(match["revision"]),
        name,
    )


@dataclass
class References:
    """Stage-artifact ids still named by a stage row, and the folders of their outputs."""

    identifiers: set[str] = field(default_factory=set)
    output_folders: set[str] = field(default_factory=set)

    def add(self, input_id: str, output_id: str, preview_id: str) -> None:
        for identifier in (input_id, output_id, preview_id):
            if identifier:
                self.identifiers.add(identifier)
        if output_id:
            self.output_folders.add(output_id.rsplit("/", 1)[0])

    def holds(self, item: StageFile) -> bool:
        if item.identifier in self.identifiers:
            return True
        # The strip thumbnail has no column; it belongs to the output beside it.
        return item.name == THUMBNAIL_NAME and item.folder in self.output_folders


def references(prefix: str = STAGE_NAMESPACE + "/") -> References:
    """Every identifier under ``prefix`` that any stage row refers to."""
    rows = ScanTailorStageState.objects.filter(
        Q(input_storage_identifier__startswith=prefix)
        | Q(output_storage_identifier__startswith=prefix)
        | Q(preview_storage_identifier__startswith=prefix)
    ).values_list(
        "input_storage_identifier", "output_storage_identifier", "preview_storage_identifier"
    )
    result = References()
    for input_id, output_id, preview_id in rows.iterator():
        result.add(input_id, output_id, preview_id)
    return result


def _list(
    storage: LocalFilesystemStorage,
    prefix: str,
    limit: int = DEFAULT_SWEEP_LIMIT,
    *,
    strict: bool = False,
) -> list[tuple[StageFile, StorageEntry]]:
    """Stage files under ``prefix`` with their storage entries (size, mtime).

    A missing folder lists as empty. Any other listing failure is logged and lists
    as empty too, unless ``strict``: then it is raised, so the sweep reports it
    instead of claiming there was nothing to delete.
    """
    try:
        if not storage.resolve_location(prefix).is_dir():
            return []
        entries = list(storage.inventory(limit=limit, prefix=prefix))
    except (OSError, ValueError, StorageUnavailableError):
        if strict:
            raise
        logger.warning("scan_artifact_listing_failed", prefix=prefix, exc_info=True)
        return []
    found: list[tuple[StageFile, StorageEntry]] = []
    for entry in entries:
        item = parse_stage_file(entry.identifier)
        if item is not None and entry.regular:
            found.append((item, entry))
    return found


def _delete(storage: LocalFilesystemStorage, item: StageFile) -> bool:
    try:
        storage.delete(item.identifier)
    except (OSError, ValueError):
        logger.warning("scan_artifact_delete_failed", identifier=item.identifier, exc_info=True)
        return False
    return True


def _remove_empty_folders(storage: LocalFilesystemStorage, folder: str, stop_at: str) -> None:
    """``rmdir`` ``folder`` and its empty parents, never ``stop_at`` or above it."""
    try:
        boundary = storage.resolve_location(stop_at)
        current = storage.resolve_location(folder)
    except ValueError:
        return
    while current != boundary and boundary in current.parents:
        try:
            current.rmdir()  # only succeeds when the folder is empty
        except OSError:
            return
        current = current.parent


def _stage_prefix(state: ScanTailorStageState) -> str:
    return f"{STAGE_NAMESPACE}/{state.scan_job_id}/{state.scan_page_id}/{state.stage}/"


# --- 1. superseded revisions ---------------------------------------------------------


def prune_superseded_revisions(state: ScanTailorStageState) -> int:
    """Delete the files of ``state``'s revisions older than the one just committed.

    Called after the commit. A revision below the current one is never written
    again (a commit requires the row's current revision), so only references can
    keep its files. Partial copies are left to their worker and to the sweep.

    The run also rebound ``state`` to its upstream stage's current output, which
    can release an older upstream revision that only this row still named; those
    files go too. Returns the number of files deleted.
    """
    storage = _storage()
    deleted = _prune_older_revisions(storage, state)
    if state.input_stage_state_id is not None:
        upstream = ScanTailorStageState.objects.filter(pk=state.input_stage_state_id).first()
        if upstream is not None:
            deleted += _prune_older_revisions(storage, upstream)
    return deleted


def _prune_older_revisions(storage: LocalFilesystemStorage, state: ScanTailorStageState) -> int:
    prefix = _stage_prefix(state)
    stale = [
        item
        for item, _entry in _list(storage, prefix)
        if item.revision < state.revision and not item.partial
    ]
    if not stale:
        return 0
    held = references(prefix)
    deleted = 0
    folders: set[str] = set()
    for item in stale:
        if held.holds(item):
            continue
        if _delete(storage, item):
            deleted += 1
            folders.add(item.folder)
    for folder in folders:
        _remove_empty_folders(storage, folder, prefix.rstrip("/"))
    if deleted:
        logger.info(
            "scantailor_superseded_revisions_pruned",
            stage_state_id=state.pk,
            stage=state.stage,
            revision=state.revision,
            files=deleted,
        )
    return deleted


# --- 2. superseded / failed jobs -----------------------------------------------------


def discard_revision_artifacts(stage_state_id: object, revision: object) -> int:
    """Delete what a closed (superseded or failed) stage job left for its revision.

    Runs under the stage row's lock, the same lock a commit takes, so a file another
    job of the same revision is committing is either already referenced or not yet
    there. Files any row refers to are kept. Returns the number of files deleted.
    """
    if not isinstance(stage_state_id, int) or not isinstance(revision, int):
        return 0
    storage = _storage()
    deleted = 0
    with transaction.atomic():
        state = ScanTailorStageState.objects.select_for_update().filter(id=stage_state_id).first()
        if state is None:
            return 0
        folder = f"{_stage_prefix(state)}v{revision}"
        items = [item for item, _entry in _list(storage, folder + "/") if not item.partial]
        if not items:
            return 0
        held = references(folder + "/")
        for item in items:
            if not held.holds(item) and _delete(storage, item):
                deleted += 1
    _remove_empty_folders(storage, folder, _stage_prefix(state).rstrip("/"))
    if deleted:
        logger.info(
            "scantailor_closed_job_artifacts_removed",
            stage_state_id=stage_state_id,
            revision=revision,
            files=deleted,
        )
    return deleted


# --- 3. sweep ------------------------------------------------------------------------


@dataclass
class SweepReport:
    files: int = 0
    bytes: int = 0
    kept: int = 0
    failed: int = 0
    dry_run: bool = False

    def as_dict(self) -> dict[str, int | bool]:
        return {
            "files": self.files,
            "bytes": self.bytes,
            "kept": self.kept,
            "failed": self.failed,
            "dry_run": self.dry_run,
        }


def sweep(
    *,
    dry_run: bool = False,
    min_age_seconds: int = DEFAULT_SWEEP_MIN_AGE_SECONDS,
    limit: int = DEFAULT_SWEEP_LIMIT,
    prefix: str = STAGE_NAMESPACE + "/",
) -> SweepReport:
    """Delete stage files under ``prefix`` that no stage row refers to.

    The tree is listed before the references are read, and only files older than
    ``min_age_seconds`` are candidates, so a run that commits meanwhile (file
    renamed into place, row saved a moment later) is never swept. ``dry_run``
    counts what would go and deletes nothing. A listing failure (for example more
    than ``limit`` entries) raises instead of reporting nothing to delete.
    """
    storage = _storage()
    report = SweepReport(dry_run=dry_run)
    listed = _list(storage, prefix, limit, strict=True)
    held = references(prefix)
    cutoff_ns = time.time_ns() - max(min_age_seconds, 0) * 1_000_000_000
    folders: set[str] = set()
    for item, entry in listed:
        if held.holds(item):
            report.kept += 1
            continue
        size = entry.size_bytes
        if entry.mtime_ns > cutoff_ns:
            report.kept += 1
            continue
        if dry_run:
            report.files += 1
            report.bytes += size
            continue
        if _delete(storage, item):
            report.files += 1
            report.bytes += size
            folders.add(item.folder)
        else:
            report.failed += 1
    for folder in folders:
        _remove_empty_folders(storage, folder, STAGE_NAMESPACE)
    logger.info("scan_artifact_sweep_finished", **report.as_dict())
    return report


def sweep_job() -> dict[str, int | bool]:
    """RQ entry point for the periodic sweep."""
    return sweep().as_dict()


# --- 4. job end ----------------------------------------------------------------------


def prune_scan_job_stage_artifacts(scan_job_id: str) -> int:
    """Remove every stage artifact of a finished or cancelled scan job.

    The stage rows lose their output/preview references first (status
    ``invalidated``), so a job reopened for a re-finish reruns its stages instead of
    pointing at deleted files; then the now unreferenced files go. Raw pages and
    other originals in the job folder are not stage files and stay. Returns the
    number of files deleted.
    """
    if normalize_scan_job_id(scan_job_id) != scan_job_id:
        return 0  # never build a storage prefix from anything but a stored job id
    if not ScanJob.objects.filter(
        pk=scan_job_id, state__in=[ScanJob.State.COMPLETE, ScanJob.State.CANCELLED]
    ).exists():
        return 0
    ScanTailorStageState.objects.filter(scan_job_id=scan_job_id).exclude(
        output_storage_identifier="", preview_storage_identifier=""
    ).update(
        output_storage_identifier="",
        preview_storage_identifier="",
        output_sha256="",
        output_size_bytes=None,
        status=ScanTailorStageState.StageStatus.INVALIDATED,
        invalidated_reason=JOB_END_REASON,
        last_error="",
        updated_at=timezone.now(),
    )
    storage = _storage()
    prefix = f"{STAGE_NAMESPACE}/{scan_job_id}/"
    listed = _list(storage, prefix)
    held = references(prefix)
    deleted = 0
    folders: set[str] = set()
    for item, _entry in listed:
        if not held.holds(item) and _delete(storage, item):
            deleted += 1
            folders.add(item.folder)
    for folder in folders:
        _remove_empty_folders(storage, folder, prefix.rstrip("/"))
    logger.info("scan_job_stage_artifacts_removed", scan_job_id=scan_job_id, files=deleted)
    return deleted


def schedule_scan_job_stage_cleanup(scan_job_id: str) -> None:
    """Run :func:`prune_scan_job_stage_artifacts` in a worker once the caller commits."""
    from ebook_server.jobs import get_queue

    def _enqueue() -> None:
        queue = get_queue()
        try:
            if queue is None:
                prune_scan_job_stage_artifacts(scan_job_id)
            else:
                queue.enqueue(prune_scan_job_stage_artifacts, scan_job_id)
        except Exception:
            logger.exception("scan_job_stage_cleanup_failed", scan_job_id=scan_job_id)

    transaction.on_commit(_enqueue)


# --- 5. free-space guard -------------------------------------------------------------


def min_free_bytes() -> int:
    """``BOOK_SCANNER_SCANTAILOR_MIN_FREE_BYTES``; 0 disables the guard."""
    try:
        value = int(getattr(django_settings, "BOOK_SCANNER_SCANTAILOR_MIN_FREE_BYTES", 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("bytes", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "bytes" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} bytes"


def low_space_message() -> str:
    """A readable refusal when the scan storage volume is below the threshold, else ``""``."""
    threshold = min_free_bytes()
    if threshold <= 0:
        return ""
    available = _storage().free_bytes()
    if available is None or available >= threshold:
        return ""
    logger.warning(
        "scantailor_stage_refused_low_space", free_bytes=available, min_free_bytes=threshold
    )
    return (
        f"Disk almost full: only {_human(available)} free on the scan storage "
        f"(at least {_human(threshold)} needed). The change was not saved. "
        "Ask an admin to free space (manage.py prune_scan_artifacts) and try again."
    )
