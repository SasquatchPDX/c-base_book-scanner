"""The explicit Run all action. Finish each stage across the book before the next."""

from __future__ import annotations

from time import sleep
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone

from ebook_server.jobs import InlineQueue, get_queue

from . import scan_artifact_pruning, scan_lifecycle, scantailor
from .models import ProcessingJob, ScanJob, ScanTailorRun, ScanTailorStageState

logger = structlog.get_logger(__name__)
State = ScanTailorRun.State
ACTIVE = (State.QUEUED, State.RUNNING)


class RunError(RuntimeError):
    pass


def start(job_id: str, user_id: int) -> ScanTailorRun:
    with transaction.atomic():
        job = ScanJob.objects.select_for_update().get(pk=job_id)
        if job.state not in scan_lifecycle.PROCESSING_STATES:
            raise RunError("This scan job is not accepting processing changes.")
        active = job.pipeline_runs.filter(state__in=ACTIVE).first()
        if active is not None:
            return active
        if message := scan_artifact_pruning.low_space_message():
            raise RunError(message)
        page_ids = [
            str(page_id)
            for page_id in job.pages.order_by("logical_sequence_number", "side").values_list(
                "pk", flat=True
            )
        ]
        if not page_ids:
            raise RunError("This scan job has no pages.")
        run = job.pipeline_runs.create(page_ids=page_ids, requested_by_id=user_id)
        transaction.on_commit(lambda: _dispatch_first_chunk(run.pk))
    run.refresh_from_db()
    return run


def _dispatch_first_chunk(run_id: int) -> None:
    try:
        dispatch(run_id, 0)
    except Exception:
        logger.exception("scantailor_run_dispatch_failed", pipeline_run_id=run_id)
        _fail(run_id, "Could not queue the run.")


def cancel(job_id: str, run_id: int) -> None:
    with transaction.atomic():
        if not ScanTailorRun.objects.filter(pk=run_id, scan_job_id=job_id, state__in=ACTIVE).update(
            state=State.CANCELLED, updated_at=timezone.now()
        ):
            return
        jobs = ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.STAGE,
            state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
            details__pipeline_run_id=run_id,
        )
        active_jobs = {str(job.job_id): job for job in jobs}
        for state in ScanTailorStageState.objects.filter(
            scan_job_id=job_id, worker_job_id__in=active_jobs
        ):
            scantailor.release_pipeline_stage(state, state.worker_job_id)
        for job in active_jobs.values():
            job.state = ProcessingJob.JobState.SUCCESS
            job.finished_at = timezone.now()
            job.details = {
                **job.details,
                "superseded": True,
                "superseded_reason": "Run all cancelled",
            }
            job.save(update_fields=["state", "finished_at", "details"])


def payload(run: ScanTailorRun) -> dict[str, Any]:
    page_count = len(run.page_ids)
    order = scantailor.stage_order()
    phase = run.cursor // max(1, page_count)
    return {
        "id": run.pk,
        "state": run.state,
        "page_count": page_count,
        "total_steps": page_count * len(order),
        "checked_steps": run.cursor,
        "failed_steps": run.failed_steps,
        "stage": order[phase] if phase < len(order) else None,
        "error": run.error,
    }


def dispatch(run_id: int, cursor: int) -> None:
    queue = get_queue()
    if queue is None or isinstance(queue, InlineQueue):
        while (next_cursor := _process_chunk(run_id, cursor)) is not None:
            if next_cursor == cursor:
                sleep(0.5)
            cursor = next_cursor
    else:
        queue.enqueue(process, run_id, cursor, at_front=False)


def process(run_id: int, cursor: int) -> None:
    try:
        if (next_cursor := _process_chunk(run_id, cursor)) is not None:
            if next_cursor == cursor:
                # Yield the queue slot to the interactive stage before retrying.
                sleep(0.5)
            dispatch(run_id, next_cursor)
    except Exception:
        logger.exception("scantailor_run_interrupted", pipeline_run_id=run_id)
        _fail(run_id, "Run all was interrupted. Start it again to resume from current results.")


def _fail(run_id: int, message: str) -> None:
    ScanTailorRun.objects.filter(pk=run_id, state__in=ACTIVE).update(
        state=State.FAILED, error=message, updated_at=timezone.now()
    )


def _process_chunk(run_id: int, cursor: int) -> int | None:
    claim = ScanTailorRun.objects.filter(pk=run_id, cursor=cursor, state=State.QUEUED)
    if not claim.update(state=State.RUNNING, updated_at=timezone.now()):
        return None
    run = ScanTailorRun.objects.get(pk=run_id)
    page_count = len(run.page_ids)
    order = scantailor.stage_order()
    phase, first_page = divmod(cursor, page_count)
    stage = order[phase]
    failed = run.failed_steps
    if message := scan_artifact_pruning.low_space_message():
        _fail(run_id, message)
        return None

    for page_id in run.page_ids[first_page : first_page + scantailor.STAGE_BATCH_CHUNK]:
        if not ScanTailorRun.objects.filter(
            pk=run_id, state=State.RUNNING, scan_job__state__in=scan_lifecycle.PROCESSING_STATES
        ).exists():
            cancel(run.scan_job_id, run_id)
            return None
        outcome = scantailor.run_pipeline_step(run, page_id, stage)
        if outcome == "busy":
            break
        if outcome == "failed":
            failed += 1
        cursor += 1

    finished = cursor == page_count * len(order)
    state = (State.FAILED if failed else State.COMPLETE) if finished else State.QUEUED
    updated = ScanTailorRun.objects.filter(pk=run_id, state=State.RUNNING).update(
        cursor=cursor,
        failed_steps=failed,
        state=state,
        error="Some stages could not complete. Check their status in the page strip."
        if finished and failed
        else "",
        updated_at=timezone.now(),
    )
    return cursor if updated and not finished else None
