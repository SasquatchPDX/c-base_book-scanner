"""``scan.jsonl`` event lines of a scan job (#129, contract §4.4 and §6.7).

The libreflip scanner (``sans``) keeps an append-only JSON Lines process log per book
(libreflip/sans#48) and delivers it line by line over ``POST jobs/{id}/events``. serif
stores every line raw (:class:`ScanEvent`), projects the event types it knows into its
own tables, adds its own rescan events (origin ``serif``), and renders the combined
``scan.jsonl`` into the job's folder and, once the job is finished, into the book's
folder.

Ordering and the rendered file
    Every insert for a job happens while holding that job's row lock, so a job's events
    ordered by primary key are in arrival order. sans lines are only ever stored in
    ``line_no`` order (a gap is refused), so in arrival order they appear as
    ``1, 2, 3...``; serif's lines sit wherever they arrived. The rendered file is every
    stored line in that order, each followed by ``\\n``: the sans lines in ``line_no``
    order with the serif events interleaved at their arrival positions. Nothing is
    reformatted: a sans line is written byte-for-byte as received.

Idempotency
    For origin ``sans``, ``(job, line_no)`` is unique. A batch overlapping lines that
    are already stored is accepted only when the overlap is byte-identical; those lines
    are confirmed and skipped. A batch starting beyond the high-water mark + 1 is a gap.

Rendering
    The file is derived state; the database is authoritative. It is written after the
    transaction commits, by a background job (inline when no queue is configured),
    atomically (temporary file, then rename) through the storage backend. Renders are
    idempotent snapshots: a render that finds nothing new skips the write, and a render
    that loses a race with a newer one notices and renders again.
"""

from __future__ import annotations

import json
import math
import threading
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, TypeGuard

import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from ebook_server.jobs import enqueue
from ebook_server.storage import LocalFilesystemStorage

from . import archive_layout, intake
from .models import Book, ScanEvent, ScanJob, ScanPage

logger = structlog.get_logger(__name__)

SCAN_JSONL_NAME: Final = "scan.jsonl"
# Per-job folder in scan storage; the same namespace raw page uploads use
# (``views.SCANNING_NAMESPACE``).
JOB_NAMESPACE: Final = "scan-jobs"
# §6.12: one line, and one batch (sum of the lines' UTF-8 bytes and number of lines).
MAX_EVENT_LINE_BYTES: Final = 64 * 1024
MAX_EVENT_BATCH_BYTES: Final = 1024 * 1024
MAX_EVENT_BATCH_LINES: Final = 1000
MAX_EVENT_TYPE_LENGTH: Final = 64
# A render that keeps losing races to newer events gives up after this many passes;
# the next stored event schedules another render.
MAX_RENDER_PASSES: Final = 5

JOB_EVENT = "job"
PAGE_EVENT = "page"
COMPLETENESS_EVENT = "completeness_check"
SERIF_EVENT_TYPES: Final = frozenset({"rescan_flag", "rescan_replace"})


# --- errors -----------------------------------------------------------------------------


class ScanEventError(Exception):
    """Base for refusals of an event batch; carries the API error code and details."""

    code = "invalid_request"
    status = 400

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = dict(details or {})


class InvalidEvent(ScanEventError):
    """A line is not one JSON object with a string ``type``, or is too long (422)."""

    code = "invalid_event"
    status = 422


class EventGap(ScanEventError):
    """``first_line_no`` is beyond the high-water mark + 1 (409)."""

    code = "event_gap"
    status = 409


class EventConflict(ScanEventError):
    """An already-stored line differs from the one sent again (409)."""

    code = "event_conflict"
    status = 409


class EventJobState(ScanEventError):
    """The job is cancelled; events are accepted in every other state (409)."""

    code = "job_state"
    status = 409


# --- validation -------------------------------------------------------------------------


