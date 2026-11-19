"""Rescans over the scanner device API (#131, libreflip contract §6.11, D7).

A member flags a page in the portal (a ``ScanTodo`` of type RESCAN plus a
``rescan_flag`` line in ``scan.jsonl``); the scanner lists the jobs with open rescans,
fetches what it needs to prompt the operator (page numbers, crop, thumbnails), and
uploads each replacement against its todo. A replacement is atomic on serif's side:

1. a new active ``ScanPageVersion`` for the page (the previous one is kept, inactive);
2. the todo becomes ``completed``;
3. a ``rescan_replace`` line is appended to ``scan.jsonl`` (origin ``serif``);
4. downstream ScanTailor stages and scan-derived library artifacts are invalidated
   (the existing logic of the web replacement);
5. a ``complete`` job goes back to ``captured`` with "re-finish needed" (§5).

The image itself goes through S4's streaming upload and verified publication
(:mod:`.scanner_uploads`): it is never held in memory, and a failure before the commit
leaves no version row and no file.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, QuerySet

from . import (
    scan_events,
    scan_lifecycle,
    scan_pipeline,
    scanner_opf,
    scanner_uploads,
    scantailor,
)
from .models import Book, ScanJob, ScanPage, ScanPageVersion, ScanTodo, ScanUploadSource
from .scan_thumbnails import render_jpeg_thumbnail as render_jpeg_thumbnail
from .storage_registry import backend_for_location

logger = structlog.get_logger(__name__)

OPEN_TODO_STATES: Final = (ScanTodo.State.OPEN, ScanTodo.State.CLAIMED)
RESCAN_FLAG_EVENT: Final = "rescan_flag"
RESCAN_REPLACE_EVENT: Final = "rescan_replace"

# §6.11: ``max`` defaults to 800 and is at most 1600. Requested edges are rounded down
# to a multiple of 100 so a page has at most 16 cached sizes.
THUMBNAIL_DEFAULT_EDGE: Final = 800
THUMBNAIL_MAX_EDGE: Final = 1600
THUMBNAIL_MIN_EDGE: Final = 100
THUMBNAIL_EDGE_STEP: Final = 100
THUMBNAIL_CACHE_DIRNAME: Final = "thumbnails"


# --- which jobs are the scanner's ---------------------------------------------------------


def scanner_jobs() -> QuerySet[ScanJob]:
    """Jobs that came from the scanner, as opposed to web-created ones.

    There is a single scanner (no device registry): a scanner job is one with
    ``source = scanner``, the same rule as ``scanner_api.scanner_job``.
    """
    return ScanJob.objects.filter(source=ScanJob.Source.SCANNER)


def is_scanner_job(job: ScanJob) -> bool:
    return job.source == ScanJob.Source.SCANNER


def open_rescan_todos(job: ScanJob) -> QuerySet[ScanTodo]:
    return job.todos.filter(todo_type=ScanTodo.TodoType.RESCAN, state__in=OPEN_TODO_STATES)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


# --- bibliographic summary: current OPF, else linked Book, else null ----------------------


def _opf_fields(job: ScanJob) -> dict[str, Any]:
    """Fields of the job's effective scanner OPF (S6, #130), or ``{}``.

    An OPF that carries the application's own "generated" marker is not metadata and
    is skipped (:func:`.scanner_opf.effective_opf`).
    """
    opf = scanner_opf.effective_opf(job)
    fields = opf.fields if opf is not None else None
    return fields if isinstance(fields, dict) else {}


def _book_isbn(book: Book) -> str | None:
    values = {identifier.kind: identifier.value for identifier in book.identifiers.all()}
    return values.get("isbn13") or values.get("isbn10")


def bibliographic_summary(job: ScanJob) -> dict[str, Any]:
    """``title``, ``authors`` and ``isbn`` for the rescan list (§6.11).

    Each comes from the current OPF, or the linked Book, or is ``null``.
    """
    opf = _opf_fields(job)
    book = job.book
    title = opf.get("title") or (book.title if book is not None else None) or None
    authors = [str(name) for name in opf.get("authors") or [] if name]
    if not authors and book is not None:
        authors = [author.full_name for author in book.authors.all()]
    isbn = opf.get("isbn") or (_book_isbn(book) if book is not None else None)
    return {"title": title, "authors": authors or None, "isbn": isbn or None}


# --- GET rescans / GET jobs/{id}/rescans --------------------------------------------------


def rescan_list() -> list[dict[str, Any]]:
    """Scanner jobs in any state except ``cancelled`` with at least one open rescan."""
    jobs = (
        scanner_jobs()
        .exclude(state=ScanJob.State.CANCELLED)
        .annotate(
            open_rescans=Count(
                "todos",
                filter=Q(
                    todos__todo_type=ScanTodo.TodoType.RESCAN,
                    todos__state__in=OPEN_TODO_STATES,
                ),
            )
        )
        .filter(open_rescans__gt=0)
        .select_related("book")
        .prefetch_related("book__authors", "book__identifiers")
        .order_by("scan_started_at", "created_at", "pk")
    )
    return [
        {
            "job_id": job.pk,
            "state": job.state,
            **bibliographic_summary(job),
            "open_rescans": job.open_rescans,
            "scan_started_at": _iso(job.scan_started_at),
        }
        for job in jobs
    ]


def thumbnail_url(job_id: str, sequence_number: int) -> str:
    return f"/api/scanner/v1/jobs/{job_id}/pages/{sequence_number}/thumbnail"


def job_rescans_payload(job: ScanJob) -> dict[str, Any]:
    """What the scanner's prompt loop needs (§6.11, sans#56)."""
    pages = [
        {"sequence_number": number, "recognized_page_number": recognized}
        for number, recognized in job.pages.filter(side=ScanPage.Side.SINGLE)
        .order_by("logical_sequence_number")
        .values_list("logical_sequence_number", "recognized_page_number")
    ]
    todos = [
        {
            "todo_id": todo.pk,
            "sequence_number": todo.scan_page.logical_sequence_number,
            "reason": todo.reason,
            "created_at": _iso(todo.created_at),
            "thumbnail_url": thumbnail_url(job.pk, todo.scan_page.logical_sequence_number),
        }
        for todo in open_rescan_todos(job)
        .select_related("scan_page")
        .order_by("scan_page__logical_sequence_number", "created_at", "pk")
    ]
    return {
        "job_id": job.pk,
        "state": job.state,
        "page_width_mm": job.page_width_mm,
        "page_number_crop": job.page_number_crop,
        "pages": pages,
        "todos": todos,
    }


