from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.conf import settings as django_settings
from django.test import Client
from django.utils import timezone

from ebook_server.core import conversion
from ebook_server.core.conversion import ConversionCapability
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage


@dataclass(frozen=True)
class StubConverter:
    payload: bytes
    version_text: str = "stub-v1"

    name = "stub"
    binary = "stub-binary"

    def is_available(self) -> bool:
        return True

    def version(self) -> str:
        return self.version_text

    def convert(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        source_format: str,
        target_format: str,
        options: dict[str, str] | None = None,
    ) -> None:
        destination_path.write_bytes(self.payload)

        _ = (source_format, target_format, options)


class FailingStubConverter:
    name = "stub-fail"
    binary = "stub-fail"

    def is_available(self) -> bool:
        return True

    def version(self) -> str:
        return "stub-fail-v1"

    def convert(self, **kwargs: object) -> None:
        _ = kwargs
        raise conversion.ConversionError("simulated converter failure")


class EchoStubConverter(StubConverter):
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
        destination_path.write_bytes(b"converted:" + source_path.read_bytes())


def _configure_storage(tmp_path: Path) -> LocalFilesystemStorage:
    django_settings.APP_DATA_DIR = tmp_path
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return LocalFilesystemStorage(str(tmp_path / "library-artifacts"))