def parse_event_line(raw: str, *, line_no: int | None = None) -> dict[str, Any]:
    """Return the event object of one raw line, or raise :class:`InvalidEvent`.

    A line is one JSON object with a string ``type``, at most 64 KiB of UTF-8, and has
    no line break of its own (it becomes exactly one line of ``scan.jsonl``).
    """
    details: dict[str, Any] = {} if line_no is None else {"line_no": line_no}
    if not isinstance(raw, str):
        raise InvalidEvent("A line must be a string.", details)
    if "\n" in raw or "\r" in raw:
        raise InvalidEvent("A line must not contain a line break.", details)
    try:
        encoded = raw.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise InvalidEvent("A line must be valid UTF-8.", details) from exc
    if len(encoded) > MAX_EVENT_LINE_BYTES:
        raise InvalidEvent(
            f"A line must not exceed {MAX_EVENT_LINE_BYTES} bytes.",
            {**details, "max_line_bytes": MAX_EVENT_LINE_BYTES},
        )
    try:
        event = json.loads(raw)
    except ValueError as exc:
        raise InvalidEvent("A line must be one JSON object.", details) from exc
    if not isinstance(event, dict):
        raise InvalidEvent("A line must be one JSON object.", details)
    event_type = event.get("type")
    if not isinstance(event_type, str) or not event_type:
        raise InvalidEvent("A line needs a non-empty string 'type'.", details)
    return event


def _type_column(event_type: str) -> str:
    # The raw line keeps the full value; the indexed column only needs a prefix.
    return event_type[:MAX_EVENT_TYPE_LENGTH]


# --- high-water mark --------------------------------------------------------------------


def sans_high_water_mark(job: ScanJob | str) -> int:
    """Highest stored ``line_no`` of the scanner's lines for ``job``; 0 when none.

    Lines ``1..N`` are always all stored (a gap is refused), so this is also the number
    of scanner lines serif has. ``capture-complete`` (§6.9) compares it with the
    manifest's ``sans_last_line_no``; ``GET jobs/{id}`` reports it (§6.4).
    """
    job_id = job.pk if isinstance(job, ScanJob) else job
    value = ScanEvent.objects.filter(scan_job_id=job_id, origin=ScanEvent.Origin.SANS).aggregate(
        top=Max("line_no")
    )["top"]
    return int(value or 0)


# --- appending --------------------------------------------------------------------------


@dataclass(frozen=True)
class AppendResult:
    sans_last_line_no: int
    stored: int
    confirmed: int


def _lock_job(job_id: str) -> ScanJob:
    return ScanJob.objects.select_for_update().get(pk=job_id)


def append_sans_lines(job_id: str, first_line_no: int, lines: Sequence[str]) -> AppendResult:
    """Store a batch of scanner lines numbered from ``first_line_no`` (§6.7).

    Lines at or below the high-water mark must equal the stored ones byte for byte and
    are skipped; the rest are appended, projected, and a render of ``scan.jsonl`` is
    scheduled for after commit. Raises :class:`InvalidEvent`, :class:`EventGap`,
    :class:`EventConflict`, :class:`EventJobState` or ``ScanJob.DoesNotExist``.

    Concurrent overlapping batches are serialised by the job's row lock; the unique
    ``(job, line_no)`` constraint is the backstop, and a batch that still collides is
    re-evaluated once against what the other one stored.
    """
    if first_line_no < 1:
        raise ScanEventError("first_line_no must be at least 1.")
    events = [
        parse_event_line(raw, line_no=first_line_no + index) for index, raw in enumerate(lines)
    ]
    for attempt in range(2):
        try:
            with transaction.atomic():
                return _append_locked(job_id, first_line_no, lines, events)
        except IntegrityError:
            if attempt:
                raise
            logger.warning("scan_events_insert_race", scan_job_id=job_id)
    raise AssertionError("unreachable")


