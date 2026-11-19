"""Serving and scheduling of ScanTailor-Web artifacts (#195 step 2b)."""

from __future__ import annotations

import io
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db import connection
from django.test import override_settings
from django.test.utils import CaptureQueriesContext
from PIL import Image

from ebook_server.core import scan_thumbnails, scantailor, views
from ebook_server.core.models import (
    ScanPage,
    ScanPageImage,
    ScanTailorStageState,
    StorageLocation,
)
from ebook_server.core.storage_registry import backend_for_location
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
    # The editor gets this token from the workspace payload; it names the generator.
    workspace = client.get(f"/scan/jobs/{job.id}/workspace/").json()
    work_token = workspace["pages"][0]["work_image_token"]
    assert work_token.startswith(f"page-{version.id}-w")
    first_input = client.get(f"{url}?input=true&v={work_token}")
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
    _run(client, str(job.id), page, "deskew", {"manual_angle": 45})
    stage_url = thumbnail_url()
    assert stage_url != raw_url and "?v=s" in stage_url
    assert client.get(stage_url)["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    # The old URL still answers, but no longer as an immutable copy.
    assert client.get(raw_url)["Cache-Control"] == views.SCAN_ARTIFACT_REVALIDATE


def _landscape_png() -> bytes:
    image = Image.new("RGB", (48, 24), (255, 255, 255))
    for x in range(48):
        for y in range(24):
            image.putpixel((x, y), (x * 5 % 256, y * 10 % 256, 128))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _reference_thumbnail(
    tmp_path: Path, name: str, location: StorageLocation | None, identifier: str
) -> bytes:
    storage = (
        backend_for_location(location) if location is not None else views._default_scan_storage()
    )
    target = tmp_path / f"{name}.jpg"
    scan_thumbnails.render_jpeg_thumbnail(
        storage, identifier, target, views.SCAN_THUMBNAIL_MAX_EDGE
    )
    return target.read_bytes()


def _body(response: Any) -> bytes:
    return b"".join(response.streaming_content)


@pytest.mark.django_db(transaction=True)
def test_thumbnail_bytes_and_token_come_from_one_selection_under_invalidation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Review of #198: invalidating Deskew mid-request must not mislabel the bytes."""
    client, job = _create_scan_job("serving-thumbnail-race")
    _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=_landscape_png(), filename="p.png"
    )
    page = ScanPage.objects.get(scan_job_id=job.id, logical_sequence_number=1)
    _run(client, str(job.id), page, "deskew", {"manual_angle": 45})
    deskew = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    version = page.active_version()
    assert version is not None
    page_image = ScanPageImage.objects.get(version=version)
    stage_token = f"s{deskew.id}-{deskew.revision}-{deskew.output_sha256[:16]}-p"
    work_token = f"v{version.id}-{version.checksum_sha256[:16]}-w{page_image.generator_version}"
    stage_bytes = _reference_thumbnail(
        tmp_path, "stage", deskew.preview_storage_location, deskew.preview_storage_identifier
    )
    work_bytes = _reference_thumbnail(
        tmp_path, "work", page_image.storage_location, page_image.work_storage_identifier
    )
    assert stage_bytes != work_bytes
    expected = {stage_token: stage_bytes, work_token: work_bytes}

    workspace = client.get(f"/scan/jobs/{job.id}/workspace/").json()
    assert workspace["pages"][0]["thumbnail"].endswith(f"?v={stage_token}")

    # The invalidation lands right after the view first read the stage rows (the
    # first walk over the stage order), i.e. while it is still answering.
    stage_order = scantailor.stage_order
    calls = 0

    def invalidating_stage_order() -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:
            ScanTailorStageState.objects.filter(scan_page=page).update(
                status=ScanTailorStageState.StageStatus.INVALIDATED
            )
        return stage_order()

    monkeypatch.setattr(scantailor, "stage_order", invalidating_stage_order)
    url = f"/scan/pages/{page.id}/image/"
    raced = client.get(f"{url}?v={stage_token}")
    monkeypatch.setattr(scantailor, "stage_order", stage_order)
    assert calls >= 1
    assert raced.status_code == 200
    raced_token = raced["ETag"].strip('"')
    assert raced_token in expected
    assert _body(raced) == expected[raced_token]
    if raced["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE:
        assert raced_token == stage_token

    # Afterwards the page shows its working image; the old URL still answers, but
    # with the new bytes it must neither claim the old token nor be immutable.
    stale = client.get(f"{url}?v={stage_token}")
    assert stale["ETag"] == f'"{work_token}"'
    assert stale["Cache-Control"] == views.SCAN_ARTIFACT_REVALIDATE
    assert _body(stale) == work_bytes
    current = client.get(f"{url}?v={work_token}")
    assert current["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    assert _body(current) == work_bytes
    workspace = client.get(f"/scan/jobs/{job.id}/workspace/").json()
    assert workspace["pages"][0]["thumbnail"].endswith(f"?v={work_token}")


@pytest.mark.django_db(transaction=True)
def test_thumbnail_token_distinguishes_raw_scan_from_working_image(tmp_path: Path) -> None:
    """The raw-scan and working-image thumbnails differ, so their tokens must too."""
    client, job = _create_scan_job("serving-thumbnail-raw")
    _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=_landscape_png(), filename="p.png"
    )
    page = ScanPage.objects.get(scan_job_id=job.id, logical_sequence_number=1)
    version = page.active_version()
    assert version is not None
    with_image = client.get(f"/scan/pages/{page.id}/image/")
    ScanPageImage.objects.filter(version=version).delete()
    raw = client.get(f"/scan/pages/{page.id}/image/")
    raw_token = f"v{version.id}-{version.checksum_sha256[:16]}-r"
    assert raw["ETag"] == f'"{raw_token}"'
    assert with_image["ETag"] != raw["ETag"]
    assert _body(raw) == _reference_thumbnail(
        tmp_path, "raw", version.storage_location, version.storage_identifier
    )
    workspace = client.get(f"/scan/jobs/{job.id}/workspace/").json()
    assert workspace["pages"][0]["thumbnail"].endswith(f"?v={raw_token}")


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
