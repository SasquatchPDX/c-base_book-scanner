"""Commit-order correctness of the workspace event cursor on PostgreSQL."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event

import pytest
from django.db import close_old_connections, connection, transaction
from django.utils import timezone

from ebook_server.core import views
from ebook_server.core.models import ScanTailorStageState
from ebook_server.core.workspace_events import WorkspaceEventPoller
from tests.test_workspace_events import workspace

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.getenv("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires PostgreSQL integration environment",
    ),
]


def test_late_commit_with_older_timestamp_is_delivered() -> None:
    assert connection.vendor == "postgresql"
    job, states = workspace()
    poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
    poller.poll()
    ready, commit = Event(), Event()
    older = timezone.now() - timedelta(seconds=5)

    def delayed_transaction() -> None:
        close_old_connections()
        try:
            with transaction.atomic():
                ScanTailorStageState.objects.filter(pk=states[0].pk).update(
                    status=ScanTailorStageState.StageStatus.RUNNING, updated_at=older
                )
                ready.set()
                assert commit.wait(10)
        finally:
            close_old_connections()

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(delayed_transaction)
        try:
            assert ready.wait(10)
            ScanTailorStageState.objects.filter(pk=states[1].pk).update(updated_at=timezone.now())
            earlier = poller.poll()
            assert earlier.payload is not None
            assert [row["id"] for row in earlier.payload["stages"]] == [states[1].pk]
        finally:
            commit.set()
        future.result(timeout=10)
    late = poller.poll()
    assert late.payload is not None
    assert [row["id"] for row in late.payload["stages"]] == [states[0].pk]
