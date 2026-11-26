from __future__ import annotations

import hashlib
import io
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart
from PIL import Image

from ebook_server.core import conversion, ocr, scan_pipeline, scantailor, views
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanJobOpf,
    ScanPage,
    ScanPageVersion,
    ScanTailorRun,
    ScanTailorStageState,
    ScanTodo,
    StorageLocation,
)
from ebook_server.jobs import RQ_ENQUEUE_OPTIONS
from ebook_server.storage import LocalFilesystemStorage
from tests.streaming_fixtures import BoundedReadStream


class _ScriptedOCRClient:
    """Small OCR client stub for synchronous scan-job workflow tests."""

    def __init__(self, poll_plan: list[dict[str, object]], outputs: dict[str, bytes]) -> None:
        self.poll_plan = poll_plan
        self.outputs = outputs
        self.poll_calls = 0
        self.submitted: list[tuple[object, object]] = []

    def submit_job(
        self,
        source_path: Path,
        source_format: str,
        source_mime: str,
        request: ocr.OCRJobRequest,
    ) -> str:
        del source_format, source_mime, request
        self.submitted.append((source_path, source_path.stat().st_size))
        return "scan-job-ocr"

    def poll_job(self, remote_job_id: str) -> ocr.OCRPollResult:
        del remote_job_id
        index = min(self.poll_calls, len(self.poll_plan) - 1)
        self.poll_calls += 1
        entry = self.poll_plan[index]
        return ocr.OCRPollResult(
            status=entry["status"],  # type: ignore[arg-type]
            artifacts=entry.get("artifacts", []),  # type: ignore[arg-type]
            error=entry.get("error"),  # type: ignore[arg-type]
        )

    def iter_artifact(
        self,
        remote_job_id: str,
        artifact: ocr.OCRArtifactDescriptor,
    ) -> Iterator[bytes]:
        del remote_job_id
        payload = self.outputs.get(artifact.format)
        if payload is None:
            raise AssertionError(f"unexpected artifact format: {artifact.format}")
        yield payload


class _InMemoryTextConverter:
    def __init__(self, payload: bytes, version: str = "stub-v1") -> None:
        self.payload = payload
        self._version = version

    name = "stub"
    binary = "stub"

    def is_available(self) -> bool:
        return True

    def version(self) -> str:
        return self._version

    def convert(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        source_format: str,
        target_format: str,
        options: dict[str, str] | None = None,
    ) -> None:
        del source_format, target_format, options
        destination_path.write_bytes(self.payload)


class _DeferredQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, tuple[Any, ...], dict[str, Any]]] = []

    def enqueue(self, func: Any, *args: Any, **kwargs: Any) -> str:
        # Like RQ, keep its own options (e.g. job_id) away from the function.
        job_kwargs = {k: v for k, v in kwargs.items() if k not in RQ_ENQUEUE_OPTIONS}
        self.calls.append((func, args, job_kwargs))
        return "deferred"

    def run_next(self) -> None:
        func, args, kwargs = self.calls.pop(0)
        func(*args, **kwargs)


def _install_ocr_client(
    monkeypatch: pytest.MonkeyPatch,
    outputs: dict[str, bytes],
    poll_plan: list[dict[str, object]],
) -> None:
    client = _ScriptedOCRClient(poll_plan, outputs)
    monkeypatch.setattr(ocr, "_get_ocr_client", lambda: client)
    monkeypatch.setattr(
        ocr,
        "_poll_schedule",
        lambda: ocr.PollSchedule(
            deadline_seconds=5.0, interval_start=0.0, interval_max=0.0, backoff=1.0
        ),
    )


def _create_stub_converter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        conversion,
        "CONVERTER_REGISTRY",
        {"stub": _InMemoryTextConverter(b"converted")},
    )
    monkeypatch.setattr(
        conversion,
        "CAPABILITIES",
        (conversion.ConversionCapability("txt", "epub", "stub"),),
    )


def _page_sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _create_scan_job(profile_user: str) -> tuple[Client, ScanJob]:
    return _create_scan_job_with_book(profile_user, book_id=None)


def _create_scan_job_with_book(
    profile_user: str,
    *,
    book_id: int | None = None,
) -> tuple[Client, ScanJob]:
    from ebook_server.core.models import get_or_create_profile

    profile = get_or_create_profile(profile_user)
    client = Client()
    client.force_login(profile.user)
    payload: dict[str, str] = {
        "provisional_title": "Test Scan",
        "scanner_id": "desk-01",
    }
    if book_id is not None:
        payload["book_id"] = str(book_id)
    response = client.post(
        "/scan/jobs/",
        data=payload,
    )
    payload = response.json()
    assert response.status_code == 201
    job = ScanJob.objects.get(pk=payload["id"])
    return client, job


