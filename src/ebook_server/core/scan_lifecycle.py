"""Scan job lifecycle on serif (#127, libreflip contract §5).

::

    open -> capturing -> captured -> finalizing -> complete
    open, capturing --abort--> cancelled
    complete --replacement page--> captured ("re-finish needed")

``captured`` is a *raw scan*: the scanner finished, every file is verified, and the job
waits for ScanTailor-Web processing and a human finish. Nothing of it is in the library
(see :mod:`.library_visibility`).

Extension points for later tickets:

* :func:`register_book_linker` -- S6 (#130) links a job to a Book from its OPF at
  finish. Until a linker returns a Book, finish keeps today's behaviour (scan
  identification and the review queue).
* :func:`register_raw_scan_annotator` -- S5/S6 add the OPF title and completeness flags
  to the portal's "Raw scans" list.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Final

from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.utils import timezone

from . import scan_artifact_pruning
from .models import Book, ScanJob, ScanTodo

State = ScanJob.State

# Pages may still be added (session API); the scanner API adds pages in OPEN/CAPTURING.
CAPTURE_STATES: Final = frozenset({State.OPEN, State.CAPTURING, State.PAUSED})
# ScanTailor processing, rescan requests, replacements and finish are allowed.
PROCESSING_STATES: Final = CAPTURE_STATES | {State.CAPTURED}
# Rescan requests and replacement pages: also after the job is complete (D7, §5).
RESCAN_STATES: Final = PROCESSING_STATES | {State.COMPLETE}
# capture-complete (§6.9) and abort (§6.10) start from these.
CAPTURE_COMPLETE_FROM: Final = frozenset({State.OPEN, State.CAPTURING})
ABORT_FROM: Final = frozenset({State.OPEN, State.CAPTURING})
# A repeated capture-complete on a job that already got past it: 200 again for the
# manifest that was verified, any other manifest is checked against what serif holds.
CAPTURE_VERIFIED_STATES: Final = frozenset({State.CAPTURED, State.FINALIZING, State.COMPLETE})

REFINISH_NOTE: Final = "re-finish needed"


class LifecycleError(Exception):
    """A transition is not allowed from the job's current state."""

    def __init__(self, state: str, message: str) -> None:
        super().__init__(message)
        self.state = state


def mark_captured(job: ScanJob, *, finished_at: Any = None, manifest_sha256: str = "") -> ScanJob:
    """``open``/``capturing`` -> ``captured`` on a row the caller has locked.

    ``manifest_sha256`` is the digest of the verified manifest; a later identical
    capture-complete is answered from it.
    """
    if job.state not in CAPTURE_COMPLETE_FROM:
        raise LifecycleError(job.state, f"capture-complete is not allowed from '{job.state}'.")
    job.state = State.CAPTURED
    job.captured_at = timezone.now()
    job.capture_finished_at = finished_at
    job.capture_manifest_sha256 = manifest_sha256
    job.save(
        update_fields=[
            "state",
            "captured_at",
            "capture_finished_at",
            "capture_manifest_sha256",
            "updated_at",
        ]
    )
    return job


def abort(job_id: str) -> ScanJob:
    """Abort Book (§6.10): ``open``/``capturing`` -> ``cancelled``; data is kept.

    Idempotent: an already cancelled job is returned unchanged.
    """
    with transaction.atomic():
        job = ScanJob.objects.select_for_update().get(pk=job_id)
        if job.state == State.CANCELLED:
            return job
        if job.state not in ABORT_FROM:
            raise LifecycleError(job.state, f"abort is not allowed from '{job.state}'.")
        job.state = State.CANCELLED
        job.finished_at = timezone.now()
        job.save(update_fields=["state", "finished_at", "updated_at"])
        # Raw pages stay; only the derived stage files go (#204).
        scan_artifact_pruning.schedule_scan_job_stage_cleanup(str(job.pk))
        return job


