"""Database serialization of recovery-task creation and competing retry actions."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import close_old_connections, connection
from django.utils import timezone

from ebook_server.core import worker_failures
from ebook_server.core.models import ProcessingJob, ReviewTask, get_or_create_profile
from tests.test_worker_failures import failed_job

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.getenv("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires PostgreSQL integration environment",
    ),
]


def test_two_terminal_writers_create_one_recovery_task() -> None:
    assert connection.vendor == "postgresql"
    owner = get_or_create_profile("pg-failure-owner").user
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.CONVERSION,
        state=ProcessingJob.JobState.RUNNING,
        details={"requested_by_user_id": owner.pk},
    )
    start = Barrier(2)

    def fail() -> None:
        close_old_connections()
        try:
            current = ProcessingJob.objects.get(pk=job.pk)
            start.wait(timeout=10)
            current.state = ProcessingJob.JobState.FAILED
            current.finished_at = timezone.now()
            current.save(update_fields=["state", "finished_at"])
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: fail(), range(2)))
    assert ReviewTask.objects.filter(processing_job=job).count() == 1
    job.refresh_from_db()
    assert job.recovery_task_id == ReviewTask.objects.get(processing_job=job).pk


def test_two_retry_requests_queue_one_attempt(monkeypatch: pytest.MonkeyPatch) -> None:
    assert connection.vendor == "postgresql"
    root = failed_job(owner="pg-retry-owner")
    task = ReviewTask.objects.get(processing_job=root)
    owner = task.uploader
    dispatched: list[ProcessingJob] = []
    monkeypatch.setattr(worker_failures, "dispatch_retry", dispatched.append)
    start = Barrier(2)

    def retry() -> bool:
        close_old_connections()
        try:
            start.wait(timeout=10)
            try:
                worker_failures.recover(
                    task.pk, user=owner, action="retry", expected_job_id=str(root.job_id)
                )
            except worker_failures.RecoveryError:
                return False
            return True
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: retry(), range(2)))
    assert sorted(outcomes) == [False, True]
    assert task.worker_attempts.count() == 2
    assert len(dispatched) == 1
    assert ReviewTask.objects.count() == 1