def _valid_png_bytes() -> bytes:
    image = Image.new("RGB", (64, 96), (255, 255, 255))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _black_png_bytes() -> bytes:
    image = Image.new("RGB", (64, 96), (0, 0, 0))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _png_for(label: bytes) -> bytes:
    """Return a small, valid and distinct PNG derived from ``label``."""
    digest = hashlib.sha256(label).digest()
    image = Image.new("RGB", (16, 16), (digest[0], digest[1], digest[2]))
    stream = io.BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def _put_scan_page(
    client: Client,
    page_id: object,
    payload: bytes,
    *,
    filename: str = "replacement.png",
    **fields: str,
) -> Any:
    """Send a real multipart PUT, as a non-Django client would."""
    body = encode_multipart(
        BOUNDARY,
        {
            "checksum_sha256": _page_sha(payload),
            **fields,
            "file": SimpleUploadedFile(filename, payload, content_type="image/png"),
        },
    )
    return client.put(
        f"/scan/pages/{page_id}/",
        data=body,
        content_type=MULTIPART_CONTENT,
    )


def _upload_scan_page(
    client: Client,
    job_id: str,
    sequence: int,
    side: str,
    payload: bytes,
    *,
    filename: str,
    content_type: str = "image/png",
) -> Any:
    if not payload.startswith(b"\x89PNG"):
        payload = _png_for(payload)
    return client.post(
        f"/scan/jobs/{job_id}/pages/",
        data={
            "logical_sequence_number": str(sequence),
            "side": side,
            "checksum_sha256": _page_sha(payload),
            "scanner_metadata": '{"lighting":"ok"}',
            "file": SimpleUploadedFile(
                filename,
                payload,
                content_type=content_type,
            ),
        },
    )


@pytest.mark.django_db(transaction=True)
def test_scan_job_create_and_list() -> None:
    client, job = _create_scan_job("scanner-create")
    response = client.get("/scan/jobs/?state=open")
    assert response.status_code == 200

    payload = response.json()
    assert payload["count"] >= 1
    assert any(item["id"] == str(job.id) for item in payload["items"])
    assert "capture_layout" not in payload["items"][0]


@pytest.mark.django_db(transaction=True)
def test_scan_job_html_list_counts_pages_without_serializing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job = _create_scan_job("scanner-list-html")
    pages = [
        ScanPage.objects.create(scan_job=job, logical_sequence_number=number)
        for number in (1, 2, 3)
    ]
    for page in pages[:2]:
        ScanTodo.objects.create(scan_job=job, scan_page=page, todo_type=ScanTodo.TodoType.RESCAN)
    ScanTailorRun.objects.create(scan_job=job, page_ids=[str(page.pk) for page in pages])
    for version, app_generated, title in [
        (1, False, "Scanner OPF Title"),
        (2, True, "Generated OPF Title"),
    ]:
        ScanJobOpf.objects.create(
            scan_job=job,
            version=version,
            app_generated=app_generated,
            content=b"",
            sha256=str(version) * 64,
            size_bytes=0,
            fields={
                "title": title,
                "authors": ["Scanner Author"],
                "isbn": "9780306406157",
                "language": "en",
            },
        )

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("HTML scan job list performed per-page serialization")

    monkeypatch.setattr(views, "_scan_job_list_item", unexpected)
    monkeypatch.setattr(scan_pipeline, "pipeline_progress", unexpected)
    response = client.get("/scan/jobs/?view=html&with_open_todos=true")
    assert response.status_code == 200
    assert b"3 pages" in response.content
    assert b"Scanner OPF Title" in response.content
    assert b"Generated OPF Title" not in response.content
    assert b"Scanner Author" in response.content
    assert b"ISBN 9780306406157" in response.content
    assert b"ScanTailor queued" in response.content
    assert b"Rescan requested" in response.content
    assert f"/scan/jobs/{job.pk}/editor/".encode() in response.content


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("with_open_todos", [False, True])
def test_scan_job_json_list_query_count_does_not_grow_with_pages(
    monkeypatch: pytest.MonkeyPatch, with_open_todos: bool
) -> None:
    """#226: the JSON list uses its prefetches and never checks page files on disk."""
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    client, job = _create_scan_job("scanner-list-json")
    location, _ = StorageLocation.objects.get_or_create(
        identifier="list-test", defaults={"base_path": "list-test"}
    )

    def add_pages(first: int, count: int) -> None:
        for number in range(first, first + count):
            page = ScanPage.objects.create(scan_job=job, logical_sequence_number=number)
            for version in (1, 2):
                ScanPageVersion.objects.create(
                    scan_page=page,
                    version=version,
                    is_active=version == 2,
                    storage_location=location,
                    storage_identifier=f"p{number}-v{version}",
                    checksum_sha256=f"{number:032x}{version:032x}",
                    mime_type="image/png",
                    size_bytes=1,
                )
            ScanTodo.objects.create(
                scan_job=job, scan_page=page, todo_type=ScanTodo.TodoType.RESCAN
            )

    def unexpected(*args: Any, **kwargs: Any) -> None:
        pytest.fail("JSON scan job list checked page files")

    monkeypatch.setattr(scan_pipeline, "pipeline_progress", unexpected)
    url = "/scan/jobs/?with_open_todos=true" if with_open_todos else "/scan/jobs/"

    def list_queries() -> tuple[int, dict[str, Any]]:
        with CaptureQueriesContext(connection) as queries:
            response = client.get(url, HTTP_ACCEPT="application/json")
        assert response.status_code == 200
        return len(queries), response.json()

    add_pages(1, 2)
    small, payload = list_queries()
    add_pages(3, 8)
    large, payload = list_queries()
    assert large == small

    item = next(item for item in payload["items"] if item["id"] == str(job.pk))
    assert "pipeline" not in item
    assert [page["logical_sequence_number"] for page in item["pages"]] == list(range(1, 11))
    first = item["pages"][0]
    assert first["active_version"]["version"] == 2
    assert [version["version"] for version in first["available_versions"]] == [2, 1]
    assert first["open_rescan_todos"] == 1
    assert first["thumbnail"] == f"/scan/pages/{first['id']}/image/"
    if with_open_todos:
        assert item["open_rescan_todo_count"] == 10