# --- thumbnails ---------------------------------------------------------------------------


class ThumbnailError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


def thumbnail_edge(raw: str | None) -> int:
    """The longest edge for ``?max=``: default 800, at most 1600, in steps of 100."""
    if raw is None or raw == "":
        return THUMBNAIL_DEFAULT_EDGE
    if not raw.isascii() or not raw.isdigit() or len(raw) > 6:
        raise ThumbnailError(400, "invalid_request", "max must be a positive integer.")
    value = min(int(raw), THUMBNAIL_MAX_EDGE)
    if value < THUMBNAIL_MIN_EDGE:
        raise ThumbnailError(400, "invalid_request", f"max must be at least {THUMBNAIL_MIN_EDGE}.")
    return value - value % THUMBNAIL_EDGE_STEP


def active_page_version(job: ScanJob, sequence_number: int) -> ScanPageVersion | None:
    return (
        ScanPageVersion.objects.filter(
            scan_page__scan_job=job,
            scan_page__logical_sequence_number=sequence_number,
            scan_page__side=ScanPage.Side.SINGLE,
            is_active=True,
        )
        .select_related("storage_location")
        .first()
    )


def thumbnail_etag(version: ScanPageVersion, edge: int) -> str:
    return f'"v{version.pk}-{version.checksum_sha256[:32]}-{edge}"'


def thumbnail_path(version: ScanPageVersion, edge: int) -> Path:
    """The cached JPEG for ``version`` at ``edge``, rendered once (bounded) if missing."""
    cache_path = (
        Path(settings.APP_DATA_DIR)
        / scanner_uploads.SCANNING_STORAGE_ROOT
        / THUMBNAIL_CACHE_DIRNAME
        / f"scanner-v{version.pk}-{version.checksum_sha256[:32]}-{edge}.jpg"
    )
    if cache_path.exists():
        return cache_path
    try:
        storage = backend_for_location(version.storage_location)
        render_jpeg_thumbnail(storage, version.storage_identifier, cache_path, edge)
    except FileNotFoundError as exc:
        raise ThumbnailError(404, "not_found", "The page image is missing.") from exc
    except Exception as exc:  # Pillow raises many types for undecodable data.
        logger.warning(
            "scanner_thumbnail_failed",
            scan_job_id=str(version.scan_page.scan_job_id),
            version_id=version.pk,
            error=type(exc).__name__,
        )
        raise ThumbnailError(500, "thumbnail_failed", "The thumbnail could not be made.") from exc
    return cache_path


# --- portal flagging ----------------------------------------------------------------------


@dataclass(frozen=True)
class FlagResult:
    todo: ScanTodo
    created: bool


