"""The ScanTailor editor page and its strip thumbnails (#195 step 3)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from PIL import Image

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
        filename=f"scan-{sequence:04d}.png",
    )
    return ScanPage.objects.get(scan_job_id=job_id, logical_sequence_number=sequence)


def _image(response: Any) -> Image.Image:
    data = b"".join(response.streaming_content)
    return Image.open(io.BytesIO(data))


@pytest.mark.django_db(transaction=True)
def test_editor_page_embeds_pages_with_file_names_and_manual_flags() -> None:
    client, job = _create_scan_job("editor-page")
    page = _page(client, str(job.id))
    client.post(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/",
        data=json.dumps({"parameters": {"manual_angle": 1.5}}),
        content_type="application/json",
    )
    response = client.get(f"/scan/jobs/{job.id}/editor/")
    assert response.status_code == 200
    pages = response.context["pages"]
    assert pages[0]["active_version"]["original_filename"] == "scan-0001.png"
    stages = {state["stage"]: state for state in pages[0]["stages"]}
    assert stages["deskew"]["manual"] is True
    assert stages["select_content"]["manual"] is False
    hints = {stage["name"]: stage["hint"] for stage in response.context["stage_navigation"]}
    assert hints["margins"] == "Resize margins by dragging any of the solid lines."
    assert client.get("/scan/jobs/not-a-job/editor/").status_code in {400, 404}


@pytest.mark.django_db(transaction=True)
def test_stage_thumbnail_is_written_by_the_worker_and_immutable_with_its_token() -> None:
    client, job = _create_scan_job("editor-thumbnail")
    page = _page(client, str(job.id))
    client.post(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/",
        data=json.dumps({"parameters": {"manual_angle": 2}}),
        content_type="application/json",
    )
    state = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    identifier = scantailor.stage_thumbnail_identifier(state)
    storage = scantailor._scan_storage()
    assert identifier.endswith("/thumbnail.jpg") and storage.exists(identifier)
    token = f"{state.revision}-{state.output_sha256[:16]}"
    url = f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/thumbnail/"
    response = client.get(f"{url}?v={token}")
    assert response.status_code == 200
    assert response["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    with _image(response) as thumb:
        assert thumb.format == "JPEG"
    # Not processed yet: no thumbnail of its own (the strip shows an earlier stage).
    not_ready = client.get(f"/scan/jobs/{job.id}/pages/{page.id}/stages/margins/thumbnail/")
    assert not_ready.status_code == 404


@pytest.mark.django_db(transaction=True)
def test_missing_stage_thumbnail_is_rebuilt_from_the_preview() -> None:
    client, job = _create_scan_job("editor-thumbnail-backfill")
    page = _page(client, str(job.id))
    client.post(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/",
        data=json.dumps({"parameters": {}}),
        content_type="application/json",
    )
    state = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    storage = scantailor._scan_storage()
    identifier = scantailor.stage_thumbnail_identifier(state)
    storage.delete(identifier)  # as for a stage published before thumbnails existed
    response = client.get(f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/thumbnail/")
    assert response.status_code == 200
    assert storage.exists(identifier)


@pytest.mark.django_db(transaction=True)
def test_small_page_thumbnail_comes_from_the_page_image() -> None:
    client, job = _create_scan_job("editor-page-thumbnail")
    page = _page(client, str(job.id))
    version = page.active_version()
    assert version is not None
    token = client.get(f"/scan/jobs/{job.id}/workspace/").json()["pages"][0]["work_image_token"]
    response = client.get(f"/scan/pages/{page.id}/thumbnail/?v={token}")
    assert response.status_code == 200
    assert response["Cache-Control"] == views.SCAN_ARTIFACT_IMMUTABLE
    assert response["Content-Type"] == "image/jpeg"


@pytest.mark.django_db(transaction=True)
def test_editor_page_query_count_does_not_grow_with_pages() -> None:
    client, job = _create_scan_job("editor-queries")
    for sequence in (1, 2):
        _page(client, str(job.id), sequence)
    client.get(f"/scan/jobs/{job.id}/editor/")  # creates the stage rows once
    with CaptureQueriesContext(connection) as small:
        assert client.get(f"/scan/jobs/{job.id}/editor/").status_code == 200
    for sequence in range(3, 9):
        _page(client, str(job.id), sequence)
    client.get(f"/scan/jobs/{job.id}/editor/")
    with CaptureQueriesContext(connection) as large:
        assert client.get(f"/scan/jobs/{job.id}/editor/").status_code == 200
    assert len(large) == len(small)
    assert not any(q["sql"].startswith(("INSERT", "UPDATE")) for q in large)