@pytest.mark.django_db(transaction=True)
def test_blank_page_is_audited_preserved_in_pdf_and_reset_on_replacement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pypdf import PdfReader

    from ebook_server.core.storage_registry import backend_for_location

    book = Book.objects.create(title="A book with a blank verso")
    client, job = _create_scan_job_with_book("blank-page-reviewer", book_id=book.id)
    monkeypatch.setattr(scantailor, "get_queue", lambda: None)
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    base_url = f"/scan/jobs/{job.pk}"

    for sequence, width in ((1, 64), (2, 120)):
        data = io.BytesIO()
        Image.new("L", (width, 96), 140).save(data, format="PNG", dpi=(300, 300))
        upload = _upload_scan_page(
            client, str(job.pk), sequence, "single", data.getvalue(), filename=f"{sequence}.png"
        )
        assert upload.status_code == 201
        normal_page = job.pages.get(logical_sequence_number=sequence)
        result = client.post(
            f"{base_url}/pages/{normal_page.pk}/stages/deskew/",
            {"parameters": {"deskew_mode": "manual", "angle_degrees": 0}},
            content_type="application/json",
        )
        assert result.status_code in {200, 202}
        result = client.post(
            f"{base_url}/pages/{normal_page.pk}/stages/select_content/",
            {"parameters": {"content_mode": "disabled"}},
            content_type="application/json",
        )
        assert result.status_code in {200, 202}

    page = job.pages.get(logical_sequence_number=2)
    version = page.active_version()
    assert version is not None
    page_url = f"{base_url}/pages/{page.pk}"
    revisions = dict(page.stage_states.values_list("stage", "revision"))
    payload = {"declared_blank": True, "version_id": str(version.pk), "revisions": revisions}
    todo = client.post(f"/scan/pages/{page.pk}/rescan/", data={})
    assert todo.status_code == 201
    previous_width = scantailor.margins_aggregate(job).width_mm

    response = client.post(f"{page_url}/blank/", payload, content_type="application/json")
    assert response.status_code == 200, response.content
    skipped = [row for row in response.json()["stages"] if row["status"] == "skipped"]
    assert {row["stage"] for row in skipped} == {"deskew", "select_content", "dewarping", "output"}
    assert all(row["skip_reason"] == "declared_blank" for row in skipped)
    assert page.todos.get().state == ScanTodo.State.CANCELLED
    assert client.post(f"/scan/pages/{page.pk}/rescan/").status_code == 400
    event = page.disposition_events.get()
    assert event.actor_id == job.operator_id
    assert (event.previous_disposition, event.disposition) == ("normal", "blank")
    assert event.reason == "declared" and event.version_id == version.pk
    page.refresh_from_db()
    assert page.page_disposition == ScanPage.Disposition.BLANK
    assert response.json()["page_disposition"] == "blank"
    assert (
        client.post(f"{page_url}/blank/", payload, content_type="application/json").status_code
        == 409
    )

    assert scantailor.margins_aggregate(job).width_mm < previous_width
    first_margins = job.pages.get(logical_sequence_number=1).stage_states.get(stage="margins")
    margins = page.stage_states.get(stage="margins")
    assert margins.output_storage_location is not None
    assert margins.parameters["output_width"] == first_margins.parameters["output_width"]
    with (
        backend_for_location(margins.output_storage_location).open(
            margins.output_storage_identifier
        ) as stream,
        Image.open(stream) as canvas,
    ):
        assert canvas.getextrema() == (255, 255)

    assert scan_pipeline.pipeline_progress(job)["processed_pages"] == 2
    ocr_input = ocr._zip_scan_job_pages(job)
    try:
        with (
            zipfile.ZipFile(ocr_input) as archive,
            archive.open("0002-single.png") as zipped_page,
            Image.open(zipped_page) as image,
        ):
            assert image.getextrema() == (255, 255)
    finally:
        ocr_input.unlink()

    finish = client.post(f"{base_url}/finish/")
    assert finish.status_code == 202, finish.content
    variant = FileVariant.objects.get(book=book, source_type=FileVariant.SourceType.CLEANED)
    assert variant.storage_identifier is not None
    with backend_for_location(variant.storage_location).open(variant.storage_identifier) as stream:
        assert len(PdfReader(stream).pages) == 2
    assert not page.stage_states.get(stage="margins").output_storage_identifier
    ocr_input = ocr._zip_scan_job_pages(job)
    try:
        with (
            zipfile.ZipFile(ocr_input) as archive,
            Image.open(io.BytesIO(archive.read("0002-single.png"))) as image,
        ):
            assert image.getextrema() == (255, 255)
            assert image.size[0] == margins.parameters["output_width"]
    finally:
        ocr_input.unlink()

    current_revisions = dict(page.stage_states.values_list("stage", "revision"))
    reset = client.post(
        f"{page_url}/blank/",
        {"declared_blank": False, "version_id": str(version.pk), "revisions": current_revisions},
        content_type="application/json",
    )
    assert reset.status_code == 200, reset.content
    job.refresh_from_db()
    variant.refresh_from_db()
    assert job.refinish_needed and job.state == ScanJob.State.CAPTURED
    assert variant.processing_state == FileVariant.ProcessingState.FAILED
    assert page.active_version() == version

    payload["revisions"] = dict(page.stage_states.values_list("stage", "revision"))
    redeclared = client.post(f"{page_url}/blank/", payload, content_type="application/json")
    assert redeclared.status_code == 200, redeclared.content
    replacement = _put_scan_page(client, page.pk, _black_png_bytes())
    assert replacement.status_code in {200, 201, 202}, replacement.content
    page.refresh_from_db()
    job.refresh_from_db()
    assert not page.declared_blank and page.versions.count() == 2
    assert job.refinish_needed
    events = list(page.disposition_events.order_by("created_at", "pk"))
    assert [event.disposition for event in events] == ["blank", "normal", "blank", "normal"]
    # The rescan cleared the declaration: a system event (no actor) with its reason.
    assert events[-1].reason == "raw_version_replaced" and events[-1].actor_id is None
    # ...and re-queued the page's stages: nothing is skipped any more.
    assert not page.stage_states.filter(status=ScanTailorStageState.StageStatus.SKIPPED).exists()
    margins_after = page.stage_states.get(stage="margins")
    assert "declared_blank" not in margins_after.parameters


