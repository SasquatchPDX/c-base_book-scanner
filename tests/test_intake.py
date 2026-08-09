from __future__ import annotations

from pathlib import Path
from typing import Any
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core import intake
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
    profile = get_or_create_profile("docx-user")
    client = Client()
    client.force_login(profile.user)

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "notes.docx",
                b"not a supported office document",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            )
        },
    )
    assert response.status_code == 400
    assert not ImportJob.objects.exists()


@pytest.mark.parametrize(
    ("filename", "expected_format"),
    [
        ("book.mobi", "mobi"),
        ("book.txt", "txt"),
        ("book.html", "html"),
        ("book.htm", "html"),
        ("book.fb2", "fb2"),
        ("book.djvu", "djvu"),
        ("book.djv", "djvu"),
        ("book.cbz", "cbz"),
        ("book.cbr", "cbr"),
    ],
)
def test_extended_formats_are_detected(filename: str, expected_format: str) -> None:
    detected_format, mime_type = intake.detect_format(filename)
    assert detected_format == expected_format
    assert mime_type != "application/octet-stream"


def test_extended_format_signatures_are_validated(tmp_path: Path) -> None:
    mobi = bytearray(68)
    mobi[60:68] = b"BOOKMOBI"
    fixtures = {
        "book.mobi": (bytes(mobi), "mobi"),
        "book.txt": ("A café story".encode(), "txt"),
        "book.html": (b"<!doctype html><html><title>Book</title></html>", "html"),
        "book.fb2": (b"<FictionBook><description/></FictionBook>", "fb2"),
        "book.djvu": (b"AT&TFORM\x00\x00\x00\x00DJVU", "djvu"),
        "book.cbr": (b"Rar!\x1a\x07\x00", "cbr"),
    }
    for filename, (payload, detected_format) in fixtures.items():
        path = tmp_path / filename
        path.write_bytes(payload)
        intake.validate_container_signature(str(path), detected_format)

    cbz_path = tmp_path / "book.cbz"
    with ZipFile(cbz_path, "w") as archive:
        archive.writestr("001.png", b"page")
    intake.validate_container_signature(str(cbz_path), "cbz")


@pytest.mark.parametrize(
    ("filename", "detected_format"),
    [
        ("fake.mobi", "mobi"),
        ("fake.html", "html"),
        ("fake.fb2", "fb2"),
        ("fake.djvu", "djvu"),
        ("fake.cbz", "cbz"),
        ("fake.cbr", "cbr"),
    ],
)
def test_spoofed_extended_formats_are_rejected(
    tmp_path: Path,
    filename: str,
    detected_format: str,
) -> None:
    path = tmp_path / filename
    path.write_bytes(b"plain bytes with the wrong container signature")
    with pytest.raises(intake.ValidationError):
        intake.validate_container_signature(str(path), detected_format)


def test_zip_book_containers_reject_unsafe_paths_and_member_floods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_cbz = tmp_path / "unsafe.cbz"
    with ZipFile(unsafe_cbz, "w") as archive:
        archive.writestr("../001.jpg", b"page")
    with pytest.raises(intake.ValidationError, match="unsafe member path"):
        intake.validate_container_signature(str(unsafe_cbz), "cbz")

    crowded_cbz = tmp_path / "crowded.cbz"
    with ZipFile(crowded_cbz, "w") as archive:
        archive.writestr("001.jpg", b"page")
        archive.writestr("002.jpg", b"page")
    monkeypatch.setattr(intake, "MAX_ARCHIVE_MEMBERS", 1)
    with pytest.raises(intake.ValidationError, match="too many members"):
        intake.validate_container_signature(str(crowded_cbz), "cbz")


