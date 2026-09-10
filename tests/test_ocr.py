from __future__ import annotations

import base64
import hashlib
import json
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any, BinaryIO, cast
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.utils import timezone

from ebook_server.core import ocr
from ebook_server.core.models import (
    AppUserProfile,
    Book,
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage
from tests.streaming_fixtures import BoundedReadStream


class _ScriptedOCRClient:
    """Simple OCR client script for unit tests."""

    name = "scripted"

    def __init__(self, poll_plan: list[dict[str, object]], outputs: dict[str, bytes]) -> None:
        self.outputs = outputs
        self.poll_plan = poll_plan
        self.poll_calls = 0
        self.submitted: list[Path] = []
        self.submitted_payloads: list[bytes] = []

    def submit_job(
        self,
        source_path: Path,
        source_format: str,
        source_mime: str,
        request: ocr.OCRJobRequest,
    ) -> str:
        del source_format, source_mime, request
        self.submitted.append(source_path)
        self.submitted_payloads.append(source_path.read_bytes())
        return "remote-ocr-job"

    def poll_job(self, remote_job_id: str) -> ocr.OCRPollResult:
        if remote_job_id != "remote-ocr-job":
            return ocr.OCRPollResult(
                status="failed",
                artifacts=[],
                error="bad job id",
            )
        index = min(self.poll_calls, len(self.poll_plan) - 1)
        entry = self.poll_plan[index]
        self.poll_calls += 1
        return ocr.OCRPollResult(
            status=str(entry["status"]),
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
            raise AssertionError(f"unexpected format: {artifact.format}")
        yield payload


@pytest.mark.parametrize(
    "content_url", [None, "/outputs/result.txt", "https://ocr.test/result.txt"]
)
def test_http_ocr_download_streams_and_closes_response(content_url: str | None) -> None:
    chunk = b"x" * ocr.ARTIFACT_CHUNK_BYTES

    class ResponseStream(httpx.SyncByteStream):
        sent = 0
        closed = False

        def __iter__(self) -> Iterator[bytes]:
            for _ in range(3):
                self.sent += 1
                yield chunk

        def close(self) -> None:
            self.closed = True

    stream = ResponseStream()

    def handler(request: httpx.Request) -> httpx.Response:
        expected = content_url or "/v1/ocr/jobs/remote/artifacts/txt"
        if expected.startswith("/"):
            expected = f"https://ocr.test{expected}"
        assert str(request.url) == expected
        return httpx.Response(200, stream=stream)

    client = ocr.HttpxOCRClient("https://ocr.test")
    client._client.close()
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        client._client = transport
        chunks = client.iter_artifact(
            "remote",
            ocr.OCRArtifactDescriptor(format="txt", content_url=content_url),
        )
        assert next(chunks) == chunk
        assert stream.sent == 1
        assert list(chunks) == [chunk, chunk]
        assert stream.closed


def test_http_ocr_inline_artifact_decodes_in_bounded_chunks() -> None:
    payload = b"inline text" * (ocr.ARTIFACT_CHUNK_BYTES // 3)
    client = ocr.HttpxOCRClient("https://ocr.test")
    try:
        chunks = list(
            client.iter_artifact(
                "remote",
                ocr.OCRArtifactDescriptor(format="txt", content=base64.b64encode(payload).decode()),
            )
        )
        assert len(chunks) > 1
        assert max(map(len, chunks)) <= ocr.ARTIFACT_CHUNK_BYTES
        assert b"".join(chunks) == payload
        with pytest.raises(ocr.OCRClientError, match="content_base64 invalid"):
            list(
                client.iter_artifact("remote", ocr.OCRArtifactDescriptor(format="txt", content="!"))
            )
    finally:
        client._client.close()


def _profile(ldap_uid: str) -> AppUserProfile:
    return get_or_create_profile(ldap_uid)


def _configure_storage(tmp_path: Path) -> LocalFilesystemStorage:
    django_settings.APP_DATA_DIR = tmp_path
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return LocalFilesystemStorage(str(tmp_path / "library-artifacts"))


def _seed_book_variant(
    book: Book,
    storage: LocalFilesystemStorage,
    *,
    fmt: str,
    mime: str,
    filename: str,
    payload: bytes,
    uploader_id: int | None = None,
) -> FileVariant:
    source = storage.resolve_location(f"seed-source-{uuid4()}.bin")
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(payload)

    destination = storage.generate_destination(f"books/{book.id}/incoming", filename)
    storage.store(str(source), destination)

    return FileVariant.objects.create(
        book=book,
        format=fmt,
        mime_type=mime,
        original_filename=filename,
        storage_location=StorageLocation.objects.get(identifier="default"),
        storage_identifier=destination,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
        uploader_id=uploader_id,
        imported_at=timezone.now(),
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )


def _seed_scan_job_with_single_page(
    *,
    book: Book,
    storage: LocalFilesystemStorage,
    payload: bytes,
) -> ScanJob:
    scan_job = ScanJob.objects.create(
        book=book,
        provisional_title="OCR Scan",
        scanner_id="scan-ocr",
    )
    page = ScanPage.objects.create(scan_job=scan_job, logical_sequence_number=1, side="single")

    source_path = storage.resolve_location(f"scan-source-{uuid4()}.png")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(payload)
    storage_identifier = storage.generate_destination(f"scan-jobs/{scan_job.id}", "page-1.png")
    storage.store(str(source_path), storage_identifier)

    ScanPageVersion.objects.create(
        scan_page=page,
        storage_location=StorageLocation.objects.get(identifier="default"),
        storage_identifier=storage_identifier,
        checksum_sha256=hashlib.sha256(payload).hexdigest(),
        mime_type="image/png",
        size_bytes=len(payload),
        version=1,
        uploader_id=None,
        original_filename="page-1.png",
        is_active=True,
    )
    return scan_job


def _ocr_client_with_payloads(
    outputs: dict[str, bytes],
    poll_plan: list[dict[str, Any]],
) -> ocr.OCRClientProtocol:
    for artifact in poll_plan:
        artifacts = artifact.get("artifacts")
        if not isinstance(artifacts, list):
            continue
        for nested in artifacts:
            assert isinstance(nested, ocr.OCRArtifactDescriptor)
    client = _ScriptedOCRClient(poll_plan, outputs)
    return client


def _install_ocr_client(
    monkeypatch: pytest.MonkeyPatch,
    outputs: dict[str, bytes],
    poll_plan: list[dict[str, Any]],
) -> None:
    client = _ocr_client_with_payloads(outputs, poll_plan)
    monkeypatch.setattr(ocr, "_get_ocr_client", lambda: client)
    monkeypatch.setattr(ocr, "_poll_interval", lambda: 0.0)


@pytest.mark.django_db(transaction=True)
def test_ocr_variant_happy_path_accepts_intermediate_statuses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("ocr-variant")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(ocr, "_default_ocr_storage", lambda: storage)
    book = Book.objects.create(title="OCR Book")
    source_payload = b"raw source pdf"
    variant = _seed_book_variant(
        book,
        storage,
        fmt="pdf",
        mime="application/pdf",
        filename="book.pdf",
        payload=source_payload,
        uploader_id=profile.user_id,
    )
    artifacts = [
        ocr.OCRArtifactDescriptor(
            format="txt",
            filename="page.txt",
        ),
        ocr.OCRArtifactDescriptor(
            format="pdf",
            filename="page.pdf",
        ),
    ]
    poll_plan: list[dict[str, Any]] = [
        {"status": "accepted"},
        {"status": "running"},
        {"status": "succeeded", "artifacts": artifacts},
    ]
    _install_ocr_client(
        monkeypatch,
        outputs={"txt": b"hello txt", "pdf": b"%PDF"},
        poll_plan=poll_plan,
    )

    client = Client()
    client.force_login(profile.user)
    response = client.post(
        f"/library/books/{book.id}/ocr/",
        data=json.dumps(
            {
                "source_variant_id": variant.id,
                "target_formats": ["txt", "pdf"],
            },
        ),
        content_type="application/json",
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == ProcessingJob.JobState.SUCCESS
    assert payload["remote_status"] == "succeeded"
    assert len(payload["output_variants"]) == 2

    job = ProcessingJob.objects.get(job_id=payload["job_id"])
    assert job.state == ProcessingJob.JobState.SUCCESS
    assert FileVariant.objects.filter(source_type=FileVariant.SourceType.OCR).count() == 2

    status_payload = client.get(f"/ocr/{job.job_id}/").json()
    assert status_payload["state"] == ProcessingJob.JobState.SUCCESS


@pytest.mark.django_db(transaction=True)
def test_ocr_reads_source_from_declared_storage_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("ocr-mounted-source")
    output_storage = _configure_storage(tmp_path)
    monkeypatch.setattr(ocr, "_default_ocr_storage", lambda: output_storage)
    custom_root = tmp_path / "ocr-network-mount"
    custom_storage = LocalFilesystemStorage(str(custom_root))
    custom_location = StorageLocation.objects.create(
        identifier="ocr-network-books",
        backend=StorageLocation.BackendType.LOCAL,
        base_path=str(custom_root),
    )
    book = Book.objects.create(title="OCR mounted source")
    source_payload = b"%PDF-ocr-mounted-source"
    source = tmp_path / "ocr-mounted.pdf"
    source.write_bytes(source_payload)
    identifier = custom_storage.generate_destination(f"books/{book.id}", "source.pdf")
    custom_storage.store(str(source), identifier)
    variant = FileVariant.objects.create(
        book=book,
        format="pdf",
        mime_type="application/pdf",
        original_filename="source.pdf",
        storage_location=custom_location,
        storage_identifier=identifier,
        size_bytes=len(source_payload),
        sha256=hashlib.sha256(source_payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )
    scripted_client = _ScriptedOCRClient(
        [
            {
                "status": "succeeded",
                "artifacts": [ocr.OCRArtifactDescriptor(format="txt", filename="ocr.txt")],
            },
        ],
        {"txt": b"mounted source text"},
    )
    monkeypatch.setattr(ocr, "_get_ocr_client", lambda: scripted_client)
    monkeypatch.setattr(ocr, "_poll_interval", lambda: 0.0)
    client = Client()
    client.force_login(profile.user)

    response = client.post(
        f"/library/books/{book.id}/ocr/",
        data=json.dumps({"source_variant_id": variant.id, "target_formats": ["txt"]}),
        content_type="application/json",
    )

    assert response.status_code == 202
    assert response.json()["state"] == ProcessingJob.JobState.SUCCESS
    assert scripted_client.submitted_payloads == [source_payload]


@pytest.mark.django_db(transaction=True)
def test_scan_job_ocr_request_zips_pages_and_materializes_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("ocr-scan")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(ocr, "_default_ocr_storage", lambda: storage)
    book = Book.objects.create(title="OCR Scan Book")
    scan_job = _seed_scan_job_with_single_page(
        book=book,
        storage=storage,
        payload=b"page content",
    )

    _install_ocr_client(
        monkeypatch,
        outputs={"txt": b"scan txt"},
        poll_plan=[
            {"status": "running"},
            {
                "status": "succeeded",
                "artifacts": [
                    ocr.OCRArtifactDescriptor(format="txt", filename="scan.txt"),
                ],
            },
        ],
    )

    client = Client()
    client.force_login(profile.user)
    response = client.post(
        f"/scan/jobs/{scan_job.id}/ocr/",
        data=json.dumps({"target_formats": ["txt"]}),
        content_type="application/json",
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == ProcessingJob.JobState.SUCCESS
    job = ProcessingJob.objects.get(job_id=payload["job_id"])
    assert job.state == ProcessingJob.JobState.SUCCESS
    assert (
        FileVariant.objects.filter(
            book=book,
            source_type=FileVariant.SourceType.OCR,
        ).count()
        == 1
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("interrupted", [False, True])
def test_ocr_scan_archive_streams_each_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    interrupted: bool,
) -> None:
    storage = _configure_storage(tmp_path)
    content = b"page bytes" * (1 << 19)
    scan_job = _seed_scan_job_with_single_page(
        book=Book.objects.create(title="Streaming pages"),
        storage=storage,
        payload=content,
    )
    streams: list[BoundedReadStream] = []
    version = scan_job.pages.get().active_version()
    assert version is not None
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    class PageStream(BoundedReadStream):
        def read(self, size: int | None = -1) -> bytes:
            if interrupted and self.tell() > 0:
                raise FileNotFoundError("mount lost during page read")
            return super().read(size)

    def open_page(backend: LocalFilesystemStorage, identifier: str) -> BinaryIO:
        assert backend.root == storage.root
        assert identifier == version.storage_identifier
        stream = PageStream(content)
        streams.append(stream)
        return cast(BinaryIO, stream)

    monkeypatch.setattr(LocalFilesystemStorage, "open", open_page)
    if interrupted:
        with pytest.raises(FileNotFoundError, match="mount lost"):
            ocr._zip_scan_job_pages(scan_job)
        assert streams[0].closed
        assert not list(tmp_path.glob("ocr-scan-pages-*"))
        return
    archive = ocr._zip_scan_job_pages(scan_job)
    try:
        with ZipFile(archive) as zipped:
            assert zipped.namelist() == ["0001-single.png"]
            assert zipped.read("0001-single.png") == content
        assert len(streams) == 1
        assert streams[0].closed
    finally:
        archive.unlink()


@pytest.mark.django_db(transaction=True)
def test_ocr_streamed_artifacts_preserve_versions_and_reuse_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_storage(tmp_path)
    book = Book.objects.create(title="Streaming OCR")
    outputs = {"txt": b"First recognition result"}
    client = _ScriptedOCRClient([], outputs)
    monkeypatch.setattr(ocr, "_get_ocr_client", lambda: client)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))

    def materialize() -> FileVariant:
        result = ocr._materialize_ocr_artifact(
            source_book=book,
            source_variant=None,
            artifact=ocr.OCRArtifactDescriptor(format="txt", filename="result.txt"),
            remote_job_id="same-remote-job",
            request=ocr.OCRJobRequest(target_formats=["txt"]),
        )
        assert not list(tmp_path.glob("ocr-artifact-*"))
        return FileVariant.objects.get(pk=result[0]["file_variant_id"])

    first = materialize()
    assert materialize() == first
    outputs["txt"] = b"Improved recognition result" * (1 << 17)
    second = materialize()
    assert materialize() == second
    assert first.storage_identifier != second.storage_identifier
    assert FileVariant.objects.filter(book=book).count() == 2
    assert storage.hash(first.storage_identifier) == first.sha256
    assert storage.hash(second.storage_identifier) == second.sha256
    assert b"".join(storage.stream(first.storage_identifier)) == b"First recognition result"
    assert second.size_bytes == len(outputs["txt"])
    assert second.sha256 == hashlib.sha256(outputs["txt"]).hexdigest()


@pytest.mark.django_db(transaction=True)
def test_ocr_interrupted_download_removes_partial_temporary_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = _configure_storage(tmp_path)
    book = Book.objects.create(title="Interrupted OCR")
    source = _seed_book_variant(
        book,
        storage,
        fmt="pdf",
        mime="application/pdf",
        filename="source.pdf",
        payload=b"%PDF",
    )

    class InterruptedClient(_ScriptedOCRClient):
        def iter_artifact(
            self,
            remote_job_id: str,
            artifact: ocr.OCRArtifactDescriptor,
        ) -> Iterator[bytes]:
            yield b"partial output"
            raise ocr.OCRClientError("download interrupted")

    client = InterruptedClient(
        [{"status": "succeeded", "artifacts": [ocr.OCRArtifactDescriptor(format="txt")]}],
        {},
    )
    monkeypatch.setattr(ocr, "_get_ocr_client", lambda: client)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    job = ocr.request_ocr_from_variant(
        source_variant=source,
        target_formats=["txt"],
    )
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.FAILED
    assert "download interrupted" in job.details["error"]
    assert not FileVariant.objects.filter(source_type=FileVariant.SourceType.OCR).exists()
    assert not list(tmp_path.glob("ocr-artifact-*"))
    assert not list(tmp_path.glob("ocr-source-*"))
    assert storage.hash(source.storage_identifier) == source.sha256


@pytest.mark.django_db(transaction=True)
def test_ocr_failed_job_records_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("ocr-fail")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(ocr, "_default_ocr_storage", lambda: storage)

    book = Book.objects.create(title="Failing OCR")
    variant = _seed_book_variant(
        book,
        storage,
        fmt="pdf",
        mime="application/pdf",
        filename="book.pdf",
        payload=b"source",
        uploader_id=profile.user_id,
    )
    _install_ocr_client(
        monkeypatch,
        outputs={"txt": b""},
        poll_plan=[
            {"status": "failed", "error": "simulated ocr error", "artifacts": []},
        ],
    )

    client = Client()
    client.force_login(profile.user)
    response = client.post(
        f"/library/books/{book.id}/ocr/",
        data=json.dumps(
            {
                "source_variant_id": variant.id,
                "target_formats": ["txt"],
            },
        ),
        content_type="application/json",
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == ProcessingJob.JobState.FAILED
    job = ProcessingJob.objects.get(job_id=payload["job_id"])
    assert job.state == ProcessingJob.JobState.FAILED
    assert "simulated ocr error" in str(job.details.get("error", ""))
    assert FileVariant.objects.filter(source_type=FileVariant.SourceType.OCR).count() == 0


@pytest.mark.django_db(transaction=True)
def test_ocr_timeout_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("ocr-timeout")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(ocr, "_default_ocr_storage", lambda: storage)
    monkeypatch.setattr(ocr, "_poll_limit", lambda: 2)
    book = Book.objects.create(title="Timeout OCR")
    variant = _seed_book_variant(
        book,
        storage,
        fmt="pdf",
        mime="application/pdf",
        filename="book.pdf",
        payload=b"source",
        uploader_id=profile.user_id,
    )
    _install_ocr_client(
        monkeypatch,
        outputs={"txt": b"never used"},
        poll_plan=[{"status": "running"}],
    )

    client = Client()
    client.force_login(profile.user)
    response = client.post(
        f"/library/books/{book.id}/ocr/",
        data=json.dumps(
            {"source_variant_id": variant.id, "target_formats": ["txt"]},
        ),
        content_type="application/json",
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == ProcessingJob.JobState.FAILED
    assert "timed out" in str(payload["error"])


@pytest.mark.django_db(transaction=True)
def test_ocr_malformed_poll_payload_is_recorded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _profile("ocr-malformed")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(ocr, "_default_ocr_storage", lambda: storage)
    book = Book.objects.create(title="Malformed OCR")
    variant = _seed_book_variant(
        book,
        storage,
        fmt="pdf",
        mime="application/pdf",
        filename="book.pdf",
        payload=b"source",
        uploader_id=profile.user_id,
    )
    poll_plan = [{"status": 123}]
    _install_ocr_client(
        monkeypatch,
        outputs={"txt": b"ignored"},
        poll_plan=poll_plan,
    )
    monkeypatch.setattr(ocr, "_poll_limit", lambda: 1)

    client = Client()
    client.force_login(profile.user)
    response = client.post(
        f"/library/books/{book.id}/ocr/",
        data=json.dumps({"source_variant_id": variant.id, "target_formats": ["txt"]}),
        content_type="application/json",
    )
    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == ProcessingJob.JobState.FAILED
    assert payload["error"]