@pytest.mark.django_db(transaction=True)
def test_blank_declaration_conflicts_with_concurrent_edits_and_reaches_other_editors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#219 criteria 1 and 7: the declaration uses the stage revisions and live events."""
    from ebook_server.core.models import get_or_create_profile
    from ebook_server.core.workspace_events import WorkspaceEventPoller

    client, job = _create_scan_job_with_book("blank-page-editor-a")
    other = Client()
    other.force_login(get_or_create_profile("blank-page-editor-b").user)
    monkeypatch.setattr(scantailor, "get_queue", lambda: None)
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    upload = _upload_scan_page(client, str(job.pk), 1, "single", b"page", filename="1.png")
    assert upload.status_code == 201
    page = job.pages.get()
    version = page.active_version()
    assert version is not None
    page_url = f"/scan/jobs/{job.pk}/pages/{page.pk}"
    assert client.get(f"{page_url}/stages/deskew/").status_code == 200
    stale = dict(page.stage_states.values_list("stage", "revision"))

    # Only "blank" is built; "excluded" is modelled but refused (open question 1).
    for disposition in ("excluded", "bogus"):
        refused = client.post(
            f"{page_url}/blank/",
            {"disposition": disposition, "version_id": str(version.pk), "revisions": stale},
            content_type="application/json",
        )
        assert refused.status_code == 400
    page.refresh_from_db()
    assert page.page_disposition == ScanPage.Disposition.NORMAL

    # Editor B saves a Deskew change first: editor A's declaration was made against
    # the old revisions and must conflict instead of overwriting B's edit.
    saved = other.post(
        f"{page_url}/stages/deskew/",
        {
            "parameters": {"manual_angle": 1.0},
            "expected_revision": stale["deskew"],
        },
        content_type="application/json",
    )
    assert saved.status_code in {200, 202}, saved.content
    conflict = client.post(
        f"{page_url}/blank/",
        {"disposition": "blank", "version_id": str(version.pk), "revisions": stale},
        content_type="application/json",
    )
    assert conflict.status_code == 409
    assert not page.disposition_events.exists()

    poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
    poller.poll()
    current = dict(page.stage_states.values_list("stage", "revision"))
    declared = client.post(
        f"{page_url}/blank/",
        {"disposition": "blank", "version_id": str(version.pk), "revisions": current},
        content_type="application/json",
    )
    assert declared.status_code == 200, declared.content
    # Editor B's open workspace receives every row of the page with the new state.
    update = poller.poll()
    assert update.payload is not None
    rows = {row["stage"]: row for row in update.payload["stages"]}
    assert {stage for stage, row in rows.items() if row["skip_reason"] == "declared_blank"} == {
        "deskew",
        "select_content",
        "dewarping",
        "output",
    }
    assert rows["margins"]["parameters"].get("declared_blank") is True
    # ...and B's next save with its old revision conflicts; editing a skipped stage
    # is refused until the declaration is cleared.
    late = other.post(
        f"{page_url}/stages/deskew/",
        {
            "parameters": {"manual_angle": 2.0},
            "expected_revision": current["deskew"],
        },
        content_type="application/json",
    )
    assert late.status_code == 409
    refused_edit = other.post(
        f"{page_url}/stages/deskew/",
        {
            "parameters": {"manual_angle": 2.0},
            "expected_revision": rows["deskew"]["revision"],
        },
        content_type="application/json",
    )
    assert refused_edit.status_code == 400

    # The job payload (read by the editor's page strip) reports the disposition.
    detail = client.get(f"/scan/jobs/{job.pk}/")
    assert detail.status_code == 200
    assert {p["page_disposition"] for p in detail.json()["pages"]} >= {"blank"}


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("layout", "status"),
    [("single_page", 201), ("single", 201), ("", 201), ("spread", 400), ("spreads", 400)],
)
def test_scan_job_create_accepts_only_single_page_layout(layout: str, status: int) -> None:
    """#195: Page Split is gone, so the legacy field may only say single_page."""
    from ebook_server.core.models import get_or_create_profile

    profile = get_or_create_profile(f"scanner-layout-{layout or 'empty'}")
    client = Client()
    client.force_login(profile.user)
    response = client.post(
        "/scan/jobs/",
        data={"provisional_title": "Layout", "capture_layout": layout},
    )
    assert response.status_code == status
    if status == 400:
        assert b"single_page" in response.content
        assert not ScanJob.objects.filter(provisional_title="Layout").exists()


