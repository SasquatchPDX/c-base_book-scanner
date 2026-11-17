"""Combined whole-book derivative of a confirmed multi-part work (#60)."""

from __future__ import annotations

import json
from io import BytesIO
from pathlib import Path

import pytest
from django.test import Client, override_settings
from pypdf import PdfReader, PdfWriter

from ebook_server.core import combine, intake
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage


def _pdf_bytes(pages: int) -> bytes:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    buffer = BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _part_book(
    tmp_path: Path, parts: list[tuple[str, bytes, str]]
) -> tuple[Book, list[FileVariant]]:
    """A confirmed split work: one Book, each part an archived FileVariant."""
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    book = Book.objects.create(title="Learning Widgets")
    variants = []
    for index, (name, payload, label) in enumerate(parts, start=1):
        staged = tmp_path / f"staged-{name}"
        staged.write_bytes(payload)
        identifier = storage.generate_destination(f"books/parts/{index}", name)
        storage.store(str(staged), identifier)
        fmt, mime = intake.detect_format(name)
        variants.append(
            FileVariant.objects.create(
                book=book,
                format=fmt,
                mime_type=mime,
                original_filename=name,
                storage_location=location,
                storage_identifier=identifier,
                size_bytes=len(payload),
                sha256=storage.hash(identifier),
                source_type=FileVariant.SourceType.INCOMING_FOLDER,
                processing_state=FileVariant.ProcessingState.COMPLETE,
                part_index=index,
                part_label=label,
            ),
        )
    return book, variants


def _client() -> Client:
    client = Client()
    client.force_login(get_or_create_profile("combine-reader").user)
    return client


@pytest.mark.django_db(transaction=True)
def test_combining_pdf_parts_creates_derived_variant_with_provenance(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, parts = _part_book(
            tmp_path,
            [
                ("ch01.pdf", _pdf_bytes(2), "Chapter 1"),
                ("ch02.pdf", _pdf_bytes(3), "Chapter 2"),
                ("AppA.pdf", _pdf_bytes(1), "Appendix A"),
            ],
        )
        originals = {part.id: part.sha256 for part in parts}
        client = _client()

        detail = client.get(f"/library/books/{book.id}/").json()
        assert detail["combine_options"] == [
            {
                "format": "pdf",
                "part_count": 3,
                "part_variant_ids": [part.id for part in parts],
                "combined_variant_id": None,
            }
        ]

        response = client.post(
            f"/library/books/{book.id}/combine-parts/",
            data=json.dumps({"target_format": "pdf"}),
            content_type="application/json",
        )
        assert response.status_code == 202, response.content
        payload = response.json()
        assert payload["state"] == ProcessingJob.JobState.SUCCESS, payload

        combined = FileVariant.objects.get(pk=payload["output_variant_id"])
        assert combined.part_index is None
        assert combined.source_type == FileVariant.SourceType.GENERATED
        assert combined.source_variant_id == parts[0].id
        assert combined.converter_tool == "pypdf"
        assert combined.converter_version.startswith("pypdf ")
        assert str(combined.job_id) == payload["job_id"]
        assert combined.converter_options["part_variant_ids"] == [part.id for part in parts]
        assert combined.converter_options["part_sha256"] == [part.sha256 for part in parts]

        storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
        with storage.open(combined.storage_identifier) as handle:
            reader = PdfReader(BytesIO(handle.read()))
        assert len(reader.pages) == 6
        titles = [getattr(item, "title", None) for item in reader.outline]
        assert titles == ["Chapter 1", "Chapter 2", "Appendix A"]

        # Originals untouched; the combined file is now a normal conversion source.
        for part in FileVariant.objects.filter(pk__in=originals):
            assert storage.hash(part.storage_identifier) == originals[part.id]
        from ebook_server.core import conversion

        sources = {
            option["source_variant_id"] for option in conversion.conversion_options_for_book(book)
        }
        assert sources <= {combined.id}

        # A second request reuses the existing combined file.
        again = client.post(
            f"/library/books/{book.id}/combine-parts/",
            data=json.dumps({"target_format": "pdf"}),
            content_type="application/json",
        ).json()
        assert again["output_variant_id"] == str(combined.id)
        assert FileVariant.objects.filter(source_type=FileVariant.SourceType.GENERATED).count() == 1
        status = client.get(f"/combine-jobs/{payload['job_id']}/").json()
        assert status["output_variant_id"] == str(combined.id)


@pytest.mark.django_db(transaction=True)
def test_combining_text_parts_decodes_and_joins_in_order(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, _parts = _part_book(
            tmp_path,
            [
                ("ch01.txt", "Erstes Kapitel über Größe.\n".encode("cp1252"), "Chapter 1"),
                ("ch02.txt", b"Second chapter.\n", "Chapter 2"),
            ],
        )
        job = combine.request_combined_variant(book=book, target_format="txt")

        assert job.state == ProcessingJob.JobState.SUCCESS, job.details
        combined = FileVariant.objects.get(pk=job.details["output_variant_id"])
        storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
        with storage.open(combined.storage_identifier) as handle:
            text = handle.read().decode("utf-8")
        assert text.index("Erstes Kapitel") < text.index("Second chapter")
        assert combined.converter_tool == "text-concat"


@pytest.mark.django_db(transaction=True)
def test_tampered_part_fails_the_combine_job_without_output(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, parts = _part_book(
            tmp_path,
            [("ch01.pdf", _pdf_bytes(1), "Chapter 1"), ("ch02.pdf", _pdf_bytes(1), "Chapter 2")],
        )
        FileVariant.objects.filter(pk=parts[1].pk).update(sha256="0" * 64)

        job = combine.request_combined_variant(book=book, target_format="pdf")

        assert job.state == ProcessingJob.JobState.FAILED
        assert "checksum" in job.details["error"]
        assert not FileVariant.objects.filter(part_index__isnull=True).exists()


@pytest.mark.django_db(transaction=True)
def test_uncombinable_formats_and_books_without_parts_are_refused(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, _parts = _part_book(
            tmp_path,
            [("ch01.pdf", _pdf_bytes(1), "Chapter 1"), ("ch02.pdf", _pdf_bytes(1), "Chapter 2")],
        )
        plain = Book.objects.create(title="Single File Book")
        client = _client()

        for target, book_id in (("epub", book.id), ("pdf", plain.id)):
            response = client.post(
                f"/library/books/{book_id}/combine-parts/",
                data=json.dumps({"target_format": target}),
                content_type="application/json",
            )
            assert response.status_code == 400
        assert not ProcessingJob.objects.exists()
