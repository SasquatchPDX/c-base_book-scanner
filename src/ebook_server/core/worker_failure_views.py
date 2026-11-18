"""Focused recovery UI for failed conversion, OCR and scan-finalization workers."""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.http import HttpRequest, HttpResponse, HttpResponseBase, JsonResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from . import worker_failures
from .models import ProcessingJob, ReviewTask

JOB_ROUTES: dict[str, str] = {
    ProcessingJob.JobType.CONVERSION: "conversion-job-status",
    ProcessingJob.JobType.OCR: "ocr-job-status",
    ProcessingJob.JobType.SCAN_FINALIZATION: "scan-finalization-job-status",
}


def wants_html(request: HttpRequest) -> bool:
    return (
        request.GET.get("view") == "html"
        or "text/html" in request.headers.get("Accept", "").lower()
    )


def detail(request: HttpRequest, task: ReviewTask) -> HttpResponseBase:
    jobs = list(task.worker_attempts.order_by("pk"))
    latest = jobs[-1] if jobs else task.processing_job
    attempts: list[dict[str, Any]] = []
    for job in jobs:
        details = job.details if isinstance(job.details, dict) else {}
        attempts.append(
            {
                "job_id": str(job.job_id),
                "job_type": job.get_job_type_display(),
                "state": job.state,
                "error": str(details.get("error", ""))[:2000],
                "url": reverse(JOB_ROUTES[job.job_type], kwargs={"job_id": job.job_id}),
                "started_at": job.started_at,
                "finished_at": job.finished_at,
            }
        )
    open_task = task.state in {ReviewTask.State.OPEN, ReviewTask.State.CLAIMED}
    profile = getattr(request.user, "library_profile", None)
    claim_permits_action = task.claimed_by_id in {None, request.user.pk} or (
        profile is not None and profile.role == "admin"
    )
    payload = {
        "task": task,
        "attempts": attempts,
        "latest": latest,
        "can_recover": open_task
        and claim_permits_action
        and latest is not None
        and latest.state == ProcessingJob.JobState.FAILED,
        "retry_active": open_task
        and latest is not None
        and latest.state
        in {
            ProcessingJob.JobState.QUEUED,
            ProcessingJob.JobState.RUNNING,
        },
    }
    if wants_html(request):
        return render(request, "core/worker_failure_review.html", payload)
    return JsonResponse(
        {
            "id": str(task.pk),
            "state": task.state,
            "decision": task.decision,
            "task_type": task.task_type,
            "attempts": attempts,
            "can_recover": payload["can_recover"],
            "retry_active": payload["retry_active"],
            "decision_payload": task.decision_payload,
            "expected_job_id": str(latest.job_id) if latest else None,
        }
    )


@require_POST
@login_required
def recover(request: HttpRequest, task_id: int) -> HttpResponseBase:
    try:
        task = ReviewTask.objects.get(pk=task_id)
        if not worker_failures.permitted(task, request.user):
            return HttpResponse(status=403)
        payload = (
            json.loads(request.body) if request.content_type == "application/json" else request.POST
        )
        if not isinstance(payload, dict) and not hasattr(payload, "get"):
            return HttpResponse("Invalid recovery request.", status=400)
        try:
            expected = str(UUID(str(payload.get("expected_job_id", ""))))
        except ValueError:
            return HttpResponse(
                "expected_job_id is required; reload the recovery task.", status=400
            )
        result = worker_failures.recover(
            task_id,
            user=request.user,
            action=str(payload.get("action", "")),
            expected_job_id=expected,
        )
    except (json.JSONDecodeError, UnicodeDecodeError):
        return HttpResponse("Invalid recovery request.", status=400)
    except ReviewTask.DoesNotExist:
        return HttpResponse(status=404)
    except PermissionDenied:
        return HttpResponse(status=403)
    except worker_failures.RecoveryError as exc:
        return HttpResponse(str(exc), status=409, content_type="text/plain; charset=utf-8")
    if wants_html(request):
        return redirect(f"{reverse('review-detail', args=[task_id])}?view=html")
    task = ReviewTask.objects.get(pk=task_id)
    return JsonResponse(
        {"task_id": str(task_id), "job_id": str(result.job_id), "state": task.state}
    )