def _append_locked(
    job_id: str,
    first_line_no: int,
    lines: Sequence[str],
    events: Sequence[dict[str, Any]],
) -> AppendResult:
    job = _lock_job(job_id)
    if job.state == ScanJob.State.CANCELLED:
        raise EventJobState("The job is cancelled.", {"state": job.state})
    high = sans_high_water_mark(job)
    if first_line_no > high + 1:
        raise EventGap(
            f"Expected the batch to start at line {high + 1}.",
            {"expected_first_line_no": high + 1, "sans_last_line_no": high},
        )
    last_line_no = first_line_no + len(lines) - 1
    overlap_end = min(last_line_no, high)
    if overlap_end >= first_line_no:
        stored = dict(
            ScanEvent.objects.filter(
                scan_job=job,
                origin=ScanEvent.Origin.SANS,
                line_no__gte=first_line_no,
                line_no__lte=overlap_end,
            ).values_list("line_no", "raw")
        )
        for line_no in range(first_line_no, overlap_end + 1):
            if stored.get(line_no) != lines[line_no - first_line_no]:
                raise EventConflict(
                    f"Line {line_no} differs from the stored line.", {"line_no": line_no}
                )
    start = max(0, high + 1 - first_line_no)
    new_rows = [
        ScanEvent(
            scan_job=job,
            origin=ScanEvent.Origin.SANS,
            line_no=first_line_no + index,
            raw=lines[index],
            type=_type_column(str(events[index]["type"])),
        )
        for index in range(start, len(lines))
    ]
    if new_rows:
        ScanEvent.objects.bulk_create(new_rows)
        _project(job, [events[index] for index in range(start, len(lines))])
        schedule_scan_jsonl_render([job.pk])
        logger.info(
            "scan_events_appended",
            scan_job_id=job.pk,
            stored=len(new_rows),
            confirmed=len(lines) - len(new_rows),
            sans_last_line_no=max(high, last_line_no),
        )
    return AppendResult(
        sans_last_line_no=max(high, last_line_no),
        stored=len(new_rows),
        confirmed=len(lines) - len(new_rows),
    )


def append_serif_event(
    job: ScanJob | str,
    event_type: str,
    fields: Mapping[str, Any] | None = None,
    *,
    todo_id: int | None = None,
    at: datetime | None = None,
) -> ScanEvent:
    """Append one of serif's own lines (``rescan_flag``/``rescan_replace``, §4.4).

    The line carries the sans#48 fields given in ``fields`` plus ``"origin": "serif"``,
    ``"at"`` (ISO 8601 UTC) and ``"todo_id"``. It is appended in arrival order after
    whatever the job already has; call it inside the transaction that makes the change
    it records, so the event and the change commit together. Allowed in any state.
    """
    job_id = job.pk if isinstance(job, ScanJob) else job
    stamp = (at or timezone.now()).astimezone(UTC).replace(microsecond=0)
    payload: dict[str, Any] = {"type": event_type, **dict(fields or {})}
    payload.update(
        {
            "origin": ScanEvent.Origin.SERIF.value,
            "at": stamp.isoformat().replace("+00:00", "Z"),
            "todo_id": todo_id,
        }
    )
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    parse_event_line(raw)
    with transaction.atomic():
        locked = _lock_job(job_id)
        event = ScanEvent.objects.create(
            scan_job=locked,
            origin=ScanEvent.Origin.SERIF,
            line_no=None,
            raw=raw,
            type=_type_column(event_type),
        )
        _project(locked, [payload])
        schedule_scan_jsonl_render([locked.pk])
    return event


# --- projection -------------------------------------------------------------------------


def _is_number(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value)


def _page_changes(event: Mapping[str, Any]) -> dict[str, Any]:
    changes: dict[str, Any] = {}
    if "recognized_page_number" in event:
        number = event["recognized_page_number"]
        if number is None or isinstance(number, str):
            changes["recognized_page_number"] = None if number is None else number[:64]
    status = event.get("status")
    if isinstance(status, str):
        changes["status"] = status[:32]
    return changes


def _page_sequence_number(event: Mapping[str, Any]) -> int | None:
    value = event.get("sequence_number")
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return None