def test_html_fb2_and_cbz_metadata_use_format_aware_parsers(tmp_path: Path) -> None:
    html_path = tmp_path / "web-book.html"
    html_path.write_text(
        """<!doctype html><html><head><title>HTML Book</title>
        <meta name="author" content="Helen Hypertext">
        <meta name="dc.identifier" content="9780306406157">
        </head><body></body></html>""",
        encoding="utf-8",
    )
    html_metadata = intake.extract_metadata(
        str(html_path),
        "html",
        original_filename=html_path.name,
    )
    assert html_metadata["title"] == "HTML Book"
    assert html_metadata["authors"] == ["Helen Hypertext"]
    assert html_metadata["identifier"] == "9780306406157"

    fb2_path = tmp_path / "fiction.fb2"
    fb2_path.write_text(
        """<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0">
        <description><title-info><genre>science</genre><author>
        <first-name>Frank</first-name><last-name>Fiction</last-name></author>
        <book-title>FB2 Book</book-title><lang>en</lang>
        <annotation><p>Structured summary.</p></annotation>
        </title-info></description><body><section/></body></FictionBook>""",
        encoding="utf-8",
    )
    fb2_metadata = intake.extract_metadata(
        str(fb2_path),
        "fb2",
        original_filename=fb2_path.name,
    )
    assert fb2_metadata["title"] == "FB2 Book"
    assert fb2_metadata["authors"] == ["Frank Fiction"]
    assert fb2_metadata["language"] == "en"

    cbz_path = tmp_path / "comic.cbz"
    with ZipFile(cbz_path, "w") as archive:
        archive.writestr("001.jpg", b"page")
        archive.writestr(
            "ComicInfo.xml",
            """<ComicInfo><Title>CBZ Book</Title><Writer>Casey Comics</Writer>
            <LanguageISO>en</LanguageISO><Publisher>Panel Press</Publisher></ComicInfo>""",
        )
    cbz_metadata = intake.extract_metadata(
        str(cbz_path),
        "cbz",
        original_filename=cbz_path.name,
    )
    assert cbz_metadata["title"] == "CBZ Book"
    assert cbz_metadata["authors"] == ["Casey Comics"]
    assert cbz_metadata["publisher"] == "Panel Press"


@pytest.mark.django_db(transaction=True)
def test_txt_upload_uses_normal_ingestion_and_external_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_storage(tmp_path)
    _force_stub_provider(monkeypatch, candidate_title="Plain Story")
    profile = get_or_create_profile("txt-upload-user")
    client = Client()
    client.force_login(profile.user)

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "Plain Story.txt",
                "A plain-text book with café characters.".encode(),
                content_type="text/plain",
            )
        },
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    assert job.state == ImportJob.State.COMPLETE
    assert job.file_variant is not None
    assert job.file_variant.format == FileVariant.VariantFormat.TXT


@pytest.mark.django_db(transaction=True)
def test_spoofed_pdf_extension_is_rejected() -> None:
    profile = get_or_create_profile("spoofed-pdf-user")
    client = Client()
    client.force_login(profile.user)

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "not-really.pdf",
                b"plain text pretending to be a document",
                content_type="application/pdf",
            ),
        },
    )

    assert response.status_code == 400
    job = ImportJob.objects.get(original_filename="not-really.pdf")
    assert job.state == ImportJob.State.FAILED
    assert "not a PDF" in job.last_error


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


@pytest.mark.django_db(transaction=True)
def test_epub_archive_bomb_metadata_is_rejected(
    tmp_path: Path,
) -> None:
    _configure_storage(tmp_path)
    profile = get_or_create_profile("unsafe-epub-uploader")
    client = Client()
    client.force_login(profile.user)
    epub_path = tmp_path / "unsafe.epub"
    with ZipFile(epub_path, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("META-INF/container.xml", b"A" * (3 * 1024 * 1024))

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "unsafe.epub",
                epub_path.read_bytes(),
                content_type="application/epub+zip",
            ),
        },
    )

    assert response.status_code == 400
    job = ImportJob.objects.get(original_filename="unsafe.epub")
    assert job.state == ImportJob.State.FAILED
    assert "safe size limit" in job.last_error