def flag_page(page: ScanPage, *, reason: str, user: Any) -> FlagResult:
    """Create (or return the outstanding) rescan todo for ``page``; ``rescan_flag`` for
    scanner jobs.

    An ``open`` or ``claimed`` todo for the page is outstanding (the scanner lists both,
    :data:`OPEN_TODO_STATES`): flagging again returns it unchanged, assignee included,
    and appends no event. Once it is ``completed`` or ``cancelled`` a new flag creates a
    new todo. Allowed in :data:`scan_lifecycle.RESCAN_STATES` (``captured`` and
    ``complete`` included, D7); the lookup and the insert run under the job's row lock,
    so concurrent flags cannot both create one. The todo and the event commit together.
    Raises :class:`scan_lifecycle.LifecycleError` for another state.
    """
    with transaction.atomic():
        job = ScanJob.objects.select_for_update().get(pk=page.scan_job_id)
        if job.state not in scan_lifecycle.RESCAN_STATES:
            raise scan_lifecycle.LifecycleError(
                job.state, "scan job is not accepting rescan requests"
            )
        existing = (
            page.todos.filter(todo_type=ScanTodo.TodoType.RESCAN, state__in=OPEN_TODO_STATES)
            .order_by("-created_at")
            .first()
        )
        if existing is not None:
            return FlagResult(existing, False)
        todo = ScanTodo.objects.create(
            scan_job=job,
            scan_page=page,
            todo_type=ScanTodo.TodoType.RESCAN,
            reason=reason,
            creator=user,
        )
        if is_scanner_job(job):
            scan_events.append_serif_event(
                job,
                RESCAN_FLAG_EVENT,
                {"sequence_number": page.logical_sequence_number, "reason": reason},
                todo_id=todo.pk,
            )
        return FlagResult(todo, True)


# --- replacement upload -------------------------------------------------------------------


@dataclass(frozen=True)
class ReplacementMetadata:
    sha256: str
    camera_role: str
    captured_at: datetime | None
    recognized_page_number: str | None
    page_number_verified: bool | None


def replacement_metadata(metadata: dict[str, Any], sequence_number: int) -> ReplacementMetadata:
    """Validate the ``metadata`` part (§6.11): the page rules of §6.5 plus the check flag."""
    page = scanner_uploads.page_metadata(metadata, sequence_number)
    verified = metadata.get("page_number_verified")
    if verified is not None and not isinstance(verified, bool):
        raise scanner_uploads.UploadError(
            400,
            "invalid_request",
            "page_number_verified must be true, false or null.",
            {"field": "page_number_verified"},
        )
    return ReplacementMetadata(
        sha256=page.sha256,
        camera_role=page.camera_role,
        captured_at=page.captured_at,
        recognized_page_number=page.recognized_page_number,
        page_number_verified=verified,
    )


def todo_state_error(todo: ScanTodo, message: str) -> scanner_uploads.UploadError:
    return scanner_uploads.UploadError(
        409, "todo_state", message, {"todo_id": todo.pk, "state": todo.state}
    )


def job_state_error(job: ScanJob) -> scanner_uploads.UploadError:
    return scanner_uploads.UploadError(
        409,
        "job_state",
        f"Rescans are not accepted for a {job.state} job.",
        {"state": job.state},
    )


@dataclass(frozen=True)
class ReplacementResult:
    created: bool
    todo_id: int
    todo_state: str
    sequence_number: int
    sha256: str
    version: int
    previous_version: int | None
    state: str
    page_id: str

    def payload(self) -> dict[str, Any]:
        return {
            "todo_id": self.todo_id,
            "todo_state": self.todo_state,
            "sequence_number": self.sequence_number,
            "sha256": self.sha256,
            "version": self.version,
            "previous_version": self.previous_version,
            "state": self.state,
        }


def _fulfilling_version(todo: ScanTodo, sha256: str) -> ScanPageVersion | None:
    """The version this todo was fulfilled with, if it has the checksum ``sha256``."""
    return (
        ScanPageVersion.objects.filter(
            scan_page_id=todo.scan_page_id,
            checksum_sha256=sha256,
            scanner_metadata__todo_id=todo.pk,
        )
        .order_by("-version")
        .first()
    )


def _repeat_result(todo: ScanTodo, job: ScanJob, sha256: str) -> ReplacementResult | None:
    """``200``: a completed todo already fulfilled by this checksum; else ``None``.

    Raises ``409 todo_state`` for a cancelled todo, or one completed by another image.
    """
    if todo.state == ScanTodo.State.CANCELLED:
        raise todo_state_error(todo, "The rescan was cancelled; skip this page.")
    if todo.state != ScanTodo.State.COMPLETED:
        return None
    version = _fulfilling_version(todo, sha256)
    if version is None:
        raise todo_state_error(todo, "The rescan was already completed with another image.")
    previous = version.scanner_metadata.get("replaces_version")
    return ReplacementResult(
        created=False,
        todo_id=todo.pk,
        todo_state=todo.state,
        sequence_number=todo.scan_page.logical_sequence_number,
        sha256=version.checksum_sha256,
        version=version.version,
        previous_version=previous if isinstance(previous, int) else None,
        state=job.state,
        page_id=str(todo.scan_page_id),
    )