@pytest.mark.django_db(transaction=True)
def test_scan_pages_are_ordered_by_sequence_and_side() -> None:
    client, job = _create_scan_job("scanner-order")

    first = _upload_scan_page(
        client,
        str(job.id),
        sequence=2,
        side="right",
        payload=b"image-right",
        filename="p2.png",
    )
    assert first.status_code == 201

    second = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="left",
        payload=b"image-left",
        filename="p1.png",
    )
    assert second.status_code == 201

    job_resp = client.get(f"/scan/jobs/{job.id}/")
    assert job_resp.status_code == 200
    pages = job_resp.json()["pages"]
    assert [(page["logical_sequence_number"], page["side"]) for page in pages] == [
        (1, "left"),
        (2, "right"),
    ]


@pytest.mark.django_db(transaction=True)
def test_idempotent_upload_reuses_existing_page_version() -> None:
    client, job = _create_scan_job("scanner-idempotent")
    payload = b"same-content"
    first = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=payload,
        filename="page.png",
    )
    assert first.status_code == 201

    second = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=payload,
        filename="page.png",
    )
    assert second.status_code == 200

    scan_page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    assert ScanPageVersion.objects.filter(scan_page=scan_page).count() == 1


@pytest.mark.django_db(transaction=True)
def test_upload_retry_does_not_create_new_versions() -> None:
    client, job = _create_scan_job("scanner-retry")
    payload = b"retry-content"

    first = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=payload,
        filename="page.png",
    )
    assert first.status_code == 201
    second = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=payload,
        filename="page.png",
    )
    assert second.status_code == 200

    scan_page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    assert ScanPageVersion.objects.filter(scan_page=scan_page).count() == 1


