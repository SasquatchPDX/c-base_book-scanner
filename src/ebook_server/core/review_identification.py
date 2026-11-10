"""Durable repeat metadata searches for archived import review tasks."""

from __future__ import annotations

from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone
from rq import get_current_job

from ebook_server.jobs import get_queue, is_stale_running

from . import identification, intake
from .models import ImportJob, ProcessingJob, ReviewTask

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
        "attempt_count": details.get("attempt_count", 0),
        "error": details.get("error", ""),
        "scheduled_at": job.scheduled_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _review_signals(task: ReviewTask, import_job: ImportJob) -> identification.IdentificationInput:
    base = identification.build_identification_input(
        source_path=import_job.source_path,
        original_filename=import_job.original_filename,
        extracted_metadata=(import_job.evidence if isinstance(import_job.evidence, dict) else {}),
    )
    title = task.inferred_title.strip()
    authors = [
        str(author).strip()
        for author in task.inferred_authors
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
) -> ProcessingJob:
    if review_task.import_job_id is None:
        raise ReviewIdentificationError("review task is not for an import")
    if review_task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
        raise ReviewIdentificationError("review task is already closed")

    with transaction.atomic():
        task = ReviewTask.objects.select_for_update().get(pk=review_task.pk)
        active = (
            ProcessingJob.objects.filter(
                job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION,
                state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
                details__review_task_id=task.id,
            )
            .order_by("-scheduled_at")
            .first()
        )
        if active is not None:
            return active
        processing_job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION,
            details={
                "review_task_id": task.id,
                "import_job_id": str(task.import_job_id),
                "requested_by_user_id": requested_by_user_id,
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
    try:
        task = ReviewTask.objects.select_related("import_job").get(pk=details["review_task_id"])
        if task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
            raise ReviewIdentificationError("review task was closed before search completed")
        import_job = task.import_job
        if import_job is None:
            raise ReviewIdentificationError("review task is not for an import")
        candidates = identification.resolve_candidates(_review_signals(task, import_job))
        with transaction.atomic():
            intake._save_metadata_candidates(import_job, candidates)
            task.provider_snapshot = [
                {
                    "provider_id": candidate.provider_id,
                    "provider_ref": candidate.provider_ref,
                    "score": candidate.score,
                }
                for candidate in candidates
            ]
            task.state = ReviewTask.State.OPEN
            task.selected_candidate = None
            task.decision_payload = {
                **(task.decision_payload if isinstance(task.decision_payload, dict) else {}),
                "candidate_count": len(candidates),
                "search_job_id": str(processing_job.job_id),
            }
            task.save(
                update_fields=[
                    "state",
                    "selected_candidate_id",
                    "provider_snapshot",
                    "decision_payload",
                    "updated_at",
                ],
            )
            import_job.state = ImportJob.State.LOW_CONFIDENCE
            import_job.save(update_fields=["state", "updated_at"])
            processing_job.state = ProcessingJob.JobState.SUCCESS
            processing_job.finished_at = timezone.now()
            processing_job.details = {
                **details,
                "candidate_count": len(candidates),
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
        logger.exception(
            "review_identification_failed",
            processing_job_id=str(processing_job.job_id),
            review_task_id=details.get("review_task_id"),
            retry_pending=retry_pending,
        )
        if raise_on_error:
            raise
