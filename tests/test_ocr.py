from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

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

    def download_artifact(self, remote_job_id: str, artifact: ocr.OCRArtifactDescriptor) -> bytes:
        del remote_job_id
        payload = self.outputs.get(artifact.format)
        if payload is None:
            raise AssertionError(f"unexpected format: {artifact.format}")
        return payload


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
