from __future__ import annotations

import io
import re
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from django.conf import settings as django_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db.models.signals import post_save
from django.http import StreamingHttpResponse
from django.test import Client, override_settings
from PIL import Image
from pypdf import PdfWriter

from ebook_server.core import intake
from ebook_server.core.models import (
    CoverImage,
    FileVariant,
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage


def _configure_storage(tmp_path: Path) -> None:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": "default",
        },
    )


def _build_epub(
    path: Path,
    title: str = "Sample EPUB",
    *,
    cover_payload: bytes | None = None,
    creator: str = "Jane Doe",
) -> None:
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
        cover_metadata = '<meta name="cover" content="cover-image"/>' if cover_payload else ""
        cover_manifest = (
            '<manifest><item id="cover-image" href="images/cover.png" '
            'media-type="image/png" properties="cover-image"/></manifest>'
            if cover_payload
            else ""
        )
        archive.writestr(
            "OEBPS/content.opf",
            (
                '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
                f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
                f"<dc:title>{title}</dc:title>"
                f"<dc:creator>{creator}</dc:creator>"
                f"{cover_metadata}"
                "</metadata>"
                f"{cover_manifest}"
                "</package>"
            ).encode(),
        )
        if cover_payload is not None:
            archive.writestr("OEBPS/images/cover.png", cover_payload)


def _image_bytes(
    *,
    size: tuple[int, int] = (320, 480),
    color: tuple[int, int, int] = (30, 80, 150),
) -> bytes:
    stream = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(stream, format="PNG")
    return stream.getvalue()


def _force_stub_provider(
    monkeypatch: pytest.MonkeyPatch,
    candidate_title: str,
    *,
    authors: list[str] | None = None,
    auto_accept_threshold: float = 0.5,
    review_threshold: float = 0.3,
    isbn: str | None = None,
    strong_match_enabled: bool = True,
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
    monkeypatch.setattr(
        django_settings,
        "BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_ENABLED",
        strong_match_enabled,
    )


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
def test_candidate_below_auto_accept_threshold_requires_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Pins the plain threshold: with the exact-title + strong-author rule off, a 0.85
    # title+author score stays below 0.86 and must go to review.
    _force_stub_provider(
        monkeypatch,
        candidate_title="Borderline Match",
        authors=["Jane Doe"],
        auto_accept_threshold=0.86,
        review_threshold=0.55,
        strong_match_enabled=False,
    )
    _configure_storage(tmp_path)
    profile = get_or_create_profile("borderline-review-user")
    client = Client()
    client.force_login(profile.user)

    storage_root = tmp_path / "storage"
    storage = LocalFilesystemStorage(str(storage_root / "library-artifacts"))
    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: storage)

    epub_path = tmp_path / "borderline.epub"
    _build_epub(epub_path, "Borderline Match")
    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                epub_path.name,
                epub_path.read_bytes(),
                content_type="application/epub+zip",
            ),
        },
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    candidate = MetadataCandidate.objects.get(import_job=job)
    task = ReviewTask.objects.get(import_job=job)

    assert float(candidate.score) == pytest.approx(0.85)
    assert candidate.state == MetadataCandidate.CandidateState.REVIEW
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert job.file_variant_id is None
    assert task.state == ReviewTask.State.OPEN
    assert storage.exists(job.storage_identifier)