def _project(job: ScanJob, events: Iterable[Mapping[str, Any]]) -> None:
    """Apply the known event types to ``job`` (locked by the caller); later wins.

    ``job`` → calibration on the job; ``page`` → the page's recognised number and
    status (when the page exists; see :func:`project_page_from_events`);
    ``completeness_check`` → the job's completeness flags. Malformed fields of a known
    type are ignored (the line itself is stored either way); other types are stored only.
    """
    job_updates: dict[str, Any] = {}
    page_updates: dict[int, dict[str, Any]] = {}
    for event in events:
        event_type = event.get("type")
        if event_type == JOB_EVENT:
            width = event.get("page_width_mm")
            if _is_number(width) and width > 0:
                job_updates["page_width_mm"] = float(width)
            crop = event.get("page_number_crop")
            if isinstance(crop, dict):
                job_updates["page_number_crop"] = crop
        elif event_type == PAGE_EVENT:
            sequence_number = _page_sequence_number(event)
            changes = _page_changes(event)
            if sequence_number is not None and changes:
                page_updates.setdefault(sequence_number, {}).update(changes)
        elif event_type == COMPLETENESS_EVENT:
            flags = event.get("flags")
            if isinstance(flags, list):
                job_updates["completeness_flags"] = flags
                job_updates["completeness_checked_at"] = timezone.now()
    now = timezone.now()
    if job_updates:
        ScanJob.objects.filter(pk=job.pk).update(**job_updates, updated_at=now)
        for name, value in job_updates.items():
            setattr(job, name, value)
    for sequence_number, changes in page_updates.items():
        ScanPage.objects.filter(
            scan_job=job,
            logical_sequence_number=sequence_number,
            side=ScanPage.Side.SINGLE,
        ).update(**changes, updated_at=now)


def project_page_from_events(page: ScanPage) -> bool:
    """Apply the latest stored ``page`` event for ``page`` to it; True if one applied.

    A ``page`` event can arrive before the page's image. The device page upload (#128)
    calls this after creating a page so the event's values are not lost.
    """
    if page.side != ScanPage.Side.SINGLE:
        return False
    candidates = (
        ScanEvent.objects.filter(scan_job_id=page.scan_job_id, type=PAGE_EVENT)
        .order_by("-id")
        .values_list("raw", flat=True)
    )
    for raw in candidates.iterator():
        try:
            event = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if _page_sequence_number(event) != page.logical_sequence_number:
            continue
        changes = _page_changes(event)
        if not changes:
            continue
        ScanPage.objects.filter(pk=page.pk).update(**changes, updated_at=timezone.now())
        for name, value in changes.items():
            setattr(page, name, value)
        return True
    return False


# --- rendering --------------------------------------------------------------------------


def render_scan_jsonl(job: ScanJob | str) -> bytes:
    """The combined ``scan.jsonl`` of ``job``: every stored line in arrival order."""
    job_id = job.pk if isinstance(job, ScanJob) else job
    return b"".join(
        raw.encode("utf-8") + b"\n"
        for raw in ScanEvent.objects.filter(scan_job_id=job_id)
        .order_by("id")
        .values_list("raw", flat=True)
        .iterator()
    )


def _scan_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(str(Path(settings.APP_DATA_DIR) / "scan-artifacts"))


def job_scan_jsonl_identifier(job_id: str) -> str:
    """Storage id of the rendered file in the job's folder of scan storage."""
    return _scan_storage().generate_destination(f"{JOB_NAMESPACE}/{job_id}", SCAN_JSONL_NAME)


