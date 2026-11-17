"""Combined whole-book derivative of a confirmed multi-part work (#60)."""

from __future__ import annotations

import hashlib
import json
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from django.db import IntegrityError, transaction
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
        book.refresh_from_db()
        assert combined.storage_identifier.startswith(f"{book.archive_folder}/")
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


# --- review findings on PR #106 -----------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_output_identical_to_another_books_file_still_belongs_to_the_requested_book(
    tmp_path: Path,
) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, parts = _part_book(
            tmp_path,
            [("ch01.txt", b"First.\n", "Chapter 1"), ("ch02.txt", b"Second.\n", "Chapter 2")],
        )
        # Book 2 already holds a file with exactly the bytes the combine will produce.
        expected = b"First.\n\nSecond.\n"
        other_book = Book.objects.create(title="Some Other Upload")
        other = FileVariant.objects.create(
            book=other_book,
            format="txt",
            mime_type="text/plain",
            original_filename="other.txt",
            storage_location=parts[0].storage_location,
            storage_identifier="books/other/other.txt",
            size_bytes=len(expected),
            sha256=hashlib.sha256(expected).hexdigest(),
            source_type=FileVariant.SourceType.UPLOAD,
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )

        job = combine.request_combined_variant(book=book, target_format="txt")

        assert job.state == ProcessingJob.JobState.SUCCESS, job.details
        combined = FileVariant.objects.get(pk=job.details["output_variant_id"])
        assert combined.id != other.id
        assert combined.book_id == book.id
        assert combined.sha256 == other.sha256
        assert combined.storage_identifier != other.storage_identifier
        assert combined.source_type == FileVariant.SourceType.GENERATED
        assert combined.source_variant_id == parts[0].id
        assert str(combined.job_id) == str(job.job_id)
        assert combined.converter_options["operation"] == combine.OPERATION
        assert combined.converter_options["part_variant_ids"] == [part.id for part in parts]
        assert list(other_book.file_variants.values_list("id", flat=True)) == [other.id]
        storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
        assert storage.hash(combined.storage_identifier) == combined.sha256


@pytest.mark.django_db(transaction=True)
def test_part_grown_in_storage_is_not_staged_beyond_the_byte_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, parts = _part_book(
            tmp_path,
            [("ch01.txt", b"a\n", "Chapter 1"), ("ch02.txt", b"b\n", "Chapter 2")],
        )
        storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
        # Recorded as 2 bytes, but the stored file has grown to 100 bytes.
        storage.resolve_location(parts[0].storage_identifier).write_bytes(b"x" * 100)
        monkeypatch.setattr(combine, "MAX_COMBINED_INPUT_BYTES", 8)
        read_sizes: list[int] = []
        original_open = LocalFilesystemStorage.open

        def counting_open(self: LocalFilesystemStorage, location: str) -> Any:
            handle = original_open(self, location)
            original_read = handle.read

            def read(size: int = -1) -> bytes:
                chunk = original_read(size)
                read_sizes.append(len(chunk))
                return chunk

            handle.read = read  # type: ignore[method-assign]
            return handle

        monkeypatch.setattr(LocalFilesystemStorage, "open", counting_open)

        job = combine.request_combined_variant(book=book, target_format="txt")

        assert job.state == ProcessingJob.JobState.FAILED
        assert "larger than its recorded size" in job.details["error"]
        # At most one byte past the recorded size was read before failing.
        assert sum(read_sizes) <= parts[0].size_bytes + 1
        assert not FileVariant.objects.filter(part_index__isnull=True).exists()


@pytest.mark.django_db(transaction=True)
def test_recorded_total_over_the_budget_is_refused_before_staging(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, _parts = _part_book(
            tmp_path,
            [("ch01.txt", b"abcde\n", "Chapter 1"), ("ch02.txt", b"fghij\n", "Chapter 2")],
        )
        monkeypatch.setattr(combine, "MAX_COMBINED_INPUT_BYTES", 8)

        job = combine.request_combined_variant(book=book, target_format="txt")

        assert job.state == ProcessingJob.JobState.FAILED
        assert "size limit" in job.details["error"]


@pytest.mark.django_db(transaction=True)
def test_at_most_one_active_combine_job_per_book_and_format(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, parts = _part_book(
            tmp_path,
            [("ch01.pdf", _pdf_bytes(1), "Chapter 1"), ("ch02.pdf", _pdf_bytes(1), "Chapter 2")],
        )
        details = {"book_id": book.id, "target_format": "pdf", "part_variant_ids": []}
        ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.COMBINE_PARTS,
            state=ProcessingJob.JobState.QUEUED,
            file_variant=parts[0],
            details=details,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            ProcessingJob.objects.create(
                job_type=ProcessingJob.JobType.COMBINE_PARTS,
                state=ProcessingJob.JobState.RUNNING,
                file_variant=parts[0],
                details=details,
            )
        # Finished jobs and other formats or books are not constrained.
        ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.COMBINE_PARTS,
            state=ProcessingJob.JobState.FAILED,
            file_variant=parts[0],
            details=details,
        )
        ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.COMBINE_PARTS,
            state=ProcessingJob.JobState.QUEUED,
            file_variant=parts[0],
            details={**details, "target_format": "txt"},
        )