def reopen_for_refinish(job: ScanJob) -> bool:
    """A replacement page arrived for a ``complete`` job (§5): back to ``captured``.

    The caller holds the row lock. The library keeps the job's existing variants
    (``refinish_needed``) until the next finish replaces them. Returns whether the job
    was reopened.
    """
    if job.state != State.COMPLETE:
        return False
    job.state = State.CAPTURED
    job.refinish_needed = True
    job.finished_at = None
    notes = dict(job.notes) if isinstance(job.notes, dict) else {}
    notes["lifecycle"] = REFINISH_NOTE
    job.notes = notes
    job.save(update_fields=["state", "refinish_needed", "finished_at", "notes", "updated_at"])
    return True


def clear_refinish(job: ScanJob) -> None:
    """Called when a finish of a reopened job succeeded."""
    if not job.refinish_needed:
        return
    job.refinish_needed = False
    notes = dict(job.notes) if isinstance(job.notes, dict) else {}
    if notes.get("lifecycle") == REFINISH_NOTE:
        notes.pop("lifecycle")
    job.notes = notes
    job.save(update_fields=["refinish_needed", "notes", "updated_at"])


# --- finish: Book linking hook (S6, #130) -----------------------------------------------

BookLinker = Callable[[ScanJob], Book | None]
_book_linkers: list[BookLinker] = []


def register_book_linker(linker: BookLinker) -> None:
    """Add a finish-time linker. S6 registers the OPF linker (§4.5) here."""
    if linker not in _book_linkers:
        _book_linkers.append(linker)


def unregister_book_linker(linker: BookLinker) -> None:
    if linker in _book_linkers:
        _book_linkers.remove(linker)


def link_book_for_finish(job: ScanJob) -> Book | None:
    """Link an unlinked job to a Book before finish, using the registered linkers.

    Each linker returns the Book it linked (and saved on the job) or ``None``. With no
    linker, or when none links, the caller falls back to scan identification.
    """
    if job.book_id is not None:
        return job.book
    for linker in _book_linkers:
        book = linker(job)
        if book is not None:
            return book
    return None


# --- portal "Raw scans" list --------------------------------------------------------------


@dataclass
class RawScanSummary:
    job: ScanJob
    title: str
    page_count: int
    open_rescans: int
    refinish_needed: bool
    #: Completeness flags (S5 projects ``completeness_check`` events into these).
    completeness: dict[str, Any] = field(default_factory=dict)
    #: Bibliographic fields from the OPF (S6), e.g. authors, isbn, language.
    metadata: dict[str, Any] = field(default_factory=dict)


RawScanAnnotator = Callable[[RawScanSummary], None]
_raw_scan_annotators: list[RawScanAnnotator] = []


def register_raw_scan_annotator(annotator: RawScanAnnotator) -> None:
    """Let a later ticket fill in a summary (OPF title, completeness flags)."""
    if annotator not in _raw_scan_annotators:
        _raw_scan_annotators.append(annotator)


def unregister_raw_scan_annotator(annotator: RawScanAnnotator) -> None:
    if annotator in _raw_scan_annotators:
        _raw_scan_annotators.remove(annotator)


def raw_scan_jobs() -> QuerySet[ScanJob]:
    return (
        ScanJob.objects.filter(state=State.CAPTURED)
        .select_related("book")
        .annotate(
            page_total=Count("pages", distinct=True),
            open_rescan_total=Count(
                "todos",
                filter=Q(
                    todos__todo_type=ScanTodo.TodoType.RESCAN,
                    todos__state__in=[ScanTodo.State.OPEN, ScanTodo.State.CLAIMED],
                ),
                distinct=True,
            ),
        )
        .order_by("-captured_at", "-created_at")
    )


def raw_scan_summaries() -> list[RawScanSummary]:
    summaries = []
    for job in raw_scan_jobs():
        summary = RawScanSummary(
            job=job,
            title=job.provisional_title,
            page_count=int(getattr(job, "page_total", 0)),
            open_rescans=int(getattr(job, "open_rescan_total", 0)),
            refinish_needed=job.refinish_needed,
        )
        for annotator in _raw_scan_annotators:
            annotator(summary)
        summaries.append(summary)
    return summaries
