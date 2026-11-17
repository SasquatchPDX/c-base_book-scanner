"""Durable repeat metadata searches for archived import review tasks."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from rq import get_current_job

from ebook_server.jobs import get_queue

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
    # Keep the typed evidence of whatever the reviewer left as inferred; terms the
    # reviewer typed in are scored as plain alternatives.
    authors_kept = not authors or authors == base.author_hints
    title_evidence = (
        base.title_evidence
        if not title
        else tuple(item for item in base.title_evidence if item.title == title)[:1]
    )
    return identification.IdentificationInput(
        title_hints=[title] if title else base.title_hints,
        author_hints=base.author_hints if authors_kept else authors,
        isbns=base.isbns,
        folder_hints=base.folder_hints,
        language=base.language,
        publisher=base.publisher,
        description=base.description,
        author_evidence=base.author_evidence if authors_kept else (),
        title_evidence=title_evidence,
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
    died) is failed and replaced. The job returned is made the import's current
    search, which fences every older job out of publishing.
    """
    if review_task.import_job_id is None:
        raise ReviewIdentificationError("review task is not for an import")
    if review_task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
        raise ReviewIdentificationError("review task is already closed")

    with transaction.atomic():
        task = ReviewTask.objects.select_for_update().get(pk=review_task.pk)
        source = ImportJob.objects.select_for_update().get(pk=task.import_job_id)
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
            metadata_search.make_current(source, reused)
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
        metadata_search.make_current(source, processing_job)

    try:
        queue = get_queue()
        if queue is None:
            process_review_identification_job(str(processing_job.job_id))
        else:
            queue.enqueue(
                process_review_identification_job,
                str(processing_job.job_id),
                raise_on_error=True,
                job_id=metadata_search.rq_job_id(processing_job),
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
    """Run one repeat search; publish its candidates only if it is still current.

    Fenced twice (#104): a delivered queue entry only starts while its job is
    QUEUED and the import's current search; and the results only become current
    if, under the task/import/job row locks, the job is still current, still this
    attempt, and the review is still open. Otherwise the run is kept as history.
    """
    with transaction.atomic():
        try:
            processing_job = ProcessingJob.objects.select_for_update().get(job_id=job_id)
        except ProcessingJob.DoesNotExist:
            return
        if processing_job.job_type != ProcessingJob.JobType.REVIEW_IDENTIFICATION:
            return
        details = _details(processing_job)
        current_search_job_id = (
            ImportJob.objects.filter(pk=details.get("import_job_id"))
            .values_list("current_search_job_id", flat=True)
            .first()
        )
        refusal = metadata_search.start_refusal(processing_job, current_search_job_id)
        if refusal is not None:
            if refusal == metadata_search.SUPERSEDED_REASON:
                metadata_search.refuse_superseded_job(processing_job)
            logger.info(
                "review_identification_not_started",
                processing_job_id=str(processing_job.job_id),
                reason=refusal,
            )
            return
        if processing_job.state == ProcessingJob.JobState.RUNNING:
            logger.warning(
                "stale_running_job_reentered",
                processing_job_id=str(processing_job.job_id),
            )
        attempt_count = metadata_search.attempt_of(processing_job) + 1
        details = {**details, "attempt_count": attempt_count, "error": ""}
        processing_job.state = ProcessingJob.JobState.RUNNING
        processing_job.started_at = timezone.now()
        processing_job.finished_at = None
        processing_job.details = details
        processing_job.save(update_fields=["state", "started_at", "finished_at", "details"])

    task: ReviewTask | None = None
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
        signals = _review_signals(task, import_job, details.get("terms"))
        candidates, outcomes = identification.resolve_candidates_with_report(signals)
        discard_reason = _publish(
            processing_job=processing_job,
            task=task,
            import_job=import_job,
            attempt_count=attempt_count,
            details=details,
            signals=signals,
            outcomes=outcomes,
            candidates=candidates,
            started_at=search_started_at,
        )
        logger.info(
            "review_identification_succeeded",
            processing_job_id=str(processing_job.job_id),
            review_task_id=task.id,
            import_job_id=str(import_job.id),
            candidate_count=len(candidates),
            published=not discard_reason,
            discarded=discard_reason,
        )
    except Exception as exc:
        rq_job = get_current_job() if raise_on_error else None
        retry_pending = bool(
            rq_job is not None and rq_job.retries_left is not None and rq_job.retries_left > 0
        )
        discarded = _record_job_failure(
            processing_job=processing_job,
            task=task,
            attempt_count=attempt_count,
            details=details,
            exc=exc,
            retry_pending=retry_pending,
        )
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
                discarded=discarded,
            )
        logger.exception(
            "review_identification_failed",
            processing_job_id=str(processing_job.job_id),
            review_task_id=details.get("review_task_id"),
            retry_pending=retry_pending,
        )
        if raise_on_error:
            raise


