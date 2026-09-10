from __future__ import annotations

import hashlib
import io
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from PIL import Image

from ebook_server.core import conversion, ocr, scan_pipeline, views
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    StorageLocation,
)
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
        self.calls.append((func, args, kwargs))
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
    monkeypatch.setattr(ocr, "_poll_interval", lambda: 0.0)


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

    second_upload = client.put(
        f"/scan/pages/{first_page.id}/",
        data={
            "checksum_sha256": _page_sha(b"version-2"),
            "file": SimpleUploadedFile("v2.png", b"version-2", content_type="image/png"),
        },
    )
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
        response = client.put(
            f"/scan/pages/{page.id}/",
            data={
                "checksum_sha256": _page_sha(content),
                "file": SimpleUploadedFile("legacy.png", b"legacy"),
            },
        )
        assert response.status_code == 201
        assert page.versions.count() == 2
        old_version.refresh_from_db()
        assert not old_version.is_active
        replacement = page.active_version()
        assert replacement is not None
        assert replacement.checksum_sha256 == _page_sha(content)
        assert replacement.size_bytes == len(content)
        assert replacement.storage_location_id != location.id
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
    assert job.state == ScanJob.State.OPEN
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

    replacement = client.put(
        f"/scan/pages/{page.id}/",
        data={
            "checksum_sha256": _page_sha(_black_png_bytes()),
            "file": SimpleUploadedFile(
                "after.png",
                _black_png_bytes(),
                content_type="image/png",
            ),
        },
    )
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
