"""The ScanTailor editor page and its strip thumbnails (#195 step 3)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from django.db import connection
from django.test.utils import CaptureQueriesContext
from PIL import Image

from ebook_server.core import scan_pipeline, scantailor, views
from ebook_server.core.models import Book, ScanJob, ScanPage, ScanTailorStageState, ScanTodo
from tests.test_scan_jobs import (
    _create_scan_job,
    _create_scan_job_with_book,
    _DeferredQueue,
    _upload_scan_page,
)


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
    assert (
        hints["margins"]
        == "Set the margins in millimetres; aligned pages share the size of the largest page."
    )
    assert client.get("/scan/jobs/not-a-job/editor/").status_code in {400, 404}


@pytest.mark.django_db(transaction=True)
def test_stage_thumbnail_is_written_by_the_worker_and_immutable_with_its_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job = _create_scan_job("editor-thumbnail")
    page = _page(client, str(job.id))
    # Only Deskew runs; the later stages would otherwise follow it (#205).
    monkeypatch.setattr(scantailor, "queue_ready_successor", lambda *args, **kwargs: None)
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


# --- rescan and finish in the editor (#228) ----------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_editor_rescan_request_keeps_its_reason_marks_the_page_and_blocks_finish() -> None:
    book = Book.objects.create(title="Rescan in the editor")
    client, job = _create_scan_job_with_book("editor-rescan", book_id=book.id)
    page = _page(client, str(job.id))

    # What the editor sends: JSON with the reason from its dialog, CSRF in a header.
    flagged = client.post(
        f"/scan/pages/{page.id}/rescan/",
        data=json.dumps({"reason": "Blurred lower margin"}),
        content_type="application/json",
    )
    assert flagged.status_code == 201
    todo = ScanTodo.objects.get(scan_page=page)
    assert (todo.reason, todo.state) == ("Blurred lower margin", ScanTodo.State.OPEN)
    # A second request does not create a second todo.
    again = client.post(f"/scan/pages/{page.id}/rescan/", data={"reason": "again"})
    assert again.status_code == 200
    assert ScanTodo.objects.filter(scan_page=page).count() == 1

    # A form post (no JavaScript) returns to the editor on that page.
    html_post = client.post(
        f"/scan/pages/{page.id}/rescan/", data={"reason": "x"}, HTTP_ACCEPT="text/html"
    )
    assert html_post.status_code == 302
    assert html_post["Location"] == f"/scan/jobs/{job.id}/editor/?page={page.id}"

    # The marker: page data of the editor and the live-update markers of other editors.
    # A todo the scanner claimed still waits for its replacement.
    ScanTodo.objects.filter(pk=todo.pk).update(state=ScanTodo.State.CLAIMED)
    editor = client.get(f"/scan/jobs/{job.id}/editor/")
    assert editor.context["pages"][0]["open_rescan_todos"] == 1
    state = client.get(f"/scan/jobs/{job.id}/workspace/state/").json()
    assert state["pages"] == [
        {"id": str(page.id), "open_rescan_todos": 1, "page_number_verified": None}
    ]

    # Decision (#228): finish waits until requested rescans are replaced or cancelled.
    refused = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data=json.dumps({"create_cleaned_variant": True, "allow_raw_pages": True}),
        content_type="application/json",
        HTTP_ACCEPT="application/json",
    )
    assert refused.status_code == 400
    assert refused.json()["error"] == "outstanding scan todos must be resolved"
    job.refresh_from_db()
    assert job.state != ScanJob.State.FINALIZING


@pytest.mark.django_db(transaction=True)
def test_editor_finish_explains_the_book_and_links_the_last_finalization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unlinked_client, unlinked = _create_scan_job("editor-no-book")
    html = unlinked_client.get(f"/scan/jobs/{unlinked.id}/editor/").content.decode()
    assert "No library book is linked yet: finish starts the bibliographic identification" in html

    book = Book.objects.create(title="Finished from the editor")
    client, job = _create_scan_job_with_book("editor-finish", book_id=book.id)
    _page(client, str(job.id))
    html = client.get(f"/scan/jobs/{job.id}/editor/").content.decode()
    assert "Adds the cleaned pages to the library book “Finished from the editor”." in html
    assert "data-finalization-link" not in html

    queue = _DeferredQueue()
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: queue)
    finish = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data=json.dumps({"create_cleaned_variant": True, "allow_raw_pages": True}),
        content_type="application/json",
    )
    assert finish.status_code == 202
    finalization_id = finish.json()["finalization_job"]["job_id"]

    editor = client.get(f"/scan/jobs/{job.id}/editor/")
    html = editor.content.decode()
    assert f'href="/scan-finalizations/{finalization_id}/?view=html" data-finalization-link' in html
    assert "Last finish: queued" in html
    status = client.get(f"/scan-finalizations/{finalization_id}/?view=html").content.decode()
    assert f'data-scan-editor-link href="/scan/jobs/{job.id}/editor/"' in status
    # Finalizing: nothing to edit or finish until the worker is done.
    assert editor.context["editable"] is False
    assert '<div class="st-finish" data-finish-scan hidden>' in html