def _create_book_with_variant(
    storage: LocalFilesystemStorage,
    *,
    source_fmt: str,
    source_payload: bytes,
    source_file_name: str,
) -> FileVariant:
    book = Book.objects.create(title="Converted Source")
    source_path = storage.resolve_location(f"incoming/{uuid4()}.{source_fmt}")
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_bytes(source_payload)

    destination = storage.generate_destination(f"books/{book.id}/incoming", source_file_name)
    storage.store(str(source_path), destination)

    return FileVariant.objects.create(
        book=book,
        format=source_fmt,
        mime_type=source_payload.decode("utf-8", errors="ignore")
        if source_fmt == "txt"
        else f"application/{source_fmt}",
        original_filename=source_file_name,
        storage_location=StorageLocation.objects.get(identifier="default"),
        storage_identifier=destination,
        size_bytes=len(source_payload),
        sha256=hashlib.sha256(source_payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
        imported_at=timezone.now(),
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )


def _request_conversion(
    client: Client,
    *,
    book_id: int,
    source_variant: FileVariant,
    target_format: str,
    request_options: dict[str, str] | None = None,
) -> Any:
    payload = {
        "source_variant_id": source_variant.id,
        "target_format": target_format,
    }
    if request_options:
        payload["request_options"] = request_options
    return client.post(
        f"/library/books/{book_id}/convert/",
        data=json.dumps(payload),
        content_type="application/json",
    )


@pytest.mark.django_db(transaction=True)
def test_conversion_is_exposed_on_book_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_or_create_profile("conversion-reader")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(
        "ebook_server.core.conversion.CONVERTER_REGISTRY",
        {"calibre": StubConverter(b"pdf->txt", "calibre-v1")},
    )

    variant = _create_book_with_variant(
        storage,
        source_fmt="pdf",
        source_payload=b"pdf-source-content",
        source_file_name="book.pdf",
    )

    client = Client()
    client.force_login(profile.user)
    response = client.get(f"/library/books/{variant.book_id}/")
    detail = response.json()
    assert "conversion_options" in detail
    assert {"source_variant_id": variant.id, "target_format": "txt"} in [
        {
            "source_variant_id": int(option["source_variant_id"]),
            "target_format": option["target_format"],
        }
        for option in detail["conversion_options"]
    ]


@pytest.mark.django_db(transaction=True)
def test_conversion_direct_route_creates_output_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_or_create_profile("converter-user")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(
        "ebook_server.core.conversion.CONVERTER_REGISTRY",
        {"calibre": StubConverter(b"converted-text", "calibre-v2")},
    )

    variant = _create_book_with_variant(
        storage,
        source_fmt="pdf",
        source_payload=b"%PDF-stub",
        source_file_name="source.pdf",
    )
    client = Client()
    client.force_login(profile.user)

    response = _request_conversion(
        client,
        book_id=variant.book_id,
        source_variant=variant,
        target_format="txt",
        request_options={"quality": "high"},
    )
    assert response.status_code == 202

    payload = response.json()
    job = ProcessingJob.objects.get(job_id=payload["job_id"])
    assert job.state == ProcessingJob.JobState.SUCCESS
    assert payload["state"] == ProcessingJob.JobState.SUCCESS
    assert payload["output_variant_id"] is not None

    output_variant = FileVariant.objects.get(pk=payload["output_variant_id"])
    assert output_variant.format == "txt"
    assert output_variant.source_variant_id == variant.id
    assert output_variant.converter_tool == "stub"
    assert output_variant.converter_version == "calibre-v2"
    assert output_variant.converter_options["request_options"]["quality"] == "high"

    status_response = client.get(f"/conversions/{str(job.job_id)}/")
    assert status_response.status_code == 200
    status_payload = status_response.json()
    assert status_payload["state"] == ProcessingJob.JobState.SUCCESS
    assert status_payload["source_variant_id"] == variant.id
    assert status_payload["target_format"] == "txt"


@pytest.mark.django_db(transaction=True)
def test_conversion_reads_and_writes_declared_storage_backend(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    django_settings.APP_DATA_DIR = tmp_path
    custom_root = tmp_path / "network-mount"
    storage = LocalFilesystemStorage(str(custom_root))
    location = StorageLocation.objects.create(
        identifier="network-books",
        backend=StorageLocation.BackendType.LOCAL,
        base_path=str(custom_root),
    )
    profile = get_or_create_profile("custom-storage-converter")
    book = Book.objects.create(title="Mounted conversion source")
    source = tmp_path / "mounted-source.pdf"
    source.write_bytes(b"%PDF-from-mounted-backend")
    identifier = storage.generate_destination(f"books/{book.id}", "source.pdf")
    storage.store(str(source), identifier)
    variant = FileVariant.objects.create(
        book=book,
        format="pdf",
        mime_type="application/pdf",
        original_filename="source.pdf",
        storage_location=location,
        storage_identifier=identifier,
        size_bytes=source.stat().st_size,
        sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )
    monkeypatch.setattr(
        conversion,
        "CONVERTER_REGISTRY",
        {"calibre": EchoStubConverter(b"unused")},
    )
    client = Client()
    client.force_login(profile.user)

    response = _request_conversion(
        client,
        book_id=book.id,
        source_variant=variant,
        target_format="txt",
    )

    assert response.status_code == 202
    output = FileVariant.objects.get(pk=response.json()["output_variant_id"])
    assert output.storage_location_id == location.id
    with storage.open(output.storage_identifier) as stream:
        assert stream.read() == b"converted:%PDF-from-mounted-backend"


@pytest.mark.django_db(transaction=True)
def test_conversion_multi_hop_route_is_used(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_or_create_profile("conversion-multihop")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(
        "ebook_server.core.conversion.CAPABILITIES",
        (
            ConversionCapability("pdf", "txt", "stub"),
            ConversionCapability("txt", "epub", "stub"),
        ),
    )
    monkeypatch.setattr(
        "ebook_server.core.conversion.CONVERTER_REGISTRY",
        {"stub": StubConverter(b"txt->epub", "stub-v1")},
    )

    variant = _create_book_with_variant(
        storage,
        source_fmt="pdf",
        source_payload=b"%PDF-hops",
        source_file_name="source.pdf",
    )
    client = Client()
    client.force_login(profile.user)
    response = _request_conversion(
        client,
        book_id=variant.book_id,
        source_variant=variant,
        target_format="epub",
    )
    assert response.status_code == 202

    payload = response.json()
    assert payload["state"] == ProcessingJob.JobState.SUCCESS
    output_variant = FileVariant.objects.get(pk=payload["output_variant_id"])
    assert output_variant.format == "epub"
    assert output_variant.source_variant_id == variant.id
    assert output_variant.converter_tool == "stub -> stub"
    assert output_variant.converter_version == "stub-v1 -> stub-v1"


@pytest.mark.django_db(transaction=True)
def test_conversion_unsupported_pair_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_or_create_profile("conversion-unsupported")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(
        "ebook_server.core.conversion.CONVERTER_REGISTRY",
        {"calibre": StubConverter(b"unused")},
    )

    variant = _create_book_with_variant(
        storage,
        source_fmt="pdf",
        source_payload=b"%PDF-sad",
        source_file_name="source.pdf",
    )
    client = Client()
    client.force_login(profile.user)

    response = _request_conversion(
        client,
        book_id=variant.book_id,
        source_variant=variant,
        target_format="djvu",
    )
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_conversion_failure_is_recorded_as_failed_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_or_create_profile("conversion-fail")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(
        "ebook_server.core.conversion.CONVERTER_REGISTRY",
        {"calibre": FailingStubConverter()},
    )

    variant = _create_book_with_variant(
        storage,
        source_fmt="pdf",
        source_payload=b"%PDF-fail",
        source_file_name="source.pdf",
    )
    client = Client()
    client.force_login(profile.user)

    response = _request_conversion(
        client,
        book_id=variant.book_id,
        source_variant=variant,
        target_format="txt",
    )
    assert response.status_code == 202

    payload = response.json()
    job = ProcessingJob.objects.get(job_id=payload["job_id"])
    assert job.state == ProcessingJob.JobState.FAILED
    assert "simulated converter failure" in str(job.details.get("error", ""))
    assert "output_variant_id" not in job.details


@pytest.mark.django_db(transaction=True)
def test_conversion_reuses_duplicate_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = get_or_create_profile("conversion-dup")
    storage = _configure_storage(tmp_path)
    monkeypatch.setattr(
        "ebook_server.core.conversion.CONVERTER_REGISTRY",
        {"calibre": StubConverter(b"stable-output", "calibre-v3")},
    )

    variant = _create_book_with_variant(
        storage,
        source_fmt="pdf",
        source_payload=b"%PDF-duplicate",
        source_file_name="source.pdf",
    )
    client = Client()
    client.force_login(profile.user)

    first = _request_conversion(
        client,
        book_id=variant.book_id,
        source_variant=variant,
        target_format="txt",
    )
    second = _request_conversion(
        client,
        book_id=variant.book_id,
        source_variant=variant,
        target_format="txt",
    )
    assert first.status_code == second.status_code == 202

    first_job = ProcessingJob.objects.get(job_id=first.json()["job_id"])
    second_job = ProcessingJob.objects.get(job_id=second.json()["job_id"])
    assert first_job.details["output_variant_id"] == second_job.details["output_variant_id"]
    assert (
        FileVariant.objects.filter(
            book=variant.book,
            source_variant=variant,
            format="txt",
            source_type=FileVariant.SourceType.CONVERTED,
        ).count()
        == 1
    )