@pytest.mark.django_db(transaction=True)
def test_scan_page_replacement_preserves_previous_version() -> None:
    client, job = _create_scan_job("scanner-replace")
    first_upload = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=b"version-1",
        filename="v1.png",
    )
    assert first_upload.status_code == 201

    first_page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    detail = client.get(f"/scan/pages/{first_page.id}/")
    assert detail.status_code == 200
    first_active = detail.json()["active_version"]["version"]
    assert first_active == 1

    second_upload = _put_scan_page(client, first_page.id, _png_for(b"version-2"), filename="v2.png")
    assert second_upload.status_code == 201

    versions = ScanPageVersion.objects.filter(scan_page=first_page).order_by("version")
    assert versions.count() == 2
    old_version = versions.first()
    latest_version = versions.last()
    assert old_version is not None
    assert latest_version is not None
    assert old_version.version == 1
    assert latest_version.version == 2
    assert latest_version.is_active is True
    assert old_version.is_active is False


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("entry_point", ["staging", "legacy-replacement"])
def test_existing_page_streams_from_declared_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    entry_point: str,
) -> None:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    client, job = _create_scan_job("streaming-scan")
    page = ScanPage.objects.create(scan_job=job, logical_sequence_number=1, side="single")
    location = StorageLocation.objects.create(
        identifier="external-scans",
        backend=StorageLocation.BackendType.NFS,
        base_path=str(tmp_path / "network-scans"),
    )
    content = _valid_png_bytes() + b"\0" * (2 << 20)
    old_version = ScanPageVersion.objects.create(
        scan_page=page,
        storage_location=location,
        storage_identifier="pages/original.png",
        checksum_sha256=_page_sha(content),
        mime_type="image/png",
        size_bytes=len(content),
        version=1,
        original_filename="original.png",
        is_active=True,
    )
    streams: list[BoundedReadStream] = []

    def open_page(backend: LocalFilesystemStorage, identifier: str) -> BinaryIO:
        assert backend.root == location.base_path
        assert identifier == old_version.storage_identifier
        stream = BoundedReadStream(content)
        streams.append(stream)
        return cast(BinaryIO, stream)

    monkeypatch.setattr(LocalFilesystemStorage, "open", open_page)
    if entry_point == "staging":
        staged, size = views._stage_scan_page_from_existing_version(page)
        try:
            assert size == len(content)
            assert Path(staged).read_bytes() == content
        finally:
            Path(staged).unlink()
    else:
        new_content = _png_for(b"fresh-replacement")
        response = _put_scan_page(client, page.id, new_content, filename="fresh.png")
        assert response.status_code == 201
        assert page.versions.count() == 2
        old_version.refresh_from_db()
        assert not old_version.is_active
        replacement = page.active_version()
        assert replacement is not None
        assert replacement.checksum_sha256 == _page_sha(new_content)
        assert replacement.size_bytes == len(new_content)
        assert replacement.original_filename == "fresh.png"
        assert replacement.storage_location_id != location.id
        assert streams == []  # PUT never re-reads the previous version
        return
    assert len(streams) == 1
    assert streams[0].closed


