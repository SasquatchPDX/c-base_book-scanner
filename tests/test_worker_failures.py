"""Human recovery begins only after worker retries are exhausted."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from django.test import Client
from django.utils import timezone

from ebook_server import jobs
from ebook_server.core import conversion, ocr, scan_pipeline, worker_failures
from ebook_server.core.models import (
    ImportJob,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    get_or_create_profile,
)
from tests.test_conversion import FailingStubConverter, StubConverter
from tests.test_conversion_claim import create_conversion

pytestmark = pytest.mark.django_db(transaction=True)


def failed_job(
    kind: str = ProcessingJob.JobType.CONVERSION, *, owner: str = "failure-owner"
) -> ProcessingJob:
    user = get_or_create_profile(owner).user
    return ProcessingJob.objects.create(
        job_type=kind,
        state=ProcessingJob.JobState.FAILED,
        finished_at=timezone.now(),
        details={"requested_by_user_id": user.pk, "error": "source unavailable"},
    )


@pytest.mark.parametrize("kind", sorted(worker_failures.SUPPORTED))
def test_terminal_failure_has_one_task_with_a_processing_source(kind: str) -> None:
    job = failed_job(kind)
    job.refresh_from_db()
    task = ReviewTask.objects.get(processing_job=job)
    assert job.recovery_task_id == task.pk
    assert task.import_job_id is None and task.scan_job_id is None
    assert task.task_type == ReviewTask.TaskType.WORKER_FAILURE
    job.save(update_fields=["state"])
    worker_failures.sync_outcome(None, job)
    assert ReviewTask.objects.count() == 1
    assert task.worker_attempts.count() == 1


def test_conversion_retry_is_new_job_with_same_task_and_preserved_original(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = get_or_create_profile("conversion-recovery-owner").user
    job, storage = create_conversion(tmp_path, monkeypatch)
    job.details["requested_by_user_id"] = user.pk
    job.save(update_fields=["details"])
    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": FailingStubConverter()})
    conversion.process_conversion_job(str(job.job_id))
    task = ReviewTask.objects.get(processing_job=job)
    source = job.file_variant
    assert source is not None
    original = storage.hash(source.storage_identifier)

    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": StubConverter(b"recovered")})
    monkeypatch.setattr(conversion, "get_queue", lambda: None)
    retry = worker_failures.recover(task.pk, user=user, action="retry")

    retry.refresh_from_db()
    task.refresh_from_db()
    job.refresh_from_db()
    assert retry.pk != job.pk and retry.job_id != job.job_id
    assert job.state == ProcessingJob.JobState.FAILED
    assert retry.state == ProcessingJob.JobState.SUCCESS
    assert retry.recovery_task_id == task.pk
    assert retry.file_variant_id == source.pk
    assert (
        task.state == ReviewTask.State.RESOLVED and task.decision == ReviewTask.Decision.RETRY_JOB
    )
    assert task.processing_job_id == job.pk
    assert [
        attempt["state"] for attempt in task.decision_payload["worker_recovery"]["attempts"]
    ] == [
        "failed",
        "success",
    ]
    assert ReviewTask.objects.count() == 1
    assert storage.hash(source.storage_identifier) == original
    assert retry.details["request_options"] == {"test": "preserved"}


def test_failed_manual_retry_reuses_task_and_acknowledgment_survives_duplicate_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = failed_job()
    task = ReviewTask.objects.get(processing_job=root)
    user = task.uploader
    captured: list[ProcessingJob] = []
    monkeypatch.setattr(worker_failures, "dispatch_retry", captured.append)
    retry = worker_failures.recover(task.pk, user=user, action="retry")
    assert captured == [retry]
    # A late historical worker cannot replace the current pending outcome.
    root.state = ProcessingJob.JobState.SUCCESS
    root.save(update_fields=["state"])
    task.refresh_from_db()
    assert task.state == ReviewTask.State.CLAIMED
    retry.state = ProcessingJob.JobState.FAILED
    retry.details["error"] = "still unavailable"
    retry.finished_at = timezone.now()
    retry.save(update_fields=["state", "details", "finished_at"])
    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN
    assert len(task.decision_payload["worker_recovery"]["attempts"]) == 2
    worker_failures.recover(task.pk, user=user, action="acknowledge")
    retry.save(update_fields=["state"])
    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.ACKNOWLEDGE_FAILURE
    assert ReviewTask.objects.count() == 1
    with pytest.raises(worker_failures.RecoveryError, match="already closed"):
        worker_failures.recover(task.pk, user=user, action="retry")


def test_conversion_automatic_retry_creates_no_task_until_exhausted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)
    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": FailingStubConverter()})
    rq_job = SimpleNamespace(args=[str(job.job_id)], retries_left=1)
    monkeypatch.setattr(worker_failures, "get_current_job", lambda: rq_job)
    with pytest.raises(RuntimeError):
        conversion.process_conversion_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.QUEUED and job.finished_at is None
    assert not ReviewTask.objects.exists()
    rq_job.retries_left = 0
    conversion.process_conversion_job(str(job.job_id))
    assert ReviewTask.objects.filter(processing_job=job).count() == 1


def test_ocr_automatic_retry_then_terminal_failure_and_manual_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = get_or_create_profile("ocr-recovery-owner").user
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.OCR,
        details={"requested_by_user_id": user.pk, "source_type": "variant"},
    )

    def fail(*args: Any, **kwargs: Any) -> Any:
        raise ocr.OCRClientError("OCR temporarily unavailable")

    monkeypatch.setattr(ocr, "_resolve_source_artifact", fail)
    rq_job = SimpleNamespace(args=[str(job.job_id)], retries_left=1)
    monkeypatch.setattr(worker_failures, "get_current_job", lambda: rq_job)
    with pytest.raises(ocr.OCRClientError):
        ocr.process_ocr_job(str(job.job_id))
    assert not ReviewTask.objects.exists()
    rq_job.retries_left = 0
    ocr.process_ocr_job(str(job.job_id))
    task = ReviewTask.objects.get(processing_job=job)
    monkeypatch.setattr(ocr, "get_queue", lambda: None)
    retry = worker_failures.recover(task.pk, user=user, action="retry")
    retry.refresh_from_db()
    assert retry.state == ProcessingJob.JobState.FAILED
    assert ReviewTask.objects.count() == 1


def test_finalization_recovery_preserves_scan_restore_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = get_or_create_profile("finish-recovery-owner").user
    scan = ScanJob.objects.create(operator=user, state=ScanJob.State.PAUSED)
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
        state=ProcessingJob.JobState.FAILED,
        details={
            "requested_by_user_id": user.pk,
            "scan_job_id": str(scan.pk),
            "create_cleaned_variant": True,
            "prior_scan_state": "paused",
            "error": "no pages",
        },
    )
    task = ReviewTask.objects.get(processing_job=job)
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    retry = worker_failures.recover(task.pk, user=user, action="retry")
    retry.refresh_from_db()
    scan.refresh_from_db()
    assert retry.state == ProcessingJob.JobState.FAILED
    assert scan.state == ScanJob.State.PAUSED
    assert ReviewTask.objects.count() == 1


@pytest.mark.parametrize("kind", sorted(worker_failures.SUPPORTED))
def test_rq_timeout_callback_defers_review_until_no_retries_left(kind: str) -> None:
    owner = get_or_create_profile("timeout-owner").user
    job = ProcessingJob.objects.create(
        job_type=kind,
        state=ProcessingJob.JobState.RUNNING,
        details={"requested_by_user_id": owner.pk},
    )
    rq_job = SimpleNamespace(args=[str(job.job_id)], retries_left=1)
    worker_failures.rq_failure_callback(rq_job, None, RuntimeError, "worker timeout", None)
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.QUEUED
    assert not ReviewTask.objects.exists()
    rq_job.retries_left = 0
    worker_failures.rq_failure_callback(rq_job, None, RuntimeError, "worker timeout", None)
    worker_failures.rq_failure_callback(rq_job, None, RuntimeError, "duplicate callback", None)
    assert ReviewTask.objects.filter(processing_job=job).count() == 1


def test_parent_rq_retries_do_not_hide_child_dispatch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = ProcessingJob.objects.create(job_type=ProcessingJob.JobType.OCR)
    monkeypatch.setattr(
        worker_failures,
        "get_current_job",
        lambda: SimpleNamespace(args=[str(uuid4())], retries_left=2),
    )
    ocr._fail_ocr_job(job, "dispatch failed")
    assert ReviewTask.objects.filter(processing_job=job).exists()


def test_queue_adapter_installs_failure_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class Queue:
        def enqueue(self, function: Any, *args: Any, **kwargs: Any) -> None:
            captured.update(kwargs)

    monkeypatch.setattr(jobs, "scheduler_active", lambda _: False)
    jobs.ConfiguredQueue(cast(Any, Queue())).enqueue(
        conversion.process_conversion_job, str(uuid4())
    )
    assert captured["on_failure"] is worker_failures.rq_failure_callback


def test_owner_admin_filter_and_recovery_ui_permissions() -> None:
    job = failed_job()
    task = ReviewTask.objects.get(processing_job=job)
    owner = get_or_create_profile("failure-owner")
    other = get_or_create_profile("failure-other")
    admin = get_or_create_profile("failure-admin")
    admin.role = "admin"
    admin.save(update_fields=["role"])
    source = ImportJob.objects.create(
        uploader=owner.user,
        original_filename="ordinary.epub",
        source_path="unused",
        source_type=ImportJob.SourceType.UPLOAD,
    )
    ReviewTask.objects.create(import_job=source, source_filename="ordinary.epub")
    client = Client()
    client.force_login(owner.user)
    queued = client.get("/reviews/?task_type=worker_failure").json()
    assert [item["id"] for item in queued["items"]] == [str(task.pk)]
    assert queued["items"][0]["source_type"] == "processing"
    page = client.get(f"/reviews/{task.pk}/?view=html")
    assert page.status_code == 200 and b"Retry job" in page.content
    assert b"Acknowledge failure" in page.content and b"source unavailable" in page.content
    # Metadata mutations are not a substitute for the recovery workflow.
    assert client.post(f"/reviews/{task.pk}/unidentified/").status_code == 403
    client.force_login(other.user)
    assert not client.get("/reviews/?task_type=worker_failure").json()["items"]
    assert client.get(f"/reviews/{task.pk}/").status_code == 403
    action = f"/reviews/{task.pk}/worker-recovery/"
    assert client.post(action, {"action": "acknowledge"}).status_code == 403
    client.force_login(admin.user)
    assert client.get(f"/reviews/{task.pk}/").status_code == 200
    assert client.get(action).status_code == 405
    assert (
        client.post(
            action, {"action": "acknowledge", "expected_job_id": str(job.job_id)}
        ).status_code
        == 200
    )
    assert (
        client.post(
            action, {"action": "acknowledge", "expected_job_id": str(job.job_id)}
        ).status_code
        == 200
    )
    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED


def test_claimed_task_recovery_is_not_silently_taken_over() -> None:
    job = failed_job()
    task = ReviewTask.objects.get(processing_job=job)
    task.state = ReviewTask.State.CLAIMED
    task.claimed_by = get_or_create_profile("other-claimant").user
    task.save()
    with pytest.raises(worker_failures.RecoveryError, match="claimed"):
        worker_failures.recover(task.pk, user=task.uploader, action="retry")


def test_repeated_retry_post_cannot_start_another_attempt_after_a_fast_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = failed_job()
    task = ReviewTask.objects.get(processing_job=root)
    # A source-less conversion fails immediately; even then a repeated POST of the
    # displayed root attempt must not create a third job.
    monkeypatch.setattr(conversion, "get_queue", lambda: None)
    client = Client()
    assert task.uploader is not None
    client.force_login(task.uploader)
    payload = {"action": "retry", "expected_job_id": str(root.job_id)}
    url = f"/reviews/{task.pk}/worker-recovery/"
    assert client.post(url, payload).status_code == 200
    assert task.worker_attempts.count() == 2
    assert client.post(url, payload).status_code == 409
    assert task.worker_attempts.count() == 2


def test_worker_kill_and_operator_stop_are_recoverable() -> None:
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.CONVERSION, state=ProcessingJob.JobState.RUNNING
    )
    rq_job = SimpleNamespace(args=[str(job.job_id)], retries_left=0)
    worker_failures.work_horse_killed(rq_job, 123, 9, None)
    assert ReviewTask.objects.filter(processing_job=job).count() == 1
    stopped = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.OCR, state=ProcessingJob.JobState.RUNNING
    )
    rq_job = SimpleNamespace(args=[str(stopped.job_id)], retries_left=5)
    worker_failures.rq_stopped_callback(rq_job, None)
    stopped.refresh_from_db()
    assert stopped.state == ProcessingJob.JobState.FAILED
    assert ReviewTask.objects.filter(processing_job=stopped).count() == 1


def test_normal_rq_worker_keeps_configurable_kill_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    from rq import Worker

    from ebook_server.recovery_worker import RecoveryWorker

    captured: dict[str, Any] = {}

    def initialize(self: Any, *args: Any, **kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(Worker, "__init__", initialize)
    RecoveryWorker([])
    assert captured["work_horse_killed_handler"] is worker_failures.work_horse_killed


def test_historical_reconciliation_is_bounded_and_preserves_acknowledgments() -> None:
    from io import StringIO

    from django.core.management import call_command

    jobs = [
        ProcessingJob.objects.create(job_type=ProcessingJob.JobType.CONVERSION) for _ in range(2)
    ]
    ProcessingJob.objects.filter(pk__in=[job.pk for job in jobs]).update(
        state=ProcessingJob.JobState.FAILED, finished_at=timezone.now()
    )
    call_command("reconcile_worker_failures", limit=1, stdout=StringIO())
    assert ReviewTask.objects.count() == 0
    call_command("reconcile_worker_failures", apply=True, limit=1, stdout=StringIO())
    task = ReviewTask.objects.get(processing_job=jobs[0])
    task.state = ReviewTask.State.RESOLVED
    task.decision = ReviewTask.Decision.ACKNOWLEDGE_FAILURE
    task.save()
    call_command("reconcile_worker_failures", apply=True, limit=1, stdout=StringIO())
    call_command("reconcile_worker_failures", apply=True, limit=1, stdout=StringIO())
    assert ReviewTask.objects.count() == 2
    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.ACKNOWLEDGE_FAILURE
    assert ProcessingJob.objects.count() == 2


def test_recovery_schema_rollback_is_explicitly_irreversible() -> None:
    from importlib import import_module

    migration = import_module("ebook_server.core.migrations.0048_worker_failure_reviews")
    assert not migration.Migration.operations[-1].reversible
