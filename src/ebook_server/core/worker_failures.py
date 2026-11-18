"""Terminal worker failures and one persistent human recovery task per attempt chain.

Manual retries use new ProcessingJobs: historical workers cannot overwrite a new
attempt's job state. All attempts point to the original task, and only its latest
attempt can change its outcome. Automatic RQ retries never create a human task.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.core.exceptions import PermissionDenied
from django.db import close_old_connections, transaction
from django.db.models.signals import post_save
from django.utils import timezone
from rq import get_current_job

from .models import ProcessingJob, ReviewTask, ScanJob

SUPPORTED = frozenset(
    {
        ProcessingJob.JobType.CONVERSION,
        ProcessingJob.JobType.OCR,
        ProcessingJob.JobType.SCAN_FINALIZATION,
    }
)
REQUEST_FIELDS: dict[str, set[str]] = {
    ProcessingJob.JobType.CONVERSION: {
        "book_id",
        "source_variant_id",
        "target_format",
        "route",
        "request_options",
    },
    ProcessingJob.JobType.OCR: {
        "source_type",
        "source_variant_id",
        "scan_job_id",
        "book_id",
        "target_formats",
        "request_language",
        "engine",
        "model",
        "request_options",
    },
    ProcessingJob.JobType.SCAN_FINALIZATION: {
        "scan_job_id",
        "create_cleaned_variant",
        "allow_raw_pages",
        "auto_ocr",
        "target_formats",
        "request_language",
        "engine",
        "model",
        "request_options",
    },
}


class RecoveryError(ValueError):
    pass


def retry_pending(job: ProcessingJob, rq_job: Any = None) -> bool:
    current = rq_job if rq_job is not None else get_current_job()
    args = getattr(current, "args", ()) if current is not None else ()
    return bool(
        args and str(args[0]) == str(job.job_id) and (getattr(current, "retries_left", 0) or 0) > 0
    )


def _owner(job: ProcessingJob) -> tuple[int | None, str]:
    details = job.details if isinstance(job.details, dict) else {}
    requested = details.get("requested_by_user_id")
    owner = (
        get_user_model().objects.filter(pk=requested).values_list("pk", flat=True).first()
        if isinstance(requested, int)
        else None
    )
    if job.file_variant_id is not None:
        source = job.file_variant
        assert source is not None
        return owner or source.uploader_id, source.original_filename
    scan = ScanJob.objects.filter(pk=details.get("scan_job_id")).first()
    return owner or (scan.operator_id if scan else None), f"{job.get_job_type_display()} job"


def _snapshot(task: ReviewTask, action: str | None = None, user_id: int | None = None) -> None:
    previous = task.decision_payload.get("worker_recovery", {})
    previous = previous if isinstance(previous, dict) else {}
    attempts = []
    for job in task.worker_attempts.order_by("pk"):
        details = job.details if isinstance(job.details, dict) else {}
        attempts.append(
            {
                "job_id": str(job.job_id),
                "state": job.state,
                "error": str(details.get("error", ""))[:2000],
                "scheduled_at": job.scheduled_at.isoformat(),
                "finished_at": job.finished_at.isoformat() if job.finished_at else None,
            }
        )
    task.decision_payload = {
        **task.decision_payload,
        "worker_recovery": {
            **previous,
            "attempts": attempts,
            **(
                {"action": action, "actor_id": user_id, "acted_at": timezone.now().isoformat()}
                if action
                else {}
            ),
        },
    }


def sync_outcome(sender: Any, instance: ProcessingJob, **kwargs: Any) -> None:
    del sender
    if kwargs.get("raw") or instance.job_type not in SUPPORTED:
        return
    if instance.recovery_task_id is None and instance.state != ProcessingJob.JobState.FAILED:
        return
    if instance.state == ProcessingJob.JobState.FAILED and retry_pending(instance):
        return
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update().get(pk=instance.pk)
        if job.recovery_task_id is None:
            if job.state != ProcessingJob.JobState.FAILED:
                return
            owner_id, filename = _owner(job)
            task, _ = ReviewTask.objects.get_or_create(
                processing_job=job,
                defaults={
                    "task_type": ReviewTask.TaskType.WORKER_FAILURE,
                    "created_by_id": owner_id,
                    "uploader_id": owner_id,
                    "source_filename": filename[:512],
                    "source_path_hint": f"processing-job:{job.job_id}",
                    "inferred_title": job.get_job_type_display(),
                },
            )
            ProcessingJob.objects.filter(pk=job.pk).update(recovery_task=task)
            job.recovery_task_id = task.pk
            instance.recovery_task_id = task.pk
        task = ReviewTask.objects.select_for_update().get(pk=job.recovery_task_id)
        newest = task.worker_attempts.order_by("-pk").values_list("pk", flat=True).first()
        if newest != job.pk:
            return  # A historical worker may not change the current recovery action.
        if task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
            return  # Repeated callbacks may not reopen an acknowledged failure.
        _snapshot(task)
        if job.state == ProcessingJob.JobState.SUCCESS:
            task.state = ReviewTask.State.RESOLVED
            task.decided_at = timezone.now()
            if task.decision == ReviewTask.Decision.UNRESOLVED:
                task.decision = ReviewTask.Decision.RETRY_JOB
            task.decision_notes = "Worker completed successfully."
        elif job.state == ProcessingJob.JobState.FAILED:
            task.state = ReviewTask.State.OPEN
            task.claimed_by = None
            task.decision_notes = "Worker attempt failed; retry or acknowledge the failure."
        task.save()


def connect_signals() -> None:
    post_save.connect(sync_outcome, sender=ProcessingJob, dispatch_uid="worker_failure_recovery")


def permitted(task: ReviewTask, user: Any) -> bool:
    profile = getattr(user, "library_profile", None)
    return bool(
        profile
        and not profile.is_disabled
        and task.processing_job_id is not None
        and task.task_type == ReviewTask.TaskType.WORKER_FAILURE
        and task.can_be_resolved_by(user)
    )


def recover(
    task_id: int,
    *,
    user: Any,
    action: str,
    expected_job_id: str | None = None,
) -> ProcessingJob:
    if action not in {"retry", "acknowledge"}:
        raise RecoveryError("Choose retry or acknowledge.")
    task = ReviewTask.objects.get(pk=task_id)
    if not permitted(task, user):
        raise PermissionDenied
    current_id = task.worker_attempts.order_by("-pk").values_list("pk", flat=True).first()
    if current_id is None:
        raise RecoveryError("This task has no recorded worker attempt.")
    with transaction.atomic():
        # Same lock order as job completion: attempt first, then its task.
        current = ProcessingJob.objects.select_for_update().get(pk=current_id)
        task = ReviewTask.objects.select_for_update().get(pk=task_id)
        if not permitted(task, user):
            raise PermissionDenied
        newest = task.worker_attempts.order_by("-pk").values_list("pk", flat=True).first()
        if expected_job_id is not None and str(current.job_id) != expected_job_id:
            raise RecoveryError("The displayed attempt changed. Reload this task before acting.")
        if (
            action == "acknowledge"
            and task.state == ReviewTask.State.RESOLVED
            and task.decision == ReviewTask.Decision.ACKNOWLEDGE_FAILURE
        ):
            return current
        if newest != current.pk or current.state != ProcessingJob.JobState.FAILED:
            raise RecoveryError("The job changed or a retry is already active. Reload this task.")
        if task.state not in {ReviewTask.State.OPEN, ReviewTask.State.CLAIMED}:
            raise RecoveryError("This recovery task is already closed.")
        if task.claimed_by_id not in {None, user.pk} and user.library_profile.role != "admin":
            raise RecoveryError("Another reviewer has claimed this task.")
        task.reviewer = user
        if action == "acknowledge":
            task.state = ReviewTask.State.RESOLVED
            task.decision = ReviewTask.Decision.ACKNOWLEDGE_FAILURE
            task.decided_at = timezone.now()
            task.decision_notes = "Failure acknowledged; source files retained."
            _snapshot(task, action, user.pk)
            task.save()
            return current
        details = current.details if isinstance(current.details, dict) else {}
        copied = {
            key: value for key, value in details.items() if key in REQUEST_FIELDS[current.job_type]
        }
        copied.update(requested_by_user_id=user.pk, error="")
        if current.job_type == ProcessingJob.JobType.SCAN_FINALIZATION:
            from . import scan_lifecycle

            scan = ScanJob.objects.select_for_update().filter(pk=details.get("scan_job_id")).first()
            if scan is None:
                raise RecoveryError(
                    "The scan no longer exists; acknowledge the failure "
                    "or contact an administrator."
                )
            if scan.state not in scan_lifecycle.PROCESSING_STATES:
                raise RecoveryError("The scan is not ready for finalization. Check its workspace.")
            if scan.todos.filter(state__in=["open", "claimed"]).exists():
                raise RecoveryError("Resolve the scan's outstanding to-dos before retrying.")
            copied.update(prior_scan_state=scan.state, attempt_count=0)
            scan.state = ScanJob.State.FINALIZING
            scan.finished_at = None
            scan.save(update_fields=["state", "finished_at", "updated_at"])
        retry = ProcessingJob.objects.create(
            file_variant=current.file_variant,
            job_type=current.job_type,
            recovery_task=task,
            details=copied,
        )
        task.state = ReviewTask.State.CLAIMED
        task.claimed_by = user
        task.decision = ReviewTask.Decision.RETRY_JOB
        task.decided_at = None
        task.decision_notes = "Retry queued; this task will record its outcome."
        _snapshot(task, action, user.pk)
        task.save()
        transaction.on_commit(lambda: dispatch_retry(retry))
        return retry


def dispatch_retry(job: ProcessingJob) -> None:
    from . import conversion, ocr, scan_pipeline

    if job.job_type == ProcessingJob.JobType.CONVERSION:
        conversion._dispatch_conversion_job(job)
    elif job.job_type == ProcessingJob.JobType.OCR:
        ocr._dispatch_ocr_job(job)
    else:
        try:
            scan_pipeline._dispatch_scan_finalization_job(str(job.job_id))
        except Exception as exc:
            job.refresh_from_db()
            if job.state == ProcessingJob.JobState.QUEUED:
                job.state = ProcessingJob.JobState.FAILED
                job.finished_at = timezone.now()
                job.details = {**job.details, "error": f"queue dispatch failed: {exc}"}
                job.save(update_fields=["state", "finished_at", "details"])
                scan = ScanJob.objects.filter(pk=job.details.get("scan_job_id")).first()
                if scan is not None:
                    scan_pipeline._restore_scan_after_failure(scan, job.details)


def rq_failure_callback(
    rq_job: Any,
    connection: Any,
    kind: Any,
    value: Any,
    traceback: Any,
    *,
    allow_retry: bool = True,
) -> None:
    """RQ also calls this after timeouts/worker exceptions outside the normal handler."""
    del connection, kind, traceback
    args = getattr(rq_job, "args", ())
    if not args:
        return
    close_old_connections()
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update().filter(job_id=args[0]).first()
        if job is None or job.job_type not in SUPPORTED:
            return
        if job.state not in {ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING}:
            return
        pending = allow_retry and retry_pending(job, rq_job)
        job.state = ProcessingJob.JobState.QUEUED if pending else ProcessingJob.JobState.FAILED
        job.finished_at = None if pending else timezone.now()
        job.details = {**job.details, "error": str(value)[:2000]}
        job.save(update_fields=["state", "finished_at", "details"])
    if job.job_type == ProcessingJob.JobType.SCAN_FINALIZATION and not pending:
        from . import scan_pipeline

        scan = ScanJob.objects.filter(pk=job.details.get("scan_job_id")).first()
        if scan is not None:
            scan_pipeline._restore_scan_after_failure(scan, job.details)


def rq_stopped_callback(rq_job: Any, connection: Any) -> None:
    rq_failure_callback(
        rq_job,
        connection,
        RuntimeError,
        "Worker job stopped by operator",
        None,
        allow_retry=False,
    )


def work_horse_killed(rq_job: Any, pid: int, status: int, usage: Any) -> None:
    del pid, usage
    rq_failure_callback(
        rq_job,
        None,
        RuntimeError,
        f"Worker process terminated unexpectedly (status {status})",
        None,
    )
