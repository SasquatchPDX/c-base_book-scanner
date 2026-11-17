"""Durable bibliographic identification for physical scan jobs."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from rq import get_current_job

from ebook_server.jobs import get_queue, is_stale_running

from . import identification, metadata_search
from .models import MetadataSearch, ProcessingJob, ReviewTask, ScanJob

logger = structlog.get_logger(__name__)


class ScanIdentificationError(RuntimeError):
    """Raised when scan identification cannot be requested or completed."""


def _details(job: ProcessingJob) -> dict[str, Any]:
    return dict(job.details) if isinstance(job.details, dict) else {}


def job_payload(job: ProcessingJob) -> dict[str, Any]:
    """Serialize durable scan-identification state."""
    details = _details(job)
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "scan_job_id": details.get("scan_job_id"),
        "review_task_id": details.get("review_task_id"),
        "candidate_count": details.get("candidate_count", 0),
        "terms": details.get("terms"),
        "search_id": details.get("search_id"),
        "attempt_count": details.get("attempt_count", 0),
        "error": details.get("error", ""),
        "scheduled_at": job.scheduled_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _provisional_terms(scan_job: ScanJob) -> dict[str, Any]:
    return {
        "title": scan_job.provisional_title.strip(),
        "authors": [
            str(author).strip()
            for author in scan_job.provisional_authors
            if isinstance(author, str) and author.strip()
        ],
    }


def _signals(terms: dict[str, Any]) -> identification.IdentificationInput:
    raw_title = terms.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    raw_authors = terms.get("authors")
    authors = [
        str(author).strip()
        for author in (raw_authors if isinstance(raw_authors, list) else [])
        if isinstance(author, str) and author.strip()
    ]
    return identification.IdentificationInput(
        title_hints=[title] if title else [],
        author_hints=authors,
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )


def _create_or_refresh_review(
    scan_job: ScanJob,
    candidates: list[identification.IdentificationCandidate],
) -> ReviewTask:
    """Create the scan's review task, or refresh an open one after a new search.

    An existing task keeps the reviewer's saved search terms: the scan job's
    provisional hints only seed a new task. An empty run keeps the earlier
    candidates' snapshot, because those candidates stay current.
    """
    task = ReviewTask.objects.filter(
        scan_job=scan_job,
        state__in=[ReviewTask.State.OPEN, ReviewTask.State.CLAIMED],
    ).first()
    is_repeat_search = task is not None and task.decision == ReviewTask.Decision.SEARCH_AGAIN
    prior_payload = (
        task.decision_payload
        if task is not None and isinstance(task.decision_payload, dict)
        else {}
    )
    prior_reviewer = task.reviewer if task is not None else None
    prior_notes = task.decision_notes if task is not None else ""
    prior_decided_at = task.decided_at if task is not None else None
    values: dict[str, Any] = {
        "created_by": scan_job.operator,
        "uploader": scan_job.operator,
        "source_filename": f"physical scan {scan_job.id}",
        "source_path_hint": f"scan-job:{scan_job.id}",
        "embedded_metadata": {
            "scanner_id": scan_job.scanner_id,
            "expected_page_count": scan_job.expected_page_count,
        },
        "state": ReviewTask.State.OPEN,
        "decision": (
            ReviewTask.Decision.SEARCH_AGAIN if is_repeat_search else ReviewTask.Decision.UNRESOLVED
        ),
        "decision_payload": {
            **(prior_payload if is_repeat_search else {}),
            "candidate_count": len(candidates),
        },
        "claimed_by": None,
        "reviewer": prior_reviewer if is_repeat_search else None,
        "selected_candidate": None,
        "decision_notes": prior_notes if is_repeat_search else "",
        "decided_at": prior_decided_at if is_repeat_search else None,
    }
    if candidates or task is None:
        values["provider_snapshot"] = metadata_search.snapshot(candidates)
    if task is None:
        return ReviewTask.objects.create(
            scan_job=scan_job,
            inferred_title=scan_job.provisional_title,
            inferred_authors=scan_job.provisional_authors,
            **values,
        )
    for field, value in values.items():
        setattr(task, field, value)
    task.save()
    return task


def request_scan_identification(
    *,
    scan_job: ScanJob,
    requested_by_user_id: int | None,
    terms: dict[str, Any] | None = None,
) -> ProcessingJob:
    """Persist and dispatch metadata lookup outside the request process.

    ``terms`` defaults to the scan job's provisional hints. An already queued
    search takes new terms; a running one with different terms raises
    :class:`metadata_search.SearchInProgressError`; a stale one is replaced.
    """
    if scan_job.book_id is not None:
        raise ScanIdentificationError("scan job is already linked to a book")
    if scan_job.state in {ScanJob.State.COMPLETE, ScanJob.State.CANCELLED}:
        raise ScanIdentificationError("scan job is not open for identification")

    with transaction.atomic():
        locked = ScanJob.objects.select_for_update().get(pk=scan_job.pk)
        search_terms = terms if terms is not None else _provisional_terms(locked)
        if not search_terms.get("title") and not search_terms.get("authors"):
            raise ScanIdentificationError("a provisional title or author is required")
        active = (
            ProcessingJob.objects.select_for_update()
            .filter(
                job_type=ProcessingJob.JobType.SCAN_IDENTIFICATION,
                state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
                details__scan_job_id=str(locked.id),
            )
            .order_by("-scheduled_at")
            .first()
        )
        reused = metadata_search.reuse_or_replace_active_job(active, search_terms)
        if reused is not None:
            return reused
        processing_job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.SCAN_IDENTIFICATION,
            details={
                "scan_job_id": str(locked.id),
                "requested_by_user_id": requested_by_user_id,
                "terms": search_terms,
                "attempt_count": 0,
                "candidate_count": 0,
                "error": "",
            },
        )

    try:
        queue = get_queue()
        if queue is None:
            process_scan_identification_job(str(processing_job.job_id))
        else:
            queue.enqueue(
                process_scan_identification_job,
                str(processing_job.job_id),
                raise_on_error=True,
            )
    except Exception as exc:
        processing_job.refresh_from_db()
        details = _details(processing_job)
        if processing_job.state != ProcessingJob.JobState.FAILED:
            processing_job.state = ProcessingJob.JobState.FAILED
            processing_job.finished_at = timezone.now()
            processing_job.details = {**details, "error": f"queue dispatch failed: {exc}"}
            processing_job.save(update_fields=["state", "finished_at", "details"])
        raise ScanIdentificationError(str(processing_job.details.get("error") or exc)) from exc

    processing_job.refresh_from_db()
    return processing_job


def process_scan_identification_job(job_id: str, *, raise_on_error: bool = False) -> None:
    """Resolve public metadata candidates and create a reusable review task."""
    with transaction.atomic():
        try:
            processing_job = ProcessingJob.objects.select_for_update().get(job_id=job_id)
        except ProcessingJob.DoesNotExist:
            return
        if processing_job.job_type != ProcessingJob.JobType.SCAN_IDENTIFICATION:
            return
        if processing_job.state == ProcessingJob.JobState.SUCCESS:
            return
        if processing_job.state == ProcessingJob.JobState.RUNNING:
            if not is_stale_running(processing_job):
                return
            logger.warning(
                "stale_running_job_reentered",
                processing_job_id=str(processing_job.job_id),
            )
        details = _details(processing_job)
        attempt_count = int(details.get("attempt_count", 0)) + 1
        details = {**details, "attempt_count": attempt_count, "error": ""}
        processing_job.state = ProcessingJob.JobState.RUNNING
        processing_job.started_at = timezone.now()
        processing_job.finished_at = None
        processing_job.details = details
        processing_job.save(update_fields=["state", "started_at", "finished_at", "details"])

    scan_job: ScanJob | None = None
    signals: identification.IdentificationInput | None = None
    outcomes: list[identification.ProviderOutcome] | None = None
    search_started_at = timezone.now()
    try:
        scan_job = ScanJob.objects.select_related("operator").get(pk=details["scan_job_id"])
        if scan_job.book_id is not None:
            raise ScanIdentificationError("scan job is already linked to a book")
        terms = details.get("terms")
        signals = _signals(terms if isinstance(terms, dict) else _provisional_terms(scan_job))
        candidates, outcomes = identification.resolve_candidates_with_report(signals)
        with transaction.atomic():
            search = metadata_search.record_search(
                trigger=MetadataSearch.Trigger.SCAN,
                signals=signals,
                outcomes=outcomes,
                candidate_count=len(candidates),
                scan_job=scan_job,
                processing_job_id=processing_job.job_id,
                requested_by_id=details.get("requested_by_user_id"),
                started_at=search_started_at,
            )
            metadata_search.save_candidates(
                search=search,
                candidates=candidates,
                match_threshold=identification.AUTO_ACCEPT_SCORE,
            )
            task = _create_or_refresh_review(scan_job, candidates)
            processing_job.state = ProcessingJob.JobState.SUCCESS
            processing_job.finished_at = timezone.now()
            processing_job.details = {
                **details,
                "candidate_count": len(candidates),
                "review_task_id": task.id,
                "search_id": str(search.id),
                "error": "",
            }
            processing_job.save(update_fields=["state", "finished_at", "details"])
        logger.info(
            "scan_identification_succeeded",
            processing_job_id=str(processing_job.job_id),
            scan_job_id=str(scan_job.id),
            candidate_count=len(candidates),
            review_task_id=task.id,
        )
    except Exception as exc:
        rq_job = get_current_job() if raise_on_error else None
        retry_pending = bool(
            rq_job is not None and rq_job.retries_left is not None and rq_job.retries_left > 0
        )
        processing_job.state = (
            ProcessingJob.JobState.QUEUED if retry_pending else ProcessingJob.JobState.FAILED
        )
        processing_job.finished_at = None if retry_pending else timezone.now()
        processing_job.details = {**details, "error": str(exc)}
        processing_job.save(update_fields=["state", "finished_at", "details"])
        if signals is not None and scan_job is not None:
            _record_failed_search(
                scan_job=scan_job,
                signals=signals,
                outcomes=outcomes,
                processing_job=processing_job,
                requested_by_id=details.get("requested_by_user_id"),
                started_at=search_started_at,
                exc=exc,
            )
        logger.exception(
            "scan_identification_failed",
            processing_job_id=str(processing_job.job_id),
            scan_job_id=details.get("scan_job_id"),
            attempt_count=details.get("attempt_count"),
            retry_pending=retry_pending,
        )
        if raise_on_error:
            raise


def _record_failed_search(
    *,
    scan_job: ScanJob,
    signals: identification.IdentificationInput,
    outcomes: list[identification.ProviderOutcome] | None,
    processing_job: ProcessingJob,
    requested_by_id: int | None,
    started_at: datetime,
    exc: Exception,
) -> None:
    stage = "saving the results failed" if outcomes is not None else "the search failed"
    try:
        metadata_search.record_search(
            trigger=MetadataSearch.Trigger.SCAN,
            signals=signals,
            outcomes=outcomes,
            scan_job=scan_job,
            processing_job_id=processing_job.job_id,
            requested_by_id=requested_by_id,
            started_at=started_at,
            error=f"{stage}: {type(exc).__name__}",
        )
    except Exception:  # never mask the original failure
        logger.exception(
            "scan_identification_failure_not_recorded",
            processing_job_id=str(processing_job.job_id),
        )
