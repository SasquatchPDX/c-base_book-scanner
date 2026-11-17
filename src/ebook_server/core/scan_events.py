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
    atomically (temporary file, then rename) through the storage backend. The first
    write claims a free name with an exclusive create and records it on the job; serif
    never overwrites a file it did not claim (an uploaded log, another job's file).
    Each render pass takes a generation under the job's row lock and only renames its
    snapshot into place while that generation is still the newest, so an older pass
    never replaces a newer snapshot. A file is current when it reads back as the
    snapshot (size and SHA-256); the recorded high-water mark is only bookkeeping.
"""

from __future__ import annotations

import hashlib
import json
import math
import threading
import uuid
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog
from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Max, Q
from django.utils import timezone

from ebook_server.jobs import enqueue
from ebook_server.storage import MAX_PUBLISH_ATTEMPTS, LocalFilesystemStorage, StorageBackend

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
# ScanPage.logical_sequence_number is a PositiveIntegerField; a larger value in an event
# names no page and is ignored rather than sent to the database.
MAX_SEQUENCE_NUMBER: Final = 2_147_483_647

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
    except (ValueError, RecursionError) as exc:
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


def _finite_float(value: Any) -> float | None:
    """``value`` as a finite float, or None when it is not a JSON number that fits one.

    JSON integers are unbounded in Python; one too large for a float (``float()`` raises
    ``OverflowError``) is malformed like a string would be, never an error of the batch.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _json_safe(value: Any) -> bool:
    """Whether ``value`` can be stored in a JSON column (no NaN/Infinity, not too deep)."""
    try:
        json.dumps(value, allow_nan=False)
    except (ValueError, TypeError, RecursionError):
        return False
    return True


# The ScanPage fields a ``page`` event can set.
_PAGE_FIELDS: Final = ("recognized_page_number", "status")


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
    if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_SEQUENCE_NUMBER:
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
            width = _finite_float(event.get("page_width_mm"))
            if width is not None and width > 0:
                job_updates["page_width_mm"] = width
            crop = event.get("page_number_crop")
            if isinstance(crop, dict) and _json_safe(crop):
                job_updates["page_number_crop"] = crop
        elif event_type == PAGE_EVENT:
            sequence_number = _page_sequence_number(event)
            changes = _page_changes(event)
            if sequence_number is not None and changes:
                page_updates.setdefault(sequence_number, {}).update(changes)
        elif event_type == COMPLETENESS_EVENT:
            flags = event.get("flags")
            if isinstance(flags, list) and _json_safe(flags):
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
    """Apply the stored ``page`` events for ``page`` to it; True if any value applied.

    A ``page`` event can arrive before the page's image. The device page upload (#128)
    calls this after creating a page so the events' values are not lost. As in the live
    projection (:func:`_project`), each field takes the latest valid value any event
    gave it: a later event that carries only ``status`` keeps an earlier
    ``recognized_page_number``.
    """
    if page.side != ScanPage.Side.SINGLE:
        return False
    candidates = (
        ScanEvent.objects.filter(scan_job_id=page.scan_job_id, type=PAGE_EVENT)
        .order_by("-id")
        .values_list("raw", flat=True)
    )
    changes: dict[str, Any] = {}
    for raw in candidates.iterator():
        try:
            event = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(event, dict):
            continue
        if _page_sequence_number(event) != page.logical_sequence_number:
            continue
        for name, value in _page_changes(event).items():
            changes.setdefault(name, value)  # newest first: the first value seen wins
        if len(changes) == len(_PAGE_FIELDS):
            break
    if not changes:
        return False
    ScanPage.objects.filter(pk=page.pk).update(**changes, updated_at=timezone.now())
    for name, value in changes.items():
        setattr(page, name, value)
    return True


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
    """Preferred storage id of the rendered file in the job's folder of scan storage.

    The file actually written is the one claimed in ``ScanJob.scan_jsonl_job_identifier``;
    it differs from this name only when something else already occupied it.
    """
    return _scan_storage().generate_destination(f"{JOB_NAMESPACE}/{job_id}", SCAN_JSONL_NAME)


class ScanJsonlWriteError(OSError):
    """The rendered file did not read back as the bytes just written."""


@dataclass(frozen=True)
class _Target:
    """One destination of the rendered file: the job folder or the book folder."""

    name: str
    identifier_field: str
    rendered_field: str


_JOB_TARGET: Final = _Target("job", "scan_jsonl_job_identifier", "scan_jsonl_job_rendered_through")
_BOOK_TARGET: Final = _Target(
    "book", "scan_jsonl_book_identifier", "scan_jsonl_book_rendered_through"
)


def _book_folder(book: Book, storage: StorageBackend) -> str:
    if archive_layout.configured_layout().job_scoped:
        return f"books/{book.pk}"
    return archive_layout.book_folder(book, storage)


def _candidate_names(job_id: str) -> Iterator[str]:
    """``scan.jsonl``, then ``scan-<job id>.jsonl``, then that name with a random token."""
    yield SCAN_JSONL_NAME
    yield f"scan-{job_id}.jsonl"
    for _attempt in range(MAX_PUBLISH_ATTEMPTS - 2):
        yield f"scan-{job_id}-{uuid.uuid4().hex[:8]}.jsonl"


def _holds(storage: StorageBackend, identifier: str, data: bytes) -> bool:
    """Whether ``identifier`` currently holds exactly ``data`` (size, then SHA-256)."""
    try:
        size, _mtime = storage.stat(identifier)
        if size != len(data):
            return False
        return storage.hash(identifier) == hashlib.sha256(data).hexdigest()
    except FileNotFoundError:
        return False


def _claim(storage: StorageBackend, staged: str, folder: str, job_id: str, target: _Target) -> str:
    """Publish ``staged`` under the first free name of :func:`_candidate_names`.

    Each name is taken with an exclusive create (the allocation ``publish_unique``
    uses), so a file that already exists there is never replaced, whoever wrote it: an
    independently uploaded log (#113), another tool, another job. Names another job has
    claimed are skipped even when their file has gone, so that job never writes over ours.
    """
    for name in _candidate_names(job_id):
        candidate = f"{folder}/{name}"
        field = target.identifier_field
        if ScanJob.objects.filter(**{field: candidate}).exclude(pk=job_id).exists():
            continue
        try:
            return storage.commit_staged_exclusive(staged, candidate)
        except FileExistsError:
            continue
    raise FileExistsError(f"no free name for scan.jsonl in {folder!r}")


def _begin_render(job_id: str) -> tuple[ScanJob, int, int] | None:
    """Start a render pass: ``(job, generation, through)``, or None if nothing to render.

    Under the job's row lock (which every event insert also takes) the pass reads the
    newest event id it will render through and takes the next render generation. A
    pass may only publish while its generation is still the job's newest, so an older
    pass can never replace the snapshot of a newer one.
    """
    with transaction.atomic():
        job = ScanJob.objects.select_for_update().filter(pk=job_id).first()
        if job is None:
            return None
        through = job.events.aggregate(top=Max("id"))["top"]
        if through is None:
            return None
        generation = job.scan_jsonl_render_generation + 1
        ScanJob.objects.filter(pk=job.pk).update(scan_jsonl_render_generation=generation)
        job.scan_jsonl_render_generation = generation
    return job, generation, int(through)


def _publish(
    job: ScanJob,
    generation: int,
    through: int,
    data: bytes,
    target: _Target,
    storage: StorageBackend,
    folder: str | None,
) -> bool:
    """Make ``target``'s file hold ``data``; False when a newer render superseded this one.

    A file that already holds ``data`` (checked by size and hash, not by the recorded
    mark) is left alone. Otherwise the bytes are staged beside it, then, under the job's
    row lock and only while ``generation`` is still the newest, renamed into place (or
    claimed exclusively on the first write), read back, and only then recorded.
    """
    identifier = getattr(job, target.identifier_field)
    if identifier and _holds(storage, identifier, data):
        _record(job.pk, generation, target, identifier, through)
        return True
    if not identifier and folder is None:
        raise ValueError("a first write needs the target folder")
    staged = storage.stage_bytes(identifier or f"{folder}/{SCAN_JSONL_NAME}", data)
    try:
        with transaction.atomic():
            locked = ScanJob.objects.select_for_update().filter(pk=job.pk).first()
            if locked is None or locked.scan_jsonl_render_generation != generation:
                return False
            identifier = getattr(locked, target.identifier_field)
            if identifier:
                storage.commit_staged(staged, identifier)
            else:
                assert folder is not None
                identifier = _claim(storage, staged, folder, job.pk, target)
            verified = _holds(storage, identifier, data)
            updates: dict[str, Any] = {target.identifier_field: identifier}
            if verified:
                updates[target.rendered_field] = through
            ScanJob.objects.filter(pk=job.pk).update(**updates)
            setattr(job, target.identifier_field, identifier)
    finally:
        storage.discard_staged(staged)
    if not verified:
        raise ScanJsonlWriteError(f"{identifier!r} does not hold the rendered snapshot")
    return True


def _record(job_id: str, generation: int, target: _Target, identifier: str, through: int) -> None:
    """Record a verified snapshot, unless a newer render has taken over meanwhile."""
    ScanJob.objects.filter(
        Q(**{f"{target.rendered_field}__isnull": True})
        | Q(**{f"{target.rendered_field}__lt": through}),
        pk=job_id,
        scan_jsonl_render_generation=generation,
        **{target.identifier_field: identifier},
    ).update(**{target.rendered_field: through})


def write_scan_jsonl(job_id: str) -> None:
    """Render ``scan.jsonl`` into the job folder and, once finished, the book folder.

    Background job. Each pass takes a render generation and a snapshot of the stored
    lines (:func:`_begin_render`) and publishes it with :func:`_publish`. A pass that a
    newer one has superseded stops: the newer pass renders at least as many lines. If
    newer lines were stored meanwhile, it renders again. Whether a file is current is
    decided by reading it back, never by the recorded high-water mark, so a retry after
    a failed or interrupted pass always repairs the file.
    """
    for _pass in range(MAX_RENDER_PASSES):
        begun = _begin_render(job_id)
        if begun is None:
            return
        job, generation, through = begun
        rows = job.events.filter(id__lte=through).order_by("id").values_list("raw", flat=True)
        data = b"".join(raw.encode("utf-8") + b"\n" for raw in rows.iterator())
        job_folder = None if job.scan_jsonl_job_identifier else f"{JOB_NAMESPACE}/{job.pk}"
        targets: list[tuple[_Target, StorageBackend, str | None]] = [
            (_JOB_TARGET, _scan_storage(), job_folder)
        ]
        book = job.book
        if job.state == ScanJob.State.COMPLETE and book is not None:
            storage = intake._default_storage()
            folder = None if job.scan_jsonl_book_identifier else _book_folder(book, storage)
            targets.append((_BOOK_TARGET, storage, folder))
        for target, target_storage, target_folder in targets:
            if not _publish(job, generation, through, data, target, target_storage, target_folder):
                logger.info(
                    "scan_jsonl_render_superseded",
                    scan_job_id=job.pk,
                    target=target.name,
                    generation=generation,
                )
                return
        latest = job.events.aggregate(top=Max("id"))["top"]
        if latest == through:
            logger.info("scan_jsonl_rendered", scan_job_id=job.pk, bytes=len(data))
            return
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