def _publish(
    *,
    processing_job: ProcessingJob,
    task: ReviewTask,
    import_job: ImportJob,
    attempt_count: int,
    details: dict[str, Any],
    signals: identification.IdentificationInput,
    outcomes: list[identification.ProviderOutcome],
    candidates: list[identification.IdentificationCandidate],
    started_at: datetime,
) -> str:
    """Store the run; make it current only if still allowed. Returns the discard reason."""
    with transaction.atomic():
        # Same lock order as request_review_identification: task, import, job.
        locked_task = ReviewTask.objects.select_for_update().get(pk=task.pk)
        locked_import = ImportJob.objects.select_for_update().get(pk=import_job.pk)
        locked_job = ProcessingJob.objects.select_for_update().get(pk=processing_job.pk)
        review_closed = (
            locked_task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}
            or locked_import.state == ImportJob.State.COMPLETE
        )
        reason = metadata_search.publication_refusal(
            source=locked_import,
            job=locked_job,
            attempt=attempt_count,
            review_closed=review_closed,
        )
        search = metadata_search.record_search(
            trigger=MetadataSearch.Trigger.REVIEW,
            signals=signals,
            outcomes=outcomes,
            candidate_count=len(candidates),
            import_job=locked_import,
            processing_job_id=locked_job.job_id,
            requested_by_id=details.get("requested_by_user_id"),
            started_at=started_at,
            discarded=reason,
        )
        metadata_search.save_candidates(
            search=search,
            candidates=candidates,
            match_threshold=intake.AUTO_ACCEPT_SCORE,
            current=not reason,
        )
        if reason:
            # A resolved review keeps its decision; a superseded run is history only.
            metadata_search.finish_discarded_job(
                locked_job, attempt=attempt_count, reason=reason, search_id=search.id
            )
            return reason
        update_fields = ["state", "selected_candidate_id", "decision_payload", "updated_at"]
        if candidates:
            # An empty run keeps the earlier candidates current, and their snapshot.
            locked_task.provider_snapshot = metadata_search.snapshot(candidates)
            update_fields.append("provider_snapshot")
        locked_task.state = ReviewTask.State.OPEN
        locked_task.selected_candidate = None
        locked_task.decision_payload = {
            **(
                locked_task.decision_payload
                if isinstance(locked_task.decision_payload, dict)
                else {}
            ),
            "candidate_count": len(candidates),
            "search_job_id": str(locked_job.job_id),
            "search_id": str(search.id),
        }
        locked_task.save(update_fields=update_fields)
        locked_import.state = ImportJob.State.LOW_CONFIDENCE
        locked_import.save(update_fields=["state", "updated_at"])
        locked_job.state = ProcessingJob.JobState.SUCCESS
        locked_job.finished_at = timezone.now()
        locked_job.details = {
            **details,
            "candidate_count": len(candidates),
            "search_id": str(search.id),
            "published": True,
            "error": "",
        }
        locked_job.save(update_fields=["state", "finished_at", "details"])
        processing_job.state = locked_job.state
        processing_job.details = locked_job.details
    return ""


def _record_job_failure(
    *,
    processing_job: ProcessingJob,
    task: ReviewTask | None,
    attempt_count: int,
    details: dict[str, Any],
    exc: Exception,
    retry_pending: bool,
) -> str:
    """Fail (or requeue) this attempt, fenced like publication. Returns a discard reason.

    Only the attempt that still owns the job updates it, and only a current search
    of a still-open review puts the review back to OPEN.
    """
    with transaction.atomic():
        locked_task = (
            ReviewTask.objects.select_for_update().filter(pk=task.pk).first()
            if task is not None
            else None
        )
        locked_import = (
            ImportJob.objects.select_for_update().filter(pk=details.get("import_job_id")).first()
        )
        locked_job = ProcessingJob.objects.select_for_update().get(pk=processing_job.pk)
        if not metadata_search.owns_job(locked_job, attempt_count):
            return metadata_search.TAKEN_OVER_REASON
        locked_job.state = (
            ProcessingJob.JobState.QUEUED if retry_pending else ProcessingJob.JobState.FAILED
        )
        locked_job.finished_at = None if retry_pending else timezone.now()
        locked_job.details = {**details, "error": str(exc)}
        locked_job.save(update_fields=["state", "finished_at", "details"])
        if locked_import is None or locked_import.current_search_job_id != locked_job.job_id:
            return metadata_search.SUPERSEDED_REASON
        if (
            locked_task is not None
            and locked_task.state not in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}
            and not retry_pending
        ):
            locked_task.state = ReviewTask.State.OPEN
            locked_task.save(update_fields=["state", "updated_at"])
    return ""


def _record_failed_search(
    *,
    import_job: ImportJob,
    signals: identification.IdentificationInput,
    outcomes: list[identification.ProviderOutcome] | None,
    processing_job: ProcessingJob,
    requested_by_id: int | None,
    started_at: datetime,
    exc: Exception,
    discarded: str = "",
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
            discarded=discarded,
        )
    except Exception:  # never mask the original failure
        logger.exception(
            "review_identification_failure_not_recorded",
            processing_job_id=str(processing_job.job_id),
        )