@pytest.mark.django_db(transaction=True)
def test_concurrent_first_request_reuses_the_job_that_won(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, parts = _part_book(
            tmp_path,
            [("ch01.pdf", _pdf_bytes(1), "Chapter 1"), ("ch02.pdf", _pdf_bytes(1), "Chapter 2")],
        )
        dispatched: list[str] = []
        monkeypatch.setattr(combine, "process_combine_job", dispatched.append)
        # Another request committed its job, but this request's check ran before that
        # commit became visible (the race the review found): the check sees nothing.
        winner = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.COMBINE_PARTS,
            state=ProcessingJob.JobState.QUEUED,
            file_variant=parts[0],
            details={"book_id": book.id, "target_format": "pdf"},
        )
        original_active = combine._active_combine_job
        calls: list[int] = []

        def racing_active(book_arg: Book, fmt: str) -> ProcessingJob | None:
            if not calls:
                calls.append(1)
                return None
            return original_active(book_arg, fmt)

        monkeypatch.setattr(combine, "_active_combine_job", racing_active)

        job = combine.request_combined_variant(book=book, target_format="pdf")

        assert (
            ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.COMBINE_PARTS).count() == 1
        )
        assert job.job_id == winner.job_id
        assert dispatched == []  # the losing request enqueued nothing


@pytest.mark.django_db(transaction=True)
def test_combined_work_includes_a_part_identical_to_another_books_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """#101 second review end to end: the duplicate chapter is part of the combined book."""
    from django.core.management import call_command

    from ebook_server.core.models import ImportJob, ReviewTask, SourceLocation
    from tests.test_multipart_works import (
        _admin_client,
        _library,
        _location,
        _other_book_with_identical_file,
        _split_book_folder,
        _stub_provider,
    )

    with override_settings(APP_DATA_DIR=tmp_path):
        _library(tmp_path, monkeypatch)
        _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
        root = tmp_path / "share"
        _folder, parts = _split_book_folder(root)
        other_variant = _other_book_with_identical_file(parts[0])
        location = _location(root, SourceLocation.TransferMode.COPY)
        call_command("ingest_sources", "--source-id", str(location.id), stdout=StringIO())
        task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
        client = _admin_client()
        assert (
            client.post(f"/reviews/{task.id}/multipart/acknowledge-incomplete/").status_code == 200
        )
        response = client.post(
            f"/reviews/{task.id}/manual-entry/",
            data=json.dumps({"title": "Learning Widgets"}),
            content_type="application/json",
        )
        assert response.status_code == 200, response.content
        book = Book.objects.get(title="Learning Widgets")
        assert ImportJob.objects.get(original_filename="ch01.txt").book_id == book.id

        response = client.post(
            f"/library/books/{book.id}/combine-parts/",
            data=json.dumps({"target_format": "txt"}),
            content_type="application/json",
        )
        assert response.status_code == 202, response.content
        payload = response.json()
        assert payload["state"] == ProcessingJob.JobState.SUCCESS, payload
        combined = FileVariant.objects.get(pk=payload["output_variant_id"])
        assert combined.book_id == book.id
        text = (tmp_path / "library-artifacts" / combined.storage_identifier).read_text(
            encoding="utf-8"
        )
        assert [line.split(",")[1].split(":")[0].strip() for line in text.split("\n\n")] == [
            "ch01.txt",
            "ch02.txt",
            "ch03.txt",
            "AppA.txt",
        ]
        other_variant.refresh_from_db()
        assert other_variant.book.file_variants.count() == 1


@pytest.mark.django_db(transaction=True)
def test_indexed_parts_combine_into_writable_book_folder(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        book, parts = _part_book(
            tmp_path,
            [
                ("ch01.txt", b"First chapter.", "Chapter 1"),
                ("ch02.txt", b"Second chapter.", "Chapter 2"),
            ],
        )
        share = tmp_path / "read-only-share"
        reference = StorageLocation.objects.create(
            identifier="referenced-parts",
            backend=StorageLocation.BackendType.LOCAL,
            base_path=str(share),
            read_only=True,
        )
        original_bytes = {}
        for part in parts:
            original = tmp_path / "library-artifacts" / part.storage_identifier
            referenced = share / part.storage_identifier
            referenced.parent.mkdir(parents=True, exist_ok=True)
            referenced.write_bytes(original.read_bytes())
            original_bytes[referenced] = referenced.read_bytes()
            part.storage_location = reference
            part.save(update_fields=["storage_location"])

        response = _client().post(
            f"/library/books/{book.id}/combine-parts/",
            data=json.dumps({"target_format": "txt"}),
            content_type="application/json",
        )
        assert response.status_code == 202, response.content
        payload = response.json()
        assert payload["state"] == ProcessingJob.JobState.SUCCESS, payload
        combined = FileVariant.objects.get(pk=payload["output_variant_id"])
        book.refresh_from_db()
        assert not combined.storage_location.read_only
        assert combined.storage_location.identifier == "default"
        assert combined.storage_identifier.startswith(f"{book.archive_folder}/")
        assert combined.source_variant_id == parts[0].pk
        assert combined.converter_options["part_variant_ids"] == [part.pk for part in parts]
        assert {path: path.read_bytes() for path in original_bytes} == original_bytes
        assert {path for path in share.rglob("*") if path.is_file()} == set(original_bytes)
