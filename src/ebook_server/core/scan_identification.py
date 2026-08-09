"""Durable bibliographic identification for physical scan jobs."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from rq import get_current_job

from ebook_server.jobs import get_queue

from . import identification
from .models import MetadataCandidate, ProcessingJob, ReviewTask, ScanJob

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
        "attempt_count": details.get("attempt_count", 0),
        "error": details.get("error", ""),
        "scheduled_at": job.scheduled_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _signals(scan_job: ScanJob) -> identification.IdentificationInput:
    authors = [
        str(author).strip()
        for author in scan_job.provisional_authors
        if isinstance(author, str) and author.strip()
    ]
    title = scan_job.provisional_title.strip()
    return identification.IdentificationInput(
        title_hints=[title] if title else [],
        author_hints=authors,
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )


def _save_candidates(
    scan_job: ScanJob,
    candidates: list[identification.IdentificationCandidate],
) -> None:
    MetadataCandidate.objects.filter(scan_job=scan_job).delete()
    for index, candidate in enumerate(candidates, start=1):
        provider_ref = candidate.provider_ref or f"result-{index}"
        MetadataCandidate.objects.create(
            scan_job=scan_job,
            provider_id=candidate.provider_id,
            provider_ref=provider_ref,
            title=candidate.title,
            authors=candidate.authors,
            isbn10=candidate.isbn10 or "",
            isbn13=candidate.isbn13 or "",
            score=Decimal(str(candidate.score)),
            raw_payload=candidate.raw,
            language=candidate.language or "",
            publisher=candidate.publisher or "",
            description=candidate.description or "",
            publication_year=candidate.publication_year,
            state=(
                MetadataCandidate.CandidateState.MATCH
                if candidate.score >= identification.AUTO_ACCEPT_SCORE
                else MetadataCandidate.CandidateState.REVIEW
            ),
        )


def _create_or_refresh_review(
    scan_job: ScanJob,
    candidates: list[identification.IdentificationCandidate],
) -> ReviewTask:
    task = ReviewTask.objects.filter(
        scan_job=scan_job,
        state__in=[ReviewTask.State.OPEN, ReviewTask.State.CLAIMED],
    ).first()
    snapshot = [
        {
            "provider_id": candidate.provider_id,
            "provider_ref": candidate.provider_ref,
            "score": candidate.score,
        }
        for candidate in candidates
    ]
    values: dict[str, Any] = {
        "created_by": scan_job.operator,
        "uploader": scan_job.operator,
        "source_filename": f"physical scan {scan_job.id}",
        "source_path_hint": f"scan-job:{scan_job.id}",
        "inferred_title": scan_job.provisional_title,
        "inferred_authors": scan_job.provisional_authors,
        "embedded_metadata": {
            "scanner_id": scan_job.scanner_id,
            "expected_page_count": scan_job.expected_page_count,
        },
        "provider_snapshot": snapshot,
        "state": ReviewTask.State.OPEN,
        "decision": ReviewTask.Decision.UNRESOLVED,
        "decision_payload": {"candidate_count": len(candidates)},
        "claimed_by": None,
        "reviewer": None,
        "selected_candidate": None,
        "decision_notes": "",
        "decided_at": None,
    }
    if task is None:
        return ReviewTask.objects.create(scan_job=scan_job, **values)
    for field, value in values.items():
        setattr(task, field, value)
    task.save()
    return task


def request_scan_identification(
    *,
    scan_job: ScanJob,
    requested_by_user_id: int | None,
) -> ProcessingJob:
    """Persist and dispatch metadata lookup outside the request process."""
    if scan_job.book_id is not None:
        raise ScanIdentificationError("scan job is already linked to a book")
    if scan_job.state in {ScanJob.State.COMPLETE, ScanJob.State.CANCELLED}:
        raise ScanIdentificationError("scan job is not open for identification")
    if not scan_job.provisional_title.strip() and not scan_job.provisional_authors:
        raise ScanIdentificationError("a provisional title or author is required")

    with transaction.atomic():
        locked = ScanJob.objects.select_for_update().get(pk=scan_job.pk)
        active = (
            ProcessingJob.objects.filter(
                job_type=ProcessingJob.JobType.SCAN_IDENTIFICATION,
                state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
                details__scan_job_id=str(locked.id),
            )
            .order_by("-scheduled_at")
            .first()
        )
        if active is not None:
            return active
        processing_job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.SCAN_IDENTIFICATION,
            details={
                "scan_job_id": str(locked.id),
                "requested_by_user_id": requested_by_user_id,
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
        if processing_job.state in {
            ProcessingJob.JobState.RUNNING,
            ProcessingJob.JobState.SUCCESS,
        }:
            return
        details = _details(processing_job)
        attempt_count = int(details.get("attempt_count", 0)) + 1
        details = {**details, "attempt_count": attempt_count, "error": ""}
        processing_job.state = ProcessingJob.JobState.RUNNING
        processing_job.started_at = timezone.now()
        processing_job.finished_at = None
        processing_job.details = details
        processing_job.save(update_fields=["state", "started_at", "finished_at", "details"])

    try:
        scan_job = ScanJob.objects.select_related("operator").get(pk=details["scan_job_id"])
        if scan_job.book_id is not None:
            raise ScanIdentificationError("scan job is already linked to a book")
        candidates = identification.resolve_candidates(_signals(scan_job))
        with transaction.atomic():
            _save_candidates(scan_job, candidates)
            task = _create_or_refresh_review(scan_job, candidates)
            processing_job.state = ProcessingJob.JobState.SUCCESS
            processing_job.finished_at = timezone.now()
            processing_job.details = {
                **details,
                "candidate_count": len(candidates),
                "review_task_id": task.id,
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
        logger.exception(
            "scan_identification_failed",
            processing_job_id=str(processing_job.job_id),
            scan_job_id=details.get("scan_job_id"),
            attempt_count=details.get("attempt_count"),
            retry_pending=retry_pending,
        )
        if raise_on_error:
            raise
