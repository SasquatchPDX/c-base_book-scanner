"""Serving and scheduling of ScanTailor-Web artifacts (#195 step 2b)."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import Any

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext

from ebook_server.core import scantailor, views
from ebook_server.core.models import ScanPage, ScanTailorStageState
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page


def _page(client: Any, job_id: str, sequence: int = 1) -> ScanPage:
    _upload_scan_page(
        client,
        job_id,
        sequence=sequence,
        side="single",
        payload=f"page-{sequence}".encode(),
        filename=f"p{sequence}.png",
    )
    return ScanPage.objects.get(scan_job_id=job_id, logical_sequence_number=sequence)


def _run(client: Any, job_id: str, page: ScanPage, stage: str, parameters: dict[str, Any]) -> Any:
    response = client.post(
        f"/scan/jobs/{job_id}/pages/{page.id}/stages/{stage}/",
        data=json.dumps({"parameters": parameters}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content
    return response


@pytest.mark.django_db(transaction=True)
def test_invalidation_is_one_update_and_bumps_updated_at() -> None:
    client, job = _create_scan_job("serving-invalidate")
    page = _page(client, str(job.id))
    client.get(f"/scan/jobs/{job.id}/pages/{page.id}/stages/")
    before = {s.stage: s.updated_at for s in ScanTailorStageState.objects.filter(scan_page=page)}
    with CaptureQueriesContext(connection) as queries:
        count = scantailor.invalidate_downstream_states(page, from_stage="deskew", reason="x")
    assert count == 4
    writes = [q["sql"] for q in queries if q["sql"].startswith("UPDATE")]
    assert len(writes) == 1
    after = {s.stage: s for s in ScanTailorStageState.objects.filter(scan_page=page)}
    for stage in ("select_content", "dewarping", "margins", "output"):
        assert after[stage].status == ScanTailorStageState.StageStatus.INVALIDATED
        assert after[stage].updated_at > before[stage]
    assert after["deskew"].updated_at == before["deskew"]


@pytest.mark.django_db(transaction=True)
def test_preview_with_current_token_is_immutable_and_others_revalidate() -> None:
    client, job = _create_scan_job("serving-preview")
    page = _page(client, str(job.id))
    client.get(f"/scan/jobs/{job.id}/pages/{page.id}/stages/")  # every stage row, as the editor
    _run(client, str(job.id), page, "deskew", {"manual_angle": 1})
    state = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    token = f"{state.revision}-{state.output_sha256[:16]}"
    url = f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/preview/"

    current = client.get(f"{url}?v={token}")
    assert current.status_code == 200
    assert current["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    assert current["ETag"] == f'"{token}"'
    assert current["Content-Type"] == "image/jpeg"

    for stale_url in (url, f"{url}?v=1-0000000000000000"):
        stale = client.get(stale_url)
        assert stale.status_code == 200
        assert stale["Cache-Control"] == views.SCAN_ARTIFACT_REVALIDATE
    assert client.get(url, HTTP_IF_NONE_MATCH=f'"{token}"').status_code == 304

    # The first stage's input is the page's working image, tokenised by version.
    version = page.active_version()
    assert version is not None
    first_input = client.get(f"{url}?input=true&v=page-{version.id}")
    assert first_input["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    # The next stage's input preview is this stage's preview, with its token.
    content_input = client.get(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/select_content/preview/?input=true&v={token}"
    )
    assert content_input["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_SCAN_ARTIFACT_ACCEL_PREFIX="/_protected/scan-artifacts/")
def test_accel_prefix_hands_previews_and_thumbnails_to_nginx() -> None:
    client, job = _create_scan_job("serving-accel")
    page = _page(client, str(job.id))
    _run(client, str(job.id), page, "deskew", {"manual_angle": 1})
    state = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    preview = client.get(f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/preview/")
    assert preview["X-Accel-Redirect"] == (
        f"/_protected/scan-artifacts/{state.preview_storage_identifier}"
    )
    assert preview.content == b""
    thumbnail = client.get(f"/scan/pages/{page.id}/image/")
    assert thumbnail["X-Accel-Redirect"].startswith("/_protected/scan-artifacts/thumbnails/")
    assert thumbnail["Content-Type"] == "image/jpeg"


@pytest.mark.django_db(transaction=True)
def test_workspace_thumbnail_urls_are_tokenised_and_change_with_the_artifact() -> None:
    client, job = _create_scan_job("serving-thumbnail")
    page = _page(client, str(job.id))

    def thumbnail_url() -> str:
        workspace = client.get(f"/scan/jobs/{job.id}/workspace/").json()
        return str(workspace["pages"][0]["thumbnail"])

    raw_url = thumbnail_url()
    assert "?v=v" in raw_url
    response = client.get(raw_url)
    assert response["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    _run(client, str(job.id), page, "deskew", {"manual_angle": 90})
    stage_url = thumbnail_url()
    assert stage_url != raw_url and "?v=s" in stage_url
    assert client.get(stage_url)["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    # The old URL still answers, but no longer as an immutable copy.
    assert client.get(raw_url)["Cache-Control"] == views.SCAN_ARTIFACT_REVALIDATE


@pytest.mark.django_db(transaction=True)
def test_state_endpoint_returns_only_changed_rows_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(views, "WORKSPACE_STATE_OVERLAP", timedelta(0))
    client, job = _create_scan_job("serving-state")
    pages = [_page(client, str(job.id), sequence) for sequence in (1, 2, 3)]
    client.get(f"/scan/jobs/{job.id}/workspace/")  # creates the stage rows
    url = f"/scan/jobs/{job.id}/workspace/state/"

    full = client.get(url).json()
    assert len(full["stages"]) == 3 * len(scantailor.stage_order())
    cursor = full["cursor"]
    with CaptureQueriesContext(connection) as queries:
        idle = client.get(url, {"since": cursor})
    assert idle.status_code == 200 and idle.json()["stages"] == []
    assert not any(q["sql"].startswith(("INSERT", "UPDATE", "DELETE")) for q in queries)
    stage_reads = [q for q in queries if "core_scantailorstagestate" in q["sql"]]
    assert len(stage_reads) == 1

    _run(client, str(job.id), pages[1], "margins", {"margin": 4})
    changed = client.get(url, {"since": cursor}).json()["stages"]
    assert {row["scan_page_id"] for row in changed} == {str(pages[1].id)}
    assert {row["stage"] for row in changed} >= {"margins", "output"}

    assert client.get(url, {"since": "yesterday"}).status_code == 400
    assert client.get(url, {"since": "2026-11-18T10:00:00"}).status_code == 400
    # The cursor is ISO 8601 with an offset, so it round-trips as ``since``.
    assert "+" in cursor or cursor.endswith("Z")


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def enqueue(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.calls.append((func.__name__, args, kwargs))


@pytest.mark.django_db(transaction=True)
def test_single_edits_jump_the_queue_and_batches_are_chunked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job = _create_scan_job("serving-queue")
    for sequence in range(1, 21):
        _page(client, str(job.id), sequence)
    queue = _RecordingQueue()
    monkeypatch.setattr(scantailor, "get_queue", lambda name=None: queue)

    response = client.post(
        f"/scan/jobs/{job.id}/stages/deskew/batch/",
        data=json.dumps({"parameters": {"manual_angle": 0.5}, "preserve_exceptions": False}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content
    assert response.json()["applied_count"] == 20
    assert [name for name, _, _ in queue.calls] == ["process_stage_jobs"] * 3
    assert [len(args[0]) for _, args, _ in queue.calls] == [8, 8, 4]
    assert all(not kwargs.get("at_front") for _, _, kwargs in queue.calls)

    queue.calls.clear()
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=3)
    _run(client, str(job.id), page, "deskew", {"manual_angle": 1})
    assert [(name, kwargs.get("at_front")) for name, _, kwargs in queue.calls] == [
        ("process_stage_job", True)
    ]