@pytest.mark.django_db(transaction=True)
def test_scan_job_finish_is_queued_and_creates_cleaned_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = Book.objects.create(title="Scan Finish Book")
    client, job = _create_scan_job_with_book("scan-finish", book_id=book.id)
    queue = _DeferredQueue()
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: queue)

    upload_payload = _valid_png_bytes()
    first = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=upload_payload,
        filename="page-1.png",
    )
    assert first.status_code == 201

    finish = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data={"create_cleaned_variant": "true", "allow_raw_pages": "true"},
    )
    assert finish.status_code == 202, finish.content.decode()
    payload = finish.json()

    assert payload["state"] == ScanJob.State.FINALIZING
    assert payload["finalization_job"]["state"] == ProcessingJob.JobState.QUEUED
    assert len(queue.calls) == 1

    blocked_upload = _upload_scan_page(
        client,
        str(job.id),
        sequence=2,
        side="single",
        payload=upload_payload,
        filename="late-page.png",
    )
    assert blocked_upload.status_code == 400

    queue.run_next()
    job.refresh_from_db()
    assert job.state == ScanJob.State.COMPLETE

    status = client.get(
        f"/scan-finalizations/{payload['finalization_job']['job_id']}/",
    )
    assert status.status_code == 200
    assert status.json()["state"] == ProcessingJob.JobState.SUCCESS
    assert status.json()["cleaned_variant_id"] is not None

    assert (
        FileVariant.objects.filter(
            book=book,
            source_type=FileVariant.SourceType.CLEANED,
            format="pdf",
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
def test_scan_job_finish_failure_does_not_mark_job_complete() -> None:
    book = Book.objects.create(title="Incomplete scan")
    client, job = _create_scan_job_with_book("scan-finish-failure", book_id=book.id)

    finish = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data={"create_cleaned_variant": "true"},
    )

    assert finish.status_code == 400
    job.refresh_from_db()
    assert job.state != ScanJob.State.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_scan_job_finish_rejects_silent_raw_page_fallback() -> None:
    book = Book.objects.create(title="Raw scan is not cleaned")
    client, job = _create_scan_job_with_book("scan-raw-rejected", book_id=book.id)
    upload = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_valid_png_bytes(),
        filename="raw.png",
    )
    assert upload.status_code == 201

    finish = client.post(f"/scan/jobs/{job.id}/finish/")

    assert finish.status_code == 400
    assert "processed output" in finish.content.decode()
    job.refresh_from_db()
    assert job.state != ScanJob.State.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_scan_finalization_failure_restores_job_and_can_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = Book.objects.create(title="Retry scan finalization")
    client, job = _create_scan_job_with_book("scan-finalization-retry", book_id=book.id)
    upload = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_valid_png_bytes(),
        filename="retry.png",
    )
    assert upload.status_code == 201

    queue = _DeferredQueue()
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: queue)
    original_builder = scan_pipeline._build_pdf_from_images

    finish = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data={"allow_raw_pages": "true"},
    )
    assert finish.status_code == 202
    finalization_job_id = finish.json()["finalization_job"]["job_id"]

    def fail_builder(_paths: Any) -> Path:
        raise scan_pipeline.ScanPipelineError("synthetic packaging failure")

    monkeypatch.setattr(scan_pipeline, "_build_pdf_from_images", fail_builder)
    with pytest.raises(scan_pipeline.ScanPipelineError, match="synthetic packaging failure"):
        queue.run_next()

    job.refresh_from_db()
    processing_job = ProcessingJob.objects.get(job_id=finalization_job_id)
    assert job.state == ScanJob.State.CAPTURING
    assert processing_job.state == ProcessingJob.JobState.FAILED
    assert processing_job.details["attempt_count"] == 1

    monkeypatch.setattr(scan_pipeline, "_build_pdf_from_images", original_builder)
    scan_pipeline.process_scan_finalization_job(finalization_job_id)

    job.refresh_from_db()
    processing_job.refresh_from_db()
    assert job.state == ScanJob.State.COMPLETE
    assert processing_job.state == ProcessingJob.JobState.SUCCESS
    assert processing_job.details["attempt_count"] == 2


@pytest.mark.django_db(transaction=True)
def test_scan_job_finish_auto_ocr_and_conversion_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = Book.objects.create(title="Scan Finish OCR chain")
    client, job = _create_scan_job_with_book("scan-finish-chain", book_id=book.id)

    upload_payload = _valid_png_bytes()
    upload = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=upload_payload,
        filename="clean-page.png",
    )
    assert upload.status_code == 201

    _install_ocr_client(
        monkeypatch,
        outputs={"txt": b"scan text", "pdf": b"%PDF"},
        poll_plan=[
            {
                "status": "running",
                "artifacts": [],
            },
            {
                "status": "succeeded",
                "artifacts": [
                    ocr.OCRArtifactDescriptor(format="txt", filename="page.txt"),
                    ocr.OCRArtifactDescriptor(format="pdf", filename="page.pdf"),
                ],
            },
        ],
    )

    finish = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data={
            "create_cleaned_variant": True,
            "allow_raw_pages": True,
            "auto_ocr": True,
            "target_formats": ["txt", "pdf"],
        },
        content_type="application/json",
    )
    assert finish.status_code == 202, finish.content.decode()
    response_payload = finish.json()

    assert response_payload["state"] == "complete"
    finalization_payload = response_payload["finalization_job"]
    assert finalization_payload["cleaned_variant_id"] is not None
    assert finalization_payload["ocr_job_id"] is not None

    job_refresh = ScanJob.objects.get(pk=job.id)
    detail_payload = client.get(f"/scan/jobs/{job_refresh.id}/").json()
    assert (
        detail_payload["pipeline"]["cleaned_variant_id"]
        == finalization_payload["cleaned_variant_id"]
    )
    assert any(
        item["state"] == "success" and item["source_type"] == "variant"
        for item in detail_payload["pipeline"]["ocr_jobs"]
    )

    ocr_variants = list(
        FileVariant.objects.filter(
            book=book,
            source_type=FileVariant.SourceType.OCR,
            format="txt",
        ),
    )
    assert len(ocr_variants) == 1
    ocr_variant = ocr_variants[0]

    _create_stub_converter(monkeypatch)
    conversion_response = client.post(
        f"/library/books/{book.id}/convert/",
        data=f'{{"source_variant_id": {ocr_variant.id}, "target_format": "epub"}}',
        content_type="application/json",
    )
    assert conversion_response.status_code == 202

    conversion_payload = conversion_response.json()
    assert conversion_payload["state"] == "success", f"conversion payload: {conversion_payload!r}"
    assert conversion_payload["source_variant_id"] == ocr_variant.id
    assert conversion_payload["target_format"] == "epub"


