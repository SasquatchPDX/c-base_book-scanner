"""Durable repeat metadata searches for archived import review tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from rq import get_current_job

from ebook_server.jobs import get_queue, is_stale_running

from . import identification, intake, metadata_search
from .models import ImportJob, MetadataSearch, ProcessingJob, ReviewTask

logger = structlog.get_logger(__name__)


class ReviewIdentificationError(RuntimeError):
    """Raised when a repeat review lookup cannot be scheduled."""


def _details(job: ProcessingJob) -> dict[str, Any]:
    return dict(job.details) if isinstance(job.details, dict) else {}


def job_payload(job: ProcessingJob) -> dict[str, Any]:
    details = _details(job)
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "review_task_id": details.get("review_task_id"),
        "import_job_id": details.get("import_job_id"),
        "candidate_count": details.get("candidate_count", 0),
        "terms": details.get("terms"),
        "search_id": details.get("search_id"),
        "attempt_count": details.get("attempt_count", 0),
        "error": details.get("error", ""),
        "scheduled_at": job.scheduled_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _review_signals(
    task: ReviewTask,
    import_job: ImportJob,
    terms: dict[str, Any] | None = None,
) -> identification.IdentificationInput:
    base = identification.build_identification_input(
        source_path=import_job.source_path,
        original_filename=import_job.original_filename,
        extracted_metadata=(import_job.evidence if isinstance(import_job.evidence, dict) else {}),
    )
    # The terms recorded on the job are the ones the reviewer submitted; fall back
    # to the task's saved terms for jobs created before terms were recorded.
    effective = terms if isinstance(terms, dict) else metadata_search.task_terms(task)
    raw_title = effective.get("title")
    title = raw_title.strip() if isinstance(raw_title, str) else ""
    raw_authors = effective.get("authors")
    authors = [
        str(author).strip()
        for author in (raw_authors if isinstance(raw_authors, list) else [])
        if isinstance(author, str) and author.strip()
    ]
    return identification.IdentificationInput(
        title_hints=[title] if title else base.title_hints,
        author_hints=authors if authors else base.author_hints,
        isbns=base.isbns,
        folder_hints=base.folder_hints,
        language=base.language,
        publisher=base.publisher,
        description=base.description,
    )


def request_review_identification(
    *,
    review_task: ReviewTask,
    requested_by_user_id: int | None,
    terms: dict[str, Any] | None = None,
) -> ProcessingJob:
    """Queue a repeat provider search with ``terms`` (default: the task's saved terms).

    An already queued search takes the new terms; a running one with different
    terms raises :class:`metadata_search.SearchInProgressError`; a stale one (worker
    died) is failed and replaced.
    """
    if review_task.import_job_id is None:
        raise ReviewIdentificationError("review task is not for an import")
    if review_task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
        raise ReviewIdentificationError("review task is already closed")

    with transaction.atomic():
        task = ReviewTask.objects.select_for_update().get(pk=review_task.pk)
        search_terms = terms if terms is not None else metadata_search.task_terms(task)
        active = (
            ProcessingJob.objects.select_for_update()
            .filter(
                job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION,
                state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
                details__review_task_id=task.id,
            )
            .order_by("-scheduled_at")
            .first()
        )
        reused = metadata_search.reuse_or_replace_active_job(active, search_terms)
        if reused is not None:
            return reused
        processing_job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION,
            details={
                "review_task_id": task.id,
                "import_job_id": str(task.import_job_id),
                "requested_by_user_id": requested_by_user_id,
                "terms": search_terms,
                "candidate_count": 0,
                "attempt_count": 0,
                "error": "",
            },
        )

    try:
        queue = get_queue()
        if queue is None:
            process_review_identification_job(str(processing_job.job_id))
        else:
            queue.enqueue(
                process_review_identification_job,
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
        ReviewTask.objects.filter(pk=review_task.pk).update(state=ReviewTask.State.OPEN)
        raise ReviewIdentificationError(str(processing_job.details.get("error") or exc)) from exc
    processing_job.refresh_from_db()
    return processing_job


def process_review_identification_job(job_id: str, *, raise_on_error: bool = False) -> None:
    with transaction.atomic():
        try:
            processing_job = ProcessingJob.objects.select_for_update().get(job_id=job_id)
        except ProcessingJob.DoesNotExist:
            return
        if processing_job.job_type != ProcessingJob.JobType.REVIEW_IDENTIFICATION:
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

    task: ReviewTask | None = None
    mode = "text"
    signals: identification.IdentificationInput | None = None
    outcomes: list[identification.ProviderOutcome] | None = None
    search_started_at = timezone.now()
    try:
        task = ReviewTask.objects.select_related("import_job").get(pk=details["review_task_id"])
        if task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
            raise ReviewIdentificationError("review task was closed before search completed")
        import_job = task.import_job
        if import_job is None:
            raise ReviewIdentificationError("review task is not for an import")
        terms = details.get("terms")
        if metadata_search.is_isbn_terms(terms):
            mode = "isbn"
            signals = metadata_search.isbn_signals(str(terms["isbn"]))
        else:
            signals = _review_signals(task, import_job, terms)
        candidates, outcomes = identification.resolve_candidates_with_report(signals)
        with transaction.atomic():
            search = metadata_search.record_search(
                trigger=MetadataSearch.Trigger.REVIEW,
                signals=signals,
                outcomes=outcomes,
                candidate_count=len(candidates),
                import_job=import_job,
                processing_job_id=processing_job.job_id,
                requested_by_id=details.get("requested_by_user_id"),
                started_at=search_started_at,
                mode=mode,
            )
            metadata_search.save_candidates(
                search=search,
                candidates=candidates,
                match_threshold=intake.AUTO_ACCEPT_SCORE,
            )
            update_fields = ["state", "selected_candidate_id", "decision_payload", "updated_at"]
            if candidates:
                # An empty run keeps the earlier candidates current, and their snapshot.
                task.provider_snapshot = metadata_search.snapshot(candidates)
                update_fields.append("provider_snapshot")
            task.state = ReviewTask.State.OPEN
            task.selected_candidate = None
            task.decision_payload = {
                **(task.decision_payload if isinstance(task.decision_payload, dict) else {}),
                "candidate_count": len(candidates),
                "search_job_id": str(processing_job.job_id),
                "search_id": str(search.id),
            }
            task.save(update_fields=update_fields)
            import_job.state = ImportJob.State.LOW_CONFIDENCE
            import_job.save(update_fields=["state", "updated_at"])
            processing_job.state = ProcessingJob.JobState.SUCCESS
            processing_job.finished_at = timezone.now()
            processing_job.details = {
                **details,
                "candidate_count": len(candidates),
                "search_id": str(search.id),
                "error": "",
            }
            processing_job.save(update_fields=["state", "finished_at", "details"])
        logger.info(
            "review_identification_succeeded",
            processing_job_id=str(processing_job.job_id),
            review_task_id=task.id,
            import_job_id=str(import_job.id),
            candidate_count=len(candidates),
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
        if (
            task is not None
            and task.state not in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}
            and not retry_pending
        ):
            task.state = ReviewTask.State.OPEN
            task.save(update_fields=["state", "updated_at"])
        if signals is not None and task is not None and task.import_job is not None:
            # The providers were asked: record the failed run so the review page never
            # shows a search that silently produced nothing.
            _record_failed_search(
                import_job=task.import_job,
                signals=signals,
                outcomes=outcomes,
                processing_job=processing_job,
                requested_by_id=details.get("requested_by_user_id"),
                started_at=search_started_at,
                exc=exc,
                mode=mode,
            )
        logger.exception(
            "review_identification_failed",
            processing_job_id=str(processing_job.job_id),
            review_task_id=details.get("review_task_id"),
            retry_pending=retry_pending,
        )
        if raise_on_error:
            raise


def _record_failed_search(
    *,
    import_job: ImportJob,
    signals: identification.IdentificationInput,
    outcomes: list[identification.ProviderOutcome] | None,
    processing_job: ProcessingJob,
    requested_by_id: int | None,
    started_at: datetime,
    exc: Exception,
    mode: str,
) -> None:
    stage = "saving the results failed" if outcomes is not None else "the search failed"
    try:
        metadata_search.record_search(
            trigger=MetadataSearch.Trigger.REVIEW,
            signals=signals,
            outcomes=outcomes,
            import_job=import_job,
            processing_job_id=processing_job.job_id,
            requested_by_id=requested_by_id,
            started_at=started_at,
            error=f"{stage}: {type(exc).__name__}",
            mode=mode,
        )
    except Exception:  # never mask the original failure
        logger.exception(
            "review_identification_failure_not_recorded",
            processing_job_id=str(processing_job.job_id),
        )
