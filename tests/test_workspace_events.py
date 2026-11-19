"""Incremental workspace events preserve state while avoiding idle JSON/model reads."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from datetime import timedelta
from typing import Any, cast
from unittest.mock import Mock

import pytest
from asgiref.sync import async_to_sync, sync_to_async
from django.db import connection
from django.http import StreamingHttpResponse
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from ebook_server.core import scantailor, views
from ebook_server.core.models import ScanJob, ScanPage, ScanTailorStageState, get_or_create_profile
from ebook_server.core.workspace_events import WorkspaceEventPoller

pytestmark = pytest.mark.django_db(transaction=True)


STAGES_PER_PAGE = len(scantailor.stage_order())


def workspace(pages: int = 2) -> tuple[ScanJob, list[ScanTailorStageState]]:
    job = ScanJob.objects.create()
    page_rows = ScanPage.objects.bulk_create(
        [ScanPage(scan_job=job, logical_sequence_number=index + 1) for index in range(pages)]
    )
    states = ScanTailorStageState.objects.bulk_create(
        [
            ScanTailorStageState(scan_job=job, scan_page=page, stage=stage, parameters={"k": 0.33})
            for page in page_rows
            for stage in scantailor.stage_order()
        ]
    )
    return job, states


def test_idle_300_page_workspace_reads_tokens_without_deserializing_stage_json() -> None:
    job, _ = workspace(300)
    serialize = Mock(side_effect=views._serialize_stage_state)
    poller = WorkspaceEventPoller(str(job.pk), serialize)
    initial = poller.poll()
    assert initial.exists and initial.payload is not None
    assert len(initial.payload["stages"]) == 300 * STAGES_PER_PAGE
    serialize.reset_mock()
    with CaptureQueriesContext(connection) as queries:
        idle = poller.poll()
    assert idle.exists and idle.payload is None
    assert len(queries) == (1 if connection.vendor == "postgresql" else 2)
    assert all('"parameters"' not in query["sql"] for query in queries)
    assert all('"output_storage_identifier"' not in query["sql"] for query in queries)
    serialize.assert_not_called()


def test_changed_tick_only_fetches_and_serializes_the_changed_stage() -> None:
    job, states = workspace()
    serialize = Mock(side_effect=views._serialize_stage_state)
    poller = WorkspaceEventPoller(str(job.pk), serialize)
    poller.poll()
    serialize.reset_mock()
    stage = states[0]
    stage.parameters = {"manual_angle": 2.5}
    stage.revision += 1
    stage.save(update_fields=["parameters", "revision", "updated_at"])
    update = poller.poll()
    assert update.payload is not None
    assert [row["id"] for row in update.payload["stages"]] == [stage.pk]
    assert update.payload["stages"][0]["parameters"] == {"manual_angle": 2.5}
    assert serialize.call_count == 1
    assert poller.poll().payload is None


def test_worker_status_and_late_older_timestamp_changes_are_not_hidden_by_a_max() -> None:
    job, states = workspace()
    poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
    poller.poll()
    now = timezone.now()
    ScanTailorStageState.objects.filter(pk=states[1].pk).update(updated_at=now)
    poller.poll()
    # A transaction may commit after the last poll with a timestamp older than another row's.
    ScanTailorStageState.objects.filter(pk=states[0].pk).update(
        status=ScanTailorStageState.StageStatus.RUNNING, updated_at=now - timedelta(seconds=5)
    )
    update = poller.poll()
    assert update.payload is not None
    assert [(row["id"], row["status"]) for row in update.payload["stages"]] == [
        (states[0].pk, ScanTailorStageState.StageStatus.RUNNING)
    ]


def test_changes_during_payload_fetch_are_seen_again_on_the_next_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, states = workspace()
    poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
    poller.poll()
    ScanTailorStageState.objects.filter(pk=states[0].pk).update(revision=2)
    original = poller.serialize_stage

    def update_during_serialization(state: ScanTailorStageState) -> dict[str, Any]:
        ScanTailorStageState.objects.filter(pk=state.pk).update(revision=3)
        return original(state)

    monkeypatch.setattr(poller, "serialize_stage", update_during_serialization)
    assert poller.poll().payload is not None
    monkeypatch.setattr(poller, "serialize_stage", original)
    following = poller.poll()
    assert following.payload is not None
    assert following.payload["stages"][0]["revision"] == 3


def test_creation_deletion_job_state_reconnect_and_job_isolation() -> None:
    job, states = workspace()
    other, other_states = workspace()
    poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
    poller.poll()
    ScanTailorStageState.objects.filter(pk=other_states[0].pk).update(revision=5)
    assert poller.poll().payload is None
    states[0].delete()
    deleted = poller.poll()
    assert deleted.exists and deleted.payload is not None and deleted.payload["stages"] == []
    new = ScanTailorStageState.objects.create(
        scan_job=job, scan_page=states[1].scan_page, stage=states[0].stage
    )
    created = poller.poll()
    assert created.payload is not None
    assert [row["id"] for row in created.payload["stages"]] == [new.pk]
    job.state = ScanJob.State.PAUSED
    job.save(update_fields=["state", "updated_at"])
    paused = poller.poll()
    assert paused.payload is not None and paused.payload["state"] == ScanJob.State.PAUSED
    reconnected = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state).poll()
    assert reconnected.payload is not None
    assert len(reconnected.payload["stages"]) == 2 * STAGES_PER_PAGE
    job.delete()
    closed = poller.poll()
    assert not closed.exists and closed.payload is None
    assert ScanJob.objects.filter(pk=other.pk).exists()


def test_postgres_fingerprint_catches_up_when_the_row_vector_is_already_current() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL fingerprint optimization")
    job, _ = workspace()
    poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
    poller.poll()
    # Fingerprint was read before a concurrent update, but the payload saw its new values.
    poller._fingerprint = "older snapshot"
    assert poller.poll().payload is None
    with CaptureQueriesContext(connection) as queries:
        assert poller.poll().payload is None
    assert len(queries) == 1


def test_stage_get_only_marks_real_input_binding_changes() -> None:
    job, states = workspace()
    client = Client()
    client.force_login(get_or_create_profile("input-binding-viewer").user)
    url = f"/scan/jobs/{job.pk}/pages/{states[1].scan_page_id}/stages/select_content/"
    assert client.get(url).status_code == 200
    poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
    poller.poll()
    with CaptureQueriesContext(connection) as queries:
        assert client.get(url).status_code == 200
    assert not any(
        query["sql"].startswith("UPDATE") and "core_scantailorstagestate" in query["sql"]
        for query in queries
    )
    assert poller.poll().payload is None
    predecessor = states[0]
    predecessor.output_storage_identifier = "new-input.png"
    predecessor.save(update_fields=["output_storage_identifier", "updated_at"])
    assert client.get(url).status_code == 200
    update = poller.poll()
    assert update.payload is not None
    content = next(row for row in update.payload["stages"] if row["id"] == states[1].pk)
    assert content["stage"] == "select_content"
    assert content["input_storage_identifier"] == "new-input.png"


@pytest.mark.parametrize("viewers", [1, 5])
@override_settings(
    SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS=0.1, SCANTAILOR_EVENT_STREAM_MAX_EVENTS=20
)
def test_connected_streams_start_immediately_idle_then_deliver_changes(
    viewers: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job, states = workspace(300)
    serialize = Mock(side_effect=views._serialize_stage_state)
    monkeypatch.setattr(views, "_serialize_stage_state", serialize)
    responses = []
    for index in range(viewers):
        client = Client()
        client.force_login(get_or_create_profile(f"workspace-viewer-{index}").user)
        response = client.get(f"/scan/jobs/{job.pk}/workspace/events/")
        assert response.status_code == 200
        responses.append(cast(StreamingHttpResponse, response))

    async def exercise_streams() -> None:
        streams = [
            cast(AsyncGenerator[bytes], response.streaming_content) for response in responses
        ]
        initial = await asyncio.gather(*(anext(stream) for stream in streams))
        for frame in initial:
            payload = json.loads(frame.decode().split("data: ", 1)[1])
            assert len(payload["stages"]) == 300 * STAGES_PER_PAGE
        serialize.reset_mock()
        # Fifteen unchanged polls yield a heartbeat without fetching/serializing models.
        idle = await asyncio.gather(*(anext(stream) for stream in streams))
        assert all(frame == b": keep-alive\n\n" for frame in idle)
        serialize.assert_not_called()
        await sync_to_async(ScanTailorStageState.objects.filter(pk=states[0].pk).update)(
            revision=2, updated_at=timezone.now()
        )
        changed = await asyncio.gather(*(anext(stream) for stream in streams))
        for frame in changed:
            payload = json.loads(frame.decode().split("data: ", 1)[1])
            assert [(row["id"], row["revision"]) for row in payload["stages"]] == [
                (states[0].pk, 2)
            ]
        assert serialize.call_count == viewers
        for stream in streams:
            await stream.aclose()

    async_to_sync(exercise_streams)()
