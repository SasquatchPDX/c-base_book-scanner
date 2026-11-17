"""Admin-triggered reorganize: sort archived files into the canonical book-folder tree (#7).

Two persistent ``ProcessingJob`` steps (``job_type=archive_reorganize``), both run by a
worker, never inside a web request:

1. **Plan (dry run).** For the selected Books (picked directly, or every Book imported
   from selected COPY/MOVE sources, or all), list each archived file that is not in
   the folder its Book's current metadata implies, and where it would go. Nothing on
   storage changes.
2. **Apply** a reviewed plan, exactly as reviewed. Each entry is re-validated first:
   the record still points at the planned file with the planned hash, still belongs
   to the planned Book, and that Book's current metadata still implies the planned
   destination folder and name. A mismatch marks the entry ``stale`` and moves
   nothing (a fresh dry run is required); the destination is never recomputed. A
   valid entry is copied into the planned folder (exclusively, never overwriting),
   verified by SHA-256, re-pointed in the database, and only then is the old copy
   deleted (AGENTS.md MOVE rule).

Files on read-only locations (index-in-place sources) are never touched; index sources
cannot be selected at all. This is also how a book follows a corrected title: the
layout never renames on its own.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone

from ebook_server.jobs import get_queue, is_stale_running

from . import archive_layout, book_opf
from .models import (
    Book,
    CoverImage,
    FileVariant,
    ImportJob,
    ProcessingJob,
    ScanJob,
    ScanLog,
    SourceLocation,
    StorageLocation,
)
from .storage_registry import filesystem_backend_for_location

logger = structlog.get_logger(__name__)

# A plan is stored in the job's JSON details; very large piles are planned in batches
# (apply, then plan again for the rest).
MAX_PLAN_ENTRIES = 5000
_KINDS: dict[str, Any] = {
    "variant": FileVariant,
    "cover": CoverImage,
    "scan_log": ScanLog,
}


# An apply entry whose reviewed plan no longer matches reality; a new dry run is needed.
STALE = "stale"


class ReorganizeError(ValueError):
    """Raised for a reorganize request that cannot be planned or applied."""


class ReorganizeDispatchError(RuntimeError):
    """The job was recorded but could not be handed to the queue."""


@dataclass
class ReorganizePlan:
    entries: list[dict[str, Any]] = field(default_factory=list)
    books: int = 0
    in_place: int = 0
    read_only_skipped: int = 0
    truncated: bool = False

    def summary(self) -> str:
        text = (
            f"{len(self.entries)} file(s) to move for {self.books} book(s); "
            f"{self.in_place} already in place; "
            f"{self.read_only_skipped} referenced in place (read-only, untouched)"
        )
        if self.truncated:
            text += f"; plan capped at {MAX_PLAN_ENTRIES}, plan again after applying"
        return text


def _details(job: ProcessingJob) -> dict[str, Any]:
    return dict(job.details) if isinstance(job.details, dict) else {}


def _require_book_scoped_layout() -> None:
    if archive_layout.configured_layout().job_scoped:
        raise ReorganizeError(
            "BOOK_SCANNER_ARCHIVE_LAYOUT is a per-import layout; there is no book "
            "folder tree to sort into",
        )


def validate_source_locations(source_location_ids: Iterable[int]) -> list[int]:
    """Only COPY/MOVE sources can be sorted; an index-in-place source is refused."""
    ids = sorted({int(pk) for pk in source_location_ids})
    sources = list(SourceLocation.objects.filter(pk__in=ids))
    if len(sources) != len(ids):
        raise ReorganizeError("unknown source location")
    index = [
        s.name for s in sources if s.transfer_mode == SourceLocation.TransferMode.INDEX_IN_PLACE
    ]
    if index:
        raise ReorganizeError(
            "index-in-place sources stay where they are and are never reorganized: "
            + ", ".join(sorted(index)),
        )
    return ids


def _book_ids_for_scope(scope: dict[str, Any]) -> list[int]:
    if scope.get("all"):
        return list(Book.objects.order_by("pk").values_list("pk", flat=True))
    ids = {int(pk) for pk in scope.get("book_ids") or []}
    source_ids = scope.get("source_location_ids") or []
    if source_ids:
        validate_source_locations(source_ids)
        ids.update(
            ImportJob.objects.filter(source_location_id__in=source_ids, book__isnull=False)
            .values_list("book_id", flat=True)
            .distinct(),
        )
    return sorted(ids)


def _files_of(book: Book) -> Iterator[tuple[str, Any]]:
    for variant in book.file_variants.select_related("storage_location").order_by("pk"):
        yield "variant", variant
    for cover in book.cover_images.select_related("storage_location").order_by("pk"):
        yield "cover", cover
    # Only stored logs have a file; queued/duplicate/failed uploads have none to move.
    for log in ScanLog.objects.filter(
        scan_job__book=book, state=ScanLog.State.STORED
    ).select_related("storage_location"):
        yield "scan_log", log


def _target_name(kind: str, record: Any, book: Book) -> str:
    suffix = PurePosixPath(record.storage_identifier).suffix
    if kind == "cover":
        return f"cover{suffix or '.jpg'}"
    if kind == "scan_log":
        return "scan.log"
    stem = archive_layout.book_file_stem(book)
    if record.source_type == FileVariant.SourceType.OCR:
        stem = f"{stem} (OCR)"
    return f"{stem}{suffix or '.' + record.format}"


def _folder_of(storage_identifier: str) -> str:
    return str(PurePosixPath(storage_identifier).parent)


def build_plan(scope: dict[str, Any]) -> ReorganizePlan:
    """Dry run: what would move where. Reads the database and checks folder existence."""
    _require_book_scoped_layout()
    plan = ReorganizePlan()
    backends: dict[int, Any] = {}
    for book_id in _book_ids_for_scope(scope):
        book = Book.objects.filter(pk=book_id).first()
        if book is None:
            continue
        plan.books += 1
        for kind, record in _files_of(book):
            location: StorageLocation = record.storage_location
            if location.read_only:
                plan.read_only_skipped += 1
                continue
            storage = backends.setdefault(location.pk, filesystem_backend_for_location(location))
            folder = archive_layout.proposed_book_folder(book, storage)
            if _folder_of(record.storage_identifier).lower() == folder.lower():
                plan.in_place += 1
                continue
            if len(plan.entries) >= MAX_PLAN_ENTRIES:
                plan.truncated = True
                continue
            plan.entries.append(
                {
                    "kind": kind,
                    "id": record.pk,
                    "book_id": book.pk,
                    "storage_location_id": location.pk,
                    "from": record.storage_identifier,
                    "sha256": record.sha256,
                    "to_folder": folder,
                    "name": _target_name(kind, record, book),
                },
            )
    return plan


def _record_book_id(kind: str, record: Any) -> int | None:
    if kind == "scan_log":
        return (
            ScanJob.objects.filter(pk=record.scan_job_id).values_list("book_id", flat=True).first()
        )
    return record.book_id


def _stale_reason(
    entry: dict[str, Any], kind: str, record: Any, book: Book | None, storage: Any
) -> str:
    """Why the reviewed plan no longer describes this move ("" when it still does).

    The plan an admin approved names the record, its file and content hash, its Book,
    and the exact destination folder and file name. If any of that changed since the
    dry run (metadata edited, record relinked or re-pointed, folder taken by another
    book), the entry is stale: it is skipped and a fresh dry run is required instead
    of performing a move nobody reviewed.
    """
    if record.storage_location_id != entry.get(
        "storage_location_id"
    ) or record.storage_identifier != entry.get("from"):
        return "file changed since the plan"
    if entry.get("sha256") and record.sha256 != entry["sha256"]:
        return "file content changed since the plan"
    if book is None or _record_book_id(kind, record) != entry.get("book_id"):
        return "book association changed since the plan"
    planned_folder = str(entry.get("to_folder") or "")
    if archive_layout.proposed_book_folder(book, storage).lower() != planned_folder.lower():
        return "book metadata changed since the plan; its destination would differ"
    if _target_name(kind, record, book) != entry.get("name"):
        return "file name would differ from the plan"
    return ""


def _move_entry(entry: dict[str, Any]) -> tuple[str, str]:
    """Apply one plan entry exactly as reviewed; returns ``(status, detail)``.

    The file goes to the planned ``to_folder`` and nowhere else. A ``stale`` status
    means the entry no longer matches the current state; nothing was changed for it.
    """
    kind = str(entry.get("kind"))
    model = _KINDS.get(kind)
    if model is None:
        return "skipped", "unknown kind"
    record: Any = model.objects.select_related("storage_location").filter(pk=entry["id"]).first()
    if record is None:
        return STALE, "record no longer exists"
    if kind == "scan_log" and record.state != ScanLog.State.STORED:
        return STALE, "log is no longer stored"
    location: StorageLocation | None = record.storage_location
    if location is None or location.read_only:
        return "skipped", "read-only location"
    storage = filesystem_backend_for_location(location)
    book = Book.objects.filter(pk=entry.get("book_id")).first()
    reason = _stale_reason(entry, kind, record, book, storage)
    if reason:
        return STALE, reason
    assert book is not None
    folder = str(entry["to_folder"])
    source = record.storage_identifier
    if _folder_of(source).lower() == folder.lower():
        return "skipped", "already in place"
    if not archive_layout.claim_planned_book_folder(book, folder, storage):
        return STALE, "planned folder is now taken; plan again"
    # Exclusive: a file already at the planned name is never replaced.
    destination = storage.publish_unique(source, folder, str(entry["name"]))
    if storage.hash(destination) != record.sha256:
        storage.delete(destination)
        return "failed", "copy verification failed; original kept"
    with transaction.atomic():
        locked: Any = model.objects.select_for_update().get(pk=record.pk)
        if (
            locked.storage_identifier != source
            or locked.storage_location_id != location.pk
            or _record_book_id(kind, locked) != book.pk
        ):
            storage.delete(destination)
            return STALE, "changed while moving"
        locked.storage_identifier = destination
        locked.save(update_fields=["storage_identifier"])
        if model is FileVariant:
            ImportJob.objects.filter(
                storage_location=location,
                storage_identifier=source,
            ).update(storage_identifier=destination)
    # Only now, with the verified copy recorded, is the old copy removed.
    storage.delete(source)
    storage.prune_empty_parents(source)
    return "moved", destination


def _drop_vacated_opfs(vacated: set[tuple[int, str]]) -> None:
    """Remove the generated ``metadata.opf`` left alone in a folder the files moved out of.

    The book's new folder gets its own OPF from the regeneration job that the moved
    records' updates queue.
    """
    for location_id, folder in sorted(vacated):
        location = StorageLocation.objects.filter(pk=location_id, read_only=False).first()
        if location is not None:
            book_opf.remove_orphaned_opf(filesystem_backend_for_location(location), folder)


def request_reorganize(
    *,
    book_ids: Iterable[int] | None = None,
    source_location_ids: Iterable[int] | None = None,
    all_books: bool = False,
    apply_plan_job: ProcessingJob | None = None,
    requested_by_user_id: int | None = None,
) -> tuple[ProcessingJob, bool]:
    """Record a plan (dry run) or apply job and hand it to the worker queue.

    Returns ``(job, dispatched)``; without a queue the job stays QUEUED for
    ``reorganize_archive --run-queued``. Nothing is planned or moved in the caller.
    """
    _require_book_scoped_layout()
    base = {"requested_by_user_id": requested_by_user_id, "attempt_count": 0, "error": ""}
    if apply_plan_job is None:
        scope: dict[str, Any] = {
            "all": bool(all_books),
            "book_ids": sorted({int(pk) for pk in book_ids or []}),
            "source_location_ids": validate_source_locations(source_location_ids or []),
        }
        if not (scope["all"] or scope["book_ids"] or scope["source_location_ids"]):
            raise ReorganizeError("nothing selected")
        job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.ARCHIVE_REORGANIZE,
            details={"mode": "plan", "scope": scope, **base},
        )
    else:
        with transaction.atomic():
            # Lock the plan so one plan is applied at most once.
            plan_job = ProcessingJob.objects.select_for_update().get(pk=apply_plan_job.pk)
            plan_details = _details(plan_job)
            if (
                plan_job.job_type != ProcessingJob.JobType.ARCHIVE_REORGANIZE
                or plan_details.get("mode") != "plan"
                or plan_job.state != ProcessingJob.JobState.SUCCESS
            ):
                raise ReorganizeError("only a finished reorganize plan (dry run) can be applied")
            if plan_details.get("applied_by_job_id"):
                raise ReorganizeError("this plan was already applied; plan again")
            job = ProcessingJob.objects.create(
                job_type=ProcessingJob.JobType.ARCHIVE_REORGANIZE,
                details={"mode": "apply", "plan_job_id": str(plan_job.job_id), **base},
            )
            plan_job.details = {**plan_details, "applied_by_job_id": str(job.job_id)}
            plan_job.save(update_fields=["details"])
    queue = get_queue()
    if queue is None:
        logger.warning("archive_reorganize_not_dispatched", processing_job_id=str(job.job_id))
        return job, False
    try:
        queue.enqueue(process_reorganize_job, str(job.job_id))
    except Exception as exc:
        job.state = ProcessingJob.JobState.FAILED
        job.finished_at = timezone.now()
        job.details = {**_details(job), "error": f"queue dispatch failed: {exc}"}
        job.save(update_fields=["state", "finished_at", "details"])
        raise ReorganizeDispatchError(str(exc)) from exc
    job.refresh_from_db()
    return job, True


def _claim(job_id: str) -> ProcessingJob | None:
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update().filter(job_id=job_id).first()
        if job is None or job.job_type != ProcessingJob.JobType.ARCHIVE_REORGANIZE:
            return None
        if job.state in {ProcessingJob.JobState.SUCCESS, ProcessingJob.JobState.FAILED}:
            return None
        if job.state == ProcessingJob.JobState.RUNNING and not is_stale_running(job):
            return None
        details = _details(job)
        details["attempt_count"] = int(details.get("attempt_count", 0)) + 1
        details["error"] = ""
        job.state = ProcessingJob.JobState.RUNNING
        job.started_at = timezone.now()
        job.finished_at = None
        job.details = details
        job.save(update_fields=["state", "started_at", "finished_at", "details"])
        return job


def _finish(job: ProcessingJob, details: dict[str, Any], *, failed: bool) -> None:
    job.state = ProcessingJob.JobState.FAILED if failed else ProcessingJob.JobState.SUCCESS
    job.finished_at = timezone.now()
    job.details = details
    job.save(update_fields=["state", "finished_at", "details"])


def process_reorganize_job(job_id: str) -> None:
    """Worker entry point for both plan and apply jobs. Safe to re-run (entries re-check)."""
    job = _claim(job_id)
    if job is None:
        return
    details = _details(job)
    try:
        if details.get("mode") == "plan":
            plan = build_plan(details.get("scope") or {})
            details.update(
                {
                    "plan": plan.entries,
                    "summary": plan.summary(),
                    "books": plan.books,
                    "in_place": plan.in_place,
                    "read_only_skipped": plan.read_only_skipped,
                    "truncated": plan.truncated,
                },
            )
            _finish(job, details, failed=False)
            logger.info(
                "archive_reorganize_planned", processing_job_id=job_id, files=len(plan.entries)
            )
            return
        plan_job = ProcessingJob.objects.get(job_id=details["plan_job_id"])
        entries = _details(plan_job).get("plan") or []
        results: list[dict[str, Any]] = []
        counts = {"moved": 0, "skipped": 0, STALE: 0, "failed": 0}
        vacated: set[tuple[int, str]] = set()
        for entry in entries:
            try:
                status, detail = _move_entry(entry)
            except Exception as exc:  # one bad file must not stop the batch
                status, detail = "failed", f"{type(exc).__name__}: {exc}"
            counts[status] += 1
            if status == "moved":
                vacated.add((int(entry["storage_location_id"]), _folder_of(str(entry["from"]))))
            results.append(
                {
                    "kind": entry.get("kind"),
                    "id": entry.get("id"),
                    "status": status,
                    "detail": detail,
                }
            )
        _drop_vacated_opfs(vacated)
        summary = (
            f"{counts['moved']} moved, {counts['skipped']} skipped, "
            f"{counts[STALE]} stale, {counts['failed']} failed"
        )
        if counts[STALE]:
            summary += "; stale entries were not moved, run a new dry run for them"
        details.update(
            {
                "results": results,
                "counts": counts,
                "requires_new_plan": bool(counts[STALE]),
                "summary": summary,
            },
        )
        if counts["failed"]:
            details["error"] = f"{counts['failed']} file(s) could not be moved; originals kept"
        _finish(job, details, failed=bool(counts["failed"]))
        logger.info("archive_reorganize_applied", processing_job_id=job_id, **counts)
    except Exception as exc:
        details["error"] = str(exc)
        _finish(job, details, failed=True)
        logger.error("archive_reorganize_failed", processing_job_id=job_id, error=str(exc))
        raise


def queued_reorganize_job_ids() -> list[str]:
    return [
        str(job_id)
        for job_id in ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.ARCHIVE_REORGANIZE,
            state=ProcessingJob.JobState.QUEUED,
        )
        .order_by("scheduled_at")
        .values_list("job_id", flat=True)
    ]