def precheck_replacement(job: ScanJob, todo: ScanTodo) -> None:
    """Refuse before the body is read: cancelled todo, or a job that takes no rescans."""
    if todo.state == ScanTodo.State.CANCELLED:
        raise todo_state_error(todo, "The rescan was cancelled; skip this page.")
    if todo.state != ScanTodo.State.COMPLETED and job.state not in scan_lifecycle.RESCAN_STATES:
        raise job_state_error(job)


def store_replacement(
    *,
    job: ScanJob,
    todo: ScanTodo,
    upload: scanner_uploads.ReceivedUpload,
    metadata: ReplacementMetadata,
    mime_type: str,
    extension: str,
) -> ReplacementResult:
    """Store a verified, staged replacement image for ``todo`` in one transaction."""
    repeat = _repeat_result(todo, job, metadata.sha256)
    if repeat is not None:
        return repeat
    page = todo.scan_page
    number = page.logical_sequence_number
    storage = scanner_uploads.scan_storage()
    destination: str | None = scanner_uploads.publish_staged(
        upload,
        storage,
        f"{scanner_uploads.SCANNING_NAMESPACE}/{job.pk}/{number:04d}-{page.side}",
        f"{number:04d}-rescan{todo.pk}-{metadata.sha256[:16]}{extension}",
    )
    try:
        with transaction.atomic():
            locked = ScanJob.objects.select_for_update().get(pk=job.pk)
            locked_todo = (
                ScanTodo.objects.select_for_update().select_related("scan_page").get(pk=todo.pk)
            )
            repeat = _repeat_result(locked_todo, locked, metadata.sha256)
            if repeat is not None:
                return repeat
            if locked.state not in scan_lifecycle.RESCAN_STATES:
                raise job_state_error(locked)
            page = ScanPage.objects.select_for_update().get(pk=locked_todo.scan_page_id)
            previous = page.versions.filter(is_active=True).first()
            latest = page.versions.order_by("-version").first()
            page.versions.filter(is_active=True).update(is_active=False)
            version = ScanPageVersion.objects.create(
                scan_page=page,
                storage_location=scanner_uploads.scan_storage_location(),
                storage_identifier=destination,
                checksum_sha256=metadata.sha256,
                mime_type=mime_type,
                size_bytes=upload.image_size,
                capture_timestamp=metadata.captured_at,
                scanner_metadata={
                    "source": "scanner_api_v1",
                    "rescan": True,
                    "todo_id": locked_todo.pk,
                    "replaces_version": previous.version if previous is not None else None,
                    "camera_role": metadata.camera_role,
                    "recognized_page_number": metadata.recognized_page_number,
                    "page_number_verified": metadata.page_number_verified,
                },
                version=(latest.version + 1) if latest is not None else 1,
                uploader=None,
                source=ScanUploadSource.SCANNER,
                original_filename=f"{number:04d}{extension}",
                page_number_verified=metadata.page_number_verified,
                is_active=True,
            )
            page.recognized_page_number = metadata.recognized_page_number
            page.save(update_fields=["recognized_page_number", "updated_at"])
            locked_todo.complete()
            scan_events.append_serif_event(
                locked,
                RESCAN_REPLACE_EVENT,
                {
                    "sequence_number": number,
                    "sha256": metadata.sha256,
                    "version": version.version,
                    "recognized_page_number": metadata.recognized_page_number,
                    "page_number_verified": metadata.page_number_verified,
                },
                todo_id=locked_todo.pk,
            )
            # The same downstream handling as the web replacement (views._store_scan_page).
            scan_lifecycle.reopen_for_refinish(locked)
            scantailor.mark_state_stale(
                scan_job_id=str(locked.pk), page_id=str(page.pk), reason="source changed"
            )
            if not locked.refinish_needed:
                scan_pipeline.invalidate_scan_job_library_artifacts(locked)
            logger.info(
                "scanner_rescan_replaced",
                scan_job_id=str(locked.pk),
                todo_id=locked_todo.pk,
                sequence_number=number,
                version=version.version,
                job_state=locked.state,
            )
            result = ReplacementResult(
                created=True,
                todo_id=locked_todo.pk,
                todo_state=locked_todo.state,
                sequence_number=number,
                sha256=metadata.sha256,
                version=version.version,
                previous_version=previous.version if previous is not None else None,
                state=locked.state,
                page_id=str(page.pk),
            )
        destination = None  # committed: the version row now owns the published file
        return result
    finally:
        if destination is not None:
            try:
                storage.delete(destination)
            except OSError:
                logger.warning("scanner_upload_cleanup_failed", storage_identifier=destination)