@pytest.mark.django_db(transaction=True)
def test_page_replacement_invalidates_scan_job_library_derivatives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = Book.objects.create(title="Rescan invalidates lineage")
    client, job = _create_scan_job_with_book("scan-rescan-lineage", book_id=book.id)

    first_upload = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_valid_png_bytes(),
        filename="before.png",
    )
    assert first_upload.status_code == 201

    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")

    scan_pipeline.create_cleaned_variant_for_scan_job(
        job,
        request_name="cleaned",
        allow_raw_pages=True,
    )

    _install_ocr_client(
        monkeypatch,
        outputs={"txt": b"text from scan", "pdf": b"%PDF-1.4"},
        poll_plan=[
            {"status": "running", "artifacts": []},
            {
                "status": "succeeded",
                "artifacts": [
                    ocr.OCRArtifactDescriptor(format="txt", filename="page.txt"),
                    ocr.OCRArtifactDescriptor(format="pdf", filename="page.pdf"),
                ],
            },
        ],
    )

    cleaned_variant = FileVariant.objects.get(
        book=book,
        source_type=FileVariant.SourceType.CLEANED,
    )

    ocr_request = client.post(
        f"/library/books/{book.id}/ocr/",
        data=f'{{"source_variant_id": {cleaned_variant.id}, "target_formats": ["txt", "pdf"]}}',
        content_type="application/json",
    )
    assert ocr_request.status_code == 202

    _create_stub_converter(monkeypatch)
    ocr_txt_variant = FileVariant.objects.get(
        book=book,
        source_type=FileVariant.SourceType.OCR,
        format="txt",
    )

    conversion_response = client.post(
        f"/library/books/{book.id}/convert/",
        data=f'{{"source_variant_id": {ocr_txt_variant.id}, "target_format": "epub"}}',
        content_type="application/json",
    )
    assert conversion_response.status_code == 202

    ocr_variants = list(
        FileVariant.objects.filter(
            book=book,
            source_type=FileVariant.SourceType.OCR,
        ).order_by("id"),
    )
    converted_variants = list(
        FileVariant.objects.filter(
            book=book,
            source_type=FileVariant.SourceType.CONVERTED,
        ).order_by("id"),
    )

    assert cleaned_variant.processing_state == FileVariant.ProcessingState.COMPLETE
    assert all(
        variant.processing_state == FileVariant.ProcessingState.COMPLETE
        for variant in (ocr_variants + converted_variants)
    )

    replacement = _put_scan_page(client, page.id, _black_png_bytes(), filename="after.png")
    assert replacement.status_code == 201

    cleaned_variant.refresh_from_db()
    ocr_variants = list(
        FileVariant.objects.filter(
            book=book,
            source_type=FileVariant.SourceType.OCR,
        ).order_by("id"),
    )
    converted_variants = list(
        FileVariant.objects.filter(
            book=book,
            source_type=FileVariant.SourceType.CONVERTED,
        ).order_by("id"),
    )

    assert cleaned_variant.processing_state == FileVariant.ProcessingState.FAILED
    assert all(
        variant.processing_state == FileVariant.ProcessingState.FAILED
        for variant in (ocr_variants + converted_variants)
    )

    pipeline = client.get(f"/scan/jobs/{job.id}/").json()["pipeline"]
    assert pipeline["cleaned_variant_id"] is None
