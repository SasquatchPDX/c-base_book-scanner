from __future__ import annotations

from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core.models import FileVariant, ImportJob, StorageLocation, get_or_create_profile
from ebook_server.storage import LocalFilesystemStorage


def _configure_storage(tmp_path: Path) -> None:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": "default",
        },
    )


def _build_epub(path: Path, title: str = "Sample EPUB") -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            (
                b'<?xml version="1.0"?>'
                b"<container "
                b'version="1.0" '
                b'xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                b"<rootfiles><rootfile full-path="
                b'"OEBPS/content.opf" media-type='
                b'"application/oebps-package+xml"/></rootfiles>'
                b"</container>"
            ),
        )
        archive.writestr(
            "OEBPS/content.opf",
            (
                '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{title}</dc:title>"
                f"<dc:creator>Jane Doe</dc:creator>"
                "</metadata>"
                "</package>"
            ).encode(),
        )


def _force_stub_provider(
    monkeypatch: pytest.MonkeyPatch,
    candidate_title: str,
    *,
    authors: list[str] | None = None,
    auto_accept_threshold: float = 0.5,
    review_threshold: float = 0.3,
    isbn: str | None = None,
) -> None:
    class StubProvider:
        provider_id = "stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, Any]]:
            del title, author, folder_hint
            payload: dict[str, Any] = {
                "title": candidate_title,
                "author_name": authors or [],
            }
            if isbn is not None:
                payload["isbn_13"] = [isbn]
            return [payload]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", auto_accept_threshold)
    monkeypatch.setattr("ebook_server.core.intake.REVIEW_SCORE", review_threshold)


@pytest.mark.django_db(transaction=True)
def test_epub_upload_registers_file_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_stub_provider(monkeypatch, candidate_title="Sample EPUB", authors=["Jane Doe"])
    _configure_storage(tmp_path)
    profile = get_or_create_profile("uploader")
    client = Client()
    client.force_login(profile.user)

    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root / "library-artifacts")),
    )

    epub_path = tmp_path / "book.epub"
    _build_epub(epub_path, "Sample EPUB")

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "sample.epub",
                epub_path.read_bytes(),
                content_type="application/epub+zip",
            ),
        },
    )
    assert response.status_code == 201

    payload = response.json()
    job = ImportJob.objects.get(pk=payload["job_id"])

    assert job.state == ImportJob.State.COMPLETE
    assert job.file_variant_id is not None
    assert job.file_variant is not None
    assert job.file_variant.storage_identifier
    storage = LocalFilesystemStorage(str(storage_root / "library-artifacts"))
    assert storage.exists(job.file_variant.storage_identifier)


@pytest.mark.django_db(transaction=True)
def test_pdf_upload_registers_file_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_stub_provider(monkeypatch, candidate_title="book", authors=["Unknown"])
    _configure_storage(tmp_path)
    profile = get_or_create_profile("pdf-user")
    client = Client()
    client.force_login(profile.user)

    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root / "library-artifacts")),
    )

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "book.pdf",
                b"%PDF-1.4\n%abc\n",
                content_type="application/pdf",
            ),
        },
    )
    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    assert job.state == ImportJob.State.COMPLETE
    assert job.file_variant is not None
    assert job.file_variant.mime_type == "application/pdf"


@pytest.mark.django_db(transaction=True)
def test_duplicate_upload_reuses_existing_physical_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_stub_provider(monkeypatch, candidate_title="first", authors=["Unknown"])
    _configure_storage(tmp_path)
    profile = get_or_create_profile("dup-user")
    client = Client()
    client.force_login(profile.user)

    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root / "library-artifacts")),
    )

    payload = b"%PDF-1.4\n%abc\n"

    first = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "first.pdf",
                payload,
                content_type="application/pdf",
            ),
        },
    )
    assert first.status_code == 201

    second = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "second.pdf",
                payload,
                content_type="application/pdf",
            ),
        },
    )
    assert second.status_code == 201

    first_job = ImportJob.objects.get(pk=first.json()["job_id"])
    second_job = ImportJob.objects.get(pk=second.json()["job_id"])

    assert first_job.file_variant_id is not None
    assert second_job.file_variant_id == first_job.file_variant_id
    assert second_job.storage_identifier == first_job.storage_identifier

    duplicate_count = FileVariant.objects.filter(sha256=first_job.sha256).count()
    assert duplicate_count == 1


@pytest.mark.django_db(transaction=True)
def test_dangerous_filename_is_rejected() -> None:
    profile = get_or_create_profile("badname")
    client = Client()
    client.force_login(profile.user)

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "..%2Fevil.pdf",
                b"%PDF-1.4",
                content_type="application/pdf",
            ),
        },
    )
    assert response.status_code == 400
    assert not ImportJob.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_unsupported_format_is_rejected() -> None:
    profile = get_or_create_profile("txt-user")
    client = Client()
    client.force_login(profile.user)

    response = client.post(
        "/imports/upload/",
        {"file": SimpleUploadedFile("notes.txt", b"hello", content_type="text/plain")},
    )
    assert response.status_code == 400
    assert not ImportJob.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_storage_failure_keeps_source_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FailingStorage:
        def store(self, source: str, destination: str) -> str:
            raise OSError("storage down")

    profile = get_or_create_profile("storage-fail")
    client = Client()
    client.force_login(profile.user)

    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: FailingStorage())

    response = client.post(
        "/imports/upload/",
        {"file": SimpleUploadedFile("broken.pdf", b"%PDF-1.4", content_type="application/pdf")},
    )

    assert response.status_code == 400
    job = ImportJob.objects.get()
    assert job.state == ImportJob.State.FAILED
    assert job.last_error
    assert Path(job.source_path).exists()