def _book_scan_jsonl_identifier(job: ScanJob, book: Book, storage: LocalFilesystemStorage) -> str:
    """Claim (once) the name of this job's ``scan.jsonl`` in the book's folder.

    The first scan job of a book gets ``scan.jsonl``; another scan job of the same book
    (or a name already taken by a file serif did not render) gets
    ``scan-<job id>.jsonl``, so two jobs never overwrite each other's log. The claim is
    stored on the job and kept, like the book folder itself.
    """
    if job.scan_jsonl_book_identifier:
        return job.scan_jsonl_book_identifier
    with transaction.atomic():
        Book.objects.select_for_update().filter(pk=book.pk).first()
        current = (
            ScanJob.objects.filter(pk=job.pk).values_list("scan_jsonl_book_identifier", flat=True)
        ).first()
        if current:
            job.scan_jsonl_book_identifier = current
            return current
        if archive_layout.configured_layout().job_scoped:
            folder = f"books/{book.pk}"
        else:
            folder = archive_layout.book_folder(book, storage)
        preferred = f"{folder}/{SCAN_JSONL_NAME}"
        taken = (
            ScanJob.objects.filter(scan_jsonl_book_identifier=preferred).exclude(pk=job.pk).exists()
        )
        identifier = preferred
        if taken or storage.exists(preferred):
            identifier = f"{folder}/scan-{job.pk}.jsonl"
        ScanJob.objects.filter(pk=job.pk).update(scan_jsonl_book_identifier=identifier)
        job.scan_jsonl_book_identifier = identifier
        return identifier


def _mark_rendered(job_id: str, field_name: str, through: int) -> None:
    ScanJob.objects.filter(
        Q(**{f"{field_name}__isnull": True}) | Q(**{f"{field_name}__lt": through}),
        pk=job_id,
    ).update(**{field_name: through})


def write_scan_jsonl(job_id: str) -> None:
    """Render ``scan.jsonl`` into the job folder and, once finished, the book folder.

    Background job. Each pass renders a snapshot of the stored lines and replaces the
    file atomically. If newer lines were stored meanwhile (or a concurrent render may
    have replaced the file with an older snapshot after this one), it renders again.
    """
    force = False
    for _pass in range(MAX_RENDER_PASSES):
        job = ScanJob.objects.select_related("book").filter(pk=job_id).first()
        if job is None:
            return
        rows = list(job.events.order_by("id").values_list("id", "raw"))
        if not rows:
            return
        through = rows[-1][0]
        data = b"".join(raw.encode("utf-8") + b"\n" for _id, raw in rows)
        if force or job.scan_jsonl_job_rendered_through != through:
            _scan_storage().replace_bytes(job_scan_jsonl_identifier(job.pk), data)
            _mark_rendered(job.pk, "scan_jsonl_job_rendered_through", through)
        book = job.book
        if job.state == ScanJob.State.COMPLETE and book is not None:
            if force or job.scan_jsonl_book_rendered_through != through:
                storage = intake._default_storage()
                identifier = _book_scan_jsonl_identifier(job, book, storage)
                storage.replace_bytes(identifier, data)
                _mark_rendered(job.pk, "scan_jsonl_book_rendered_through", through)
        latest = job.events.aggregate(top=Max("id"))["top"]
        if latest == through:
            logger.info("scan_jsonl_rendered", scan_job_id=job.pk, lines=len(rows))
            return
        force = True
    logger.warning("scan_jsonl_render_gave_up", scan_job_id=job_id, passes=MAX_RENDER_PASSES)


class _PendingRenders(threading.local):
    def __init__(self) -> None:
        self.job_ids: set[str] = set()


_pending = _PendingRenders()


def schedule_scan_jsonl_render(job_ids: Iterable[str]) -> None:
    """Render these jobs' ``scan.jsonl`` once the current transaction commits.

    Calls within one transaction coalesce into one render per job. The render is a
    background job (``ebook_server.jobs.enqueue``), so a slow network share never
    delays the request that stored the events.
    """
    ids = {str(job_id) for job_id in job_ids if job_id}
    if not ids:
        return
    _pending.job_ids.update(ids)
    transaction.on_commit(_flush_pending)


def _flush_pending() -> None:
    ids = sorted(_pending.job_ids)
    _pending.job_ids.clear()
    for job_id in ids:
        try:
            enqueue(write_scan_jsonl, job_id)
        except Exception:  # never fail the write that stored the events
            logger.exception("scan_jsonl_render_failed", scan_job_id=job_id)