@pytest.mark.django_db(transaction=True)
def test_exact_title_and_author_without_isbn_auto_accepts_with_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Regression for #9: exact title + author, no ISBN/language/publisher, scored 0.85
    # and could never reach the 0.86 auto-accept threshold.
    _force_stub_provider(
        monkeypatch,
        candidate_title="Borderline Match",
        authors=["Jane Doe"],
        auto_accept_threshold=0.86,
        review_threshold=0.55,
    )
    _configure_storage(tmp_path)
    profile = get_or_create_profile("strong-match-user")
    client = Client()
    client.force_login(profile.user)
    storage = LocalFilesystemStorage(str(tmp_path / "storage" / "library-artifacts"))
    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: storage)

    epub_path = tmp_path / "strong.epub"
    _build_epub(epub_path, "Borderline Match")
    response = client.post(
        "/imports/upload/",
        {"file": SimpleUploadedFile(epub_path.name, epub_path.read_bytes())},
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    candidate = MetadataCandidate.objects.get(import_job=job)
    assert candidate.state == MetadataCandidate.CandidateState.MATCH
    assert float(candidate.score) == pytest.approx(0.86)
    evidence = candidate.raw_payload["_auto_accept_rule"]
    assert evidence["rule"] == "exact_title_strong_author"
    assert evidence["applied"] is True
    assert evidence["base_score"] == pytest.approx(0.85)
    assert job.state == ImportJob.State.COMPLETE
    assert job.book_id is not None
    assert not ReviewTask.objects.filter(import_job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_matching_placeholder_authors_do_not_trigger_promotion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An identical placeholder in the file and provider is not authorship evidence.
    _force_stub_provider(
        monkeypatch,
        candidate_title="Borderline Match",
        authors=["Unknown"],
        auto_accept_threshold=0.86,
        review_threshold=0.55,
    )
    _configure_storage(tmp_path)
    profile = get_or_create_profile("strong-match-guard-user")
    client = Client()
    client.force_login(profile.user)
    storage = LocalFilesystemStorage(str(tmp_path / "storage" / "library-artifacts"))
    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: storage)

    epub_path = tmp_path / "guarded.epub"
    _build_epub(epub_path, "Borderline Match", creator="Unknown")
    response = client.post(
        "/imports/upload/",
        {"file": SimpleUploadedFile("unknown.epub", epub_path.read_bytes())},
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    candidate = MetadataCandidate.objects.get(import_job=job)
    evidence = candidate.raw_payload["_auto_accept_rule"]
    assert evidence["applied"] is False
    assert float(candidate.score) < 0.86
    assert candidate.state == MetadataCandidate.CandidateState.REVIEW
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert ReviewTask.objects.filter(import_job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_strong_match_rule_does_not_promote_against_embedded_author(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The filename's author matches the provider, but the file's embedded author is
    # someone else: the rule itself refuses to promote, so the candidate stays at its
    # plain score and the import goes to review.
    _force_stub_provider(
        monkeypatch,
        candidate_title="Borderline Match",
        authors=["John Roe"],
        auto_accept_threshold=0.86,
        review_threshold=0.55,
    )
    _configure_storage(tmp_path)
    profile = get_or_create_profile("strong-match-conflict-user")
    client = Client()
    client.force_login(profile.user)
    storage = LocalFilesystemStorage(str(tmp_path / "storage" / "library-artifacts"))
    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: storage)

    epub_path = tmp_path / "conflict.epub"
    _build_epub(epub_path, "Borderline Match", creator="Jane Doe")
    response = client.post(
        "/imports/upload/",
        {"file": SimpleUploadedFile("John Roe - Borderline Match.epub", epub_path.read_bytes())},
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    candidate = MetadataCandidate.objects.get(import_job=job)
    evidence = candidate.raw_payload["_auto_accept_rule"]
    assert evidence["applied"] is False
    assert "author_evidence_conflict" in evidence["blocked_by"]
    assert float(candidate.score) < 0.86
    assert candidate.state == MetadataCandidate.CandidateState.REVIEW
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert ReviewTask.objects.filter(import_job=job).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("auto_accept_threshold", [0.8, 0.86])
def test_import_status_exposes_each_processing_phase(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    auto_accept_threshold: float,
) -> None:
    _force_stub_provider(
        monkeypatch,
        candidate_title="Observed Book",
        authors=["Jane Doe"],
        auto_accept_threshold=auto_accept_threshold,
        strong_match_enabled=False,
    )
    profile = get_or_create_profile("status-observer")
    client = Client()
    client.force_login(profile.user)
    epub_path = tmp_path / "observed.epub"
    _build_epub(epub_path, "Observed Book")
    states: list[str] = []

    def observe_status(sender: type[ImportJob], instance: ImportJob, **kwargs: Any) -> None:
        del sender, kwargs
        response = client.get(f"/imports/{instance.id}/")
        assert response.status_code == 200
        state = response.json()["state"]
        if not states or states[-1] != state:
            states.append(state)

    post_save.connect(observe_status, sender=ImportJob)
    try:
        with override_settings(APP_DATA_DIR=tmp_path):
            response = client.post(
                "/imports/upload/",
                {"file": SimpleUploadedFile(epub_path.name, epub_path.read_bytes())},
            )
        assert response.status_code == 201
    finally:
        post_save.disconnect(observe_status, sender=ImportJob)

    accepted = auto_accept_threshold == 0.8
    assert states == [
        ImportJob.State.DISCOVERED,
        ImportJob.State.HASHING,
        ImportJob.State.METADATA_EXTRACTION,
        ImportJob.State.SIGNAL_NORMALIZATION,
        ImportJob.State.EXTERNAL_IDENTIFICATION,
        ImportJob.State.MATCHED if accepted else ImportJob.State.LOW_CONFIDENCE,
        ImportJob.State.ARCHIVING,
        ImportJob.State.COMPLETE if accepted else ImportJob.State.LOW_CONFIDENCE,
    ]


@pytest.mark.django_db(transaction=True)
def test_epub_upload_extracts_normalized_cover_with_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_data = tmp_path / "cover-app-data"
    _force_stub_provider(monkeypatch, candidate_title="Covered EPUB", authors=["Jane Doe"])
    profile = get_or_create_profile("covered-epub-user")
    client = Client()
    client.force_login(profile.user)
    epub_path = tmp_path / "covered.epub"
    _build_epub(epub_path, "Covered EPUB", cover_payload=_image_bytes())

    with override_settings(APP_DATA_DIR=app_data):
        _configure_storage(tmp_path)
        response = client.post(
            "/imports/upload/",
            {
                "file": SimpleUploadedFile(
                    "covered.epub",
                    epub_path.read_bytes(),
                    content_type="application/epub+zip",
                ),
            },
        )

        assert response.status_code == 201
        job = ImportJob.objects.get(pk=response.json()["job_id"])
        assert job.file_variant is not None
        cover = CoverImage.objects.get(book=job.book, position=CoverImage.CoverPosition.FRONT)
        assert cover.source_variant_id == job.file_variant_id
        assert cover.extractor_tool == "zipfile+pillow"
        assert "Pillow" in cover.extractor_version
        assert cover.mime_type == "image/jpeg"
        assert job.book is not None
        job.book.refresh_from_db()
        assert job.book.front_cover_id == cover.id

        cover_response = client.get(f"/library/books/{job.book_id}/covers/front/")
        assert cover_response.status_code == 200
        assert cover_response["Content-Type"] == "image/jpeg"
        cover_stream = cast(StreamingHttpResponse, cover_response)
        cover_bytes = b"".join(cast(Iterable[bytes], cover_stream.streaming_content))
        assert len(cover_bytes) == cover.size_bytes
        with Image.open(io.BytesIO(cover_bytes)) as image:
            assert image.format == "JPEG"
            assert image.size == (320, 480)


@pytest.mark.django_db(transaction=True)
def test_pdf_upload_registers_file_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_stub_provider(monkeypatch, candidate_title="book", authors=["Jane Doe"])
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
def test_pdf_upload_renders_first_page_cover_with_pdfium(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_data = tmp_path / "pdf-cover-app-data"
    _force_stub_provider(monkeypatch, candidate_title="Rendered PDF", authors=["Jane Doe"])
    profile = get_or_create_profile("pdf-cover-user")
    client = Client()
    client.force_login(profile.user)
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    pdf_stream = io.BytesIO()
    writer.write(pdf_stream)

    with override_settings(APP_DATA_DIR=app_data):
        _configure_storage(tmp_path)
        response = client.post(
            "/imports/upload/",
            {
                "file": SimpleUploadedFile(
                    "Rendered PDF.pdf",
                    pdf_stream.getvalue(),
                    content_type="application/pdf",
                ),
            },
        )

        assert response.status_code == 201
        job = ImportJob.objects.get(pk=response.json()["job_id"])
        cover = CoverImage.objects.get(book=job.book, position=CoverImage.CoverPosition.FRONT)
        assert cover.source_variant_id == job.file_variant_id
        assert cover.extractor_tool == "pypdfium2+pillow"
        assert "pypdfium2" in cover.extractor_version


@pytest.mark.django_db(transaction=True)
def test_cbz_upload_uses_first_page_as_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_data = tmp_path / "cbz-cover-app-data"
    _force_stub_provider(monkeypatch, candidate_title="Covered Comic", authors=["Jane Doe"])
    profile = get_or_create_profile("cbz-cover-user")
    client = Client()
    client.force_login(profile.user)
    cbz_path = tmp_path / "Covered Comic.cbz"
    with ZipFile(cbz_path, "w") as archive:
        archive.writestr("001.png", _image_bytes(color=(170, 40, 30)))
        archive.writestr("002.png", _image_bytes(color=(20, 130, 60)))

    with override_settings(APP_DATA_DIR=app_data):
        _configure_storage(tmp_path)
        response = client.post(
            "/imports/upload/",
            {
                "file": SimpleUploadedFile(
                    cbz_path.name,
                    cbz_path.read_bytes(),
                    content_type="application/vnd.comicbook+zip",
                ),
            },
        )

        assert response.status_code == 201
        job = ImportJob.objects.get(pk=response.json()["job_id"])
        cover = CoverImage.objects.get(book=job.book, position=CoverImage.CoverPosition.FRONT)
        assert cover.source_variant_id == job.file_variant_id
        assert cover.extractor_tool == "zipfile+pillow"


@pytest.mark.django_db(transaction=True)
def test_cover_decompression_warning_does_not_fail_original_import(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_data = tmp_path / "cover-bomb-app-data"
    _force_stub_provider(monkeypatch, candidate_title="Large Cover EPUB", authors=["Jane Doe"])
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    profile = get_or_create_profile("cover-bomb-user")
    client = Client()
    client.force_login(profile.user)
    epub_path = tmp_path / "large-cover.epub"
    _build_epub(epub_path, "Large Cover EPUB", cover_payload=_image_bytes())

    with override_settings(APP_DATA_DIR=app_data):
        _configure_storage(tmp_path)
        response = client.post(
            "/imports/upload/",
            {
                "file": SimpleUploadedFile(
                    epub_path.name,
                    epub_path.read_bytes(),
                    content_type="application/epub+zip",
                ),
            },
        )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    assert job.state == ImportJob.State.COMPLETE
    assert not CoverImage.objects.filter(book=job.book).exists()


@pytest.mark.django_db(transaction=True)
def test_duplicate_upload_reuses_existing_physical_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _force_stub_provider(monkeypatch, candidate_title="first", authors=["Jane Doe"])
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


def test_book_title_with_slash_is_sanitized_before_naming_the_archive() -> None:
    """Regression: an Open Library omnibus/anthology title containing "/" (e.g.
    "Franchise / The Fun They Had / The Last Question / ...") made
    normalize_filename(f"{book.title}{suffix}") raise "dangerous filename" and fail the
    whole import, even though the title itself is perfectly legitimate - it's just not
    safe to use raw in a path component."""
    title = "Franchise / The Fun They Had / The Last Question"
    sanitized = intake._sanitize_title_for_filename(title)

    assert "/" not in sanitized
    assert (
        intake.normalize_filename(f"{sanitized}.txt")
        == "Franchise_-_The_Fun_They_Had_-_The_Last_Question.txt"
    )


@pytest.mark.django_db(transaction=True)
def test_upload_picker_and_help_follow_the_format_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A newly registered extension must appear without editing the template.
    monkeypatch.setitem(
        intake.SUPPORTED_FORMATS, ".newbook", intake.SupportedFormat("txt", "text/plain")
    )
    client = Client()
    client.force_login(get_or_create_profile("upload-form-user").user)

    response = client.get("/imports/upload/")

    assert response.status_code == 200
    html = response.content.decode()
    accepted = re.search(r'accept="([^"]+)"', html)
    assert accepted is not None
    assert set(accepted[1].split(",")) == set(intake.SUPPORTED_FORMATS)
    assert '<label for="book-file">Book file</label>' in html
    assert 'aria-describedby="book-file-help"' in html
    help_text = re.search(r'<small id="book-file-help">([^<]+)</small>', html)
    assert help_text is not None
    assert "Supported formats:" in help_text[1]
    for extension in intake.SUPPORTED_FORMATS:
        assert extension[1:].upper() in help_text[1]
    assert "Maximum" in help_text[1]
    assert "EPUB or PDF" not in html


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
    assert response.content.decode() == "unsupported file format: notes.docx"
    assert not ImportJob.objects.exists()


@pytest.mark.parametrize(
    ("filename", "expected_format"),
    [
        ("book.mobi", "mobi"),
        ("book.azw", "azw"),
        ("book.AZW3", "azw3"),
        ("book.prc", "prc"),
        ("book.rtf", "rtf"),
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
    palmdoc = bytearray(68)
    palmdoc[60:68] = b"TEXtREAd"
    fixtures = {
        "book.mobi": (bytes(mobi), "mobi"),
        "book.azw": (bytes(mobi), "azw"),
        "book.azw3": (bytes(mobi), "azw3"),
        "book.prc": (bytes(mobi), "prc"),
        "palmdoc.prc": (bytes(palmdoc), "prc"),
        "book.rtf": (b"{\\rtf1\\ansi Hello}", "rtf"),
        "bom.rtf": (b"\xef\xbb\xbf{\\rtf1 Hello}", "rtf"),
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
        ("fake.azw", "azw"),
        ("fake.azw3", "azw3"),
        ("fake.prc", "prc"),
        ("fake.rtf", "rtf"),
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
    _force_stub_provider(monkeypatch, candidate_title="Plain Story", authors=["Jane Doe"])
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
    assert MetadataCandidate.objects.filter(import_job=job, provider_id="stub").exists()


def test_plain_palmdoc_is_only_accepted_as_prc(tmp_path: Path) -> None:
    palmdoc = bytearray(68)
    palmdoc[60:68] = b"TEXtREAd"
    path = tmp_path / "book.azw3"
    path.write_bytes(bytes(palmdoc))
    with pytest.raises(intake.ValidationError):
        intake.validate_container_signature(str(path), "azw3")


@pytest.mark.django_db(transaction=True)
def test_azw3_upload_registers_file_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_storage(tmp_path)
    # Auto-accept needs provider authors (master's no_provider_authors block).
    _force_stub_provider(monkeypatch, candidate_title="Kindle Story", authors=["Jane Doe"])
    profile = get_or_create_profile("azw3-upload-user")
    client = Client()
    client.force_login(profile.user)
    payload = bytearray(256)
    payload[60:68] = b"BOOKMOBI"

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "Kindle Story.azw3",
                bytes(payload),
                content_type="application/octet-stream",
            )
        },
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    assert job.state == ImportJob.State.COMPLETE
    assert job.file_variant is not None
    assert job.file_variant.format == FileVariant.VariantFormat.AZW3
    assert job.file_variant.mime_type == "application/x-mobi8-ebook"


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
