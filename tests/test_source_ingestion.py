from __future__ import annotations

import hashlib
import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings

from ebook_server.core import intake
from ebook_server.core.intake import (
    create_source_import_job,
    discover_source_jobs_for_location,
)
from ebook_server.core.models import (
    Book,
    FileVariant,
    ImportJob,
    ReviewTask,
    SourceLocation,
    StorageLocation,
)
from ebook_server.storage import LocalFilesystemStorage


def _configure_storage(tmp_path: Path) -> None:
    from ebook_server.core.models import StorageLocation

    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": "default",
        },
    )


def _stub_provider(monkeypatch: pytest.MonkeyPatch, *, title: str) -> None:
    expected_title = title

    class StubProvider:
        provider_id = "stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, object]]:
            del title, author, folder_hint, isbn
            return [
                {
                    "title": expected_title,
                    "author_name": ["Source Importer"],
                    "publisher": "CLI Test Press",
                }
            ]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )


def _create_source_root(tmp_root: Path, *, recursive: bool = True) -> SourceLocation:
    return SourceLocation.objects.create(
        name=f"src-{tmp_root.name}-{recursive}",
        root_path=str(tmp_root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        recursive=recursive,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
        is_active=True,
    )


@pytest.mark.django_db(transaction=True)
def test_preview_reports_supported_source_files_only(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "incoming"
    source_root.mkdir()
    source_file = source_root / "book.pdf"
    text_file = source_root / "notes.txt"
    other_file = source_root / "ignore.docx"
    source_file.write_bytes(b"%PDF-1.4")
    text_file.write_text("A supported plain-text book.")
    other_file.write_text("skip me")

    location = _create_source_root(source_root)
    stdout = StringIO()
    call_command(
        "ingest_sources",
        "--source-id",
        str(location.id),
        "--preview",
        "--json",
        stdout=stdout,
    )
    payload = json.loads(stdout.getvalue())

    importable = {item["path"] for item in payload if item.get("status") == "importable"}
    assert importable == {str(source_file), str(text_file)}


@pytest.mark.django_db(transaction=True)
def test_preview_lists_skipped_unsupported_files_with_extension_counts(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "incoming-mixed"
    (source_root / "nested").mkdir(parents=True)
    supported = source_root / "book.pdf"
    supported.write_bytes(b"%PDF-1.4")
    skipped_paths = {
        source_root / "manual.chm",
        source_root / "nested" / "other.chm",
        source_root / "old.lit",
        source_root / "README",
    }
    for path in skipped_paths:
        path.write_bytes(b"unsupported")

    location = _create_source_root(source_root)
    stdout = StringIO()
    call_command(
        "ingest_sources",
        "--source-id",
        str(location.id),
        "--preview",
        "--json",
        stdout=stdout,
    )
    payload = json.loads(stdout.getvalue())

    skipped = [item for item in payload if item.get("status") == "skipped"]
    assert {item["path"] for item in skipped} == {str(path) for path in skipped_paths}
    assert all(item["reason"] == "unsupported format" for item in skipped)
    summaries = [item["summary"] for item in payload if "summary" in item]
    assert summaries == [
        {
            "importable": 1,
            "skipped": 4,
            "skipped_audio": 0,
            "skipped_by_extension": {".chm": 2, "(no extension)": 1, ".lit": 1},
        }
    ]

    text_out = StringIO()
    call_command(
        "ingest_sources",
        "--source-id",
        str(location.id),
        "--preview",
        stdout=text_out,
    )
    text = text_out.getvalue()
    assert f"SKIPPED (unsupported format: .lit) => {source_root / 'old.lit'}" in text
    assert "importable=1 skipped=4 (.chm=2, (no extension)=1, .lit=1)" in text
    assert not ImportJob.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_discovery_reports_skipped_unsupported_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "incoming-skip-log"
    source_root.mkdir()
    (source_root / "book.pdf").write_bytes(b"%PDF-1.4")
    (source_root / "notes.doc").write_bytes(b"legacy word file")
    location = _create_source_root(source_root)
    events: list[tuple[str, dict[str, object]]] = []

    class RecordingLogger:
        def warning(self, event: str, **kwargs: object) -> None:
            events.append((event, kwargs))

    monkeypatch.setattr(intake, "logger", RecordingLogger())

    jobs = discover_source_jobs_for_location(location)

    assert [job.original_filename for job in jobs] == ["book.pdf"]
    assert events == [
        (
            "source_files_skipped_unsupported",
            {
                "source_location_id": str(location.id),
                "skipped_count": 1,
                "skipped_by_extension": {".doc": 1},
            },
        )
    ]


@pytest.mark.django_db(transaction=True)
def test_kindle_and_rtf_source_files_are_discovered_for_import(tmp_path: Path) -> None:
    source_root = tmp_path / "incoming-kindle"
    source_root.mkdir()
    for name in ("a.azw", "b.azw3", "c.prc", "d.rtf"):
        (source_root / name).write_bytes(b"payload")
    location = _create_source_root(source_root)

    jobs = discover_source_jobs_for_location(location)

    assert sorted(job.original_filename for job in jobs) == ["a.azw", "b.azw3", "c.prc", "d.rtf"]


@pytest.mark.django_db(transaction=True)
def test_copy_mode_source_import_keeps_source_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "incoming-copy"
    source_root.mkdir()
    source_file = source_root / "book.pdf"
    source_file.write_bytes(b"%PDF-1.4")
    storage_root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root)),
    )
    _configure_storage(tmp_path)
    _stub_provider(monkeypatch, title="book")
    location = _create_source_root(source_root)
    location.transfer_mode = SourceLocation.TransferMode.COPY
    location.save(update_fields=["transfer_mode"])

    call_command("ingest_sources", "--source-id", str(location.id))

    job = ImportJob.objects.get(source_path=str(source_file.resolve()))
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert ReviewTask.objects.get(import_job=job).state == ReviewTask.State.OPEN
    assert source_file.exists()
    assert job.storage_identifier
    assert LocalFilesystemStorage(str(storage_root)).hash(job.storage_identifier) == job.sha256


@pytest.mark.django_db(transaction=True)
def test_move_mode_source_import_removes_source_after_verified_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "incoming-move"
    source_root.mkdir()
    source_file = source_root / "book.pdf"
    source_file.write_bytes(b"%PDF-1.4")
    storage_root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root)),
    )
    _configure_storage(tmp_path)
    _stub_provider(monkeypatch, title="book")

    location = _create_source_root(source_root)
    location.transfer_mode = SourceLocation.TransferMode.MOVE
    location.save(update_fields=["transfer_mode"])

    call_command("ingest_sources", "--source-id", str(location.id))

    job = ImportJob.objects.get(source_path=str(source_file.resolve()))
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert ReviewTask.objects.get(import_job=job).state == ReviewTask.State.OPEN
    assert not source_file.exists()
    assert job.storage_identifier
    assert LocalFilesystemStorage(str(storage_root)).hash(job.storage_identifier) == job.sha256


@pytest.mark.django_db(transaction=True)
def test_recursive_scan_includes_nested_paths(tmp_path: Path) -> None:
    source_root = tmp_path / "incoming-recursive"
    source_root.mkdir()
    nested = source_root / "nested"
    nested.mkdir()
    nested_file = nested / "nested-book.pdf"
    nested_file.write_bytes(b"%PDF-1.4")
    source_file = source_root / "root-book.pdf"
    source_file.write_bytes(b"%PDF-1.4")

    location = _create_source_root(source_root, recursive=True)

    discovered = list(discover_source_jobs_for_location(location))

    assert {str(source_file), str(nested_file)} <= {str(job.source_path) for job in discovered}


@pytest.mark.django_db(transaction=True)
def test_duplicate_files_across_locations_reuse_variant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "incoming-dup-1"
    second_root = tmp_path / "incoming-dup-2"
    first_root.mkdir()
    second_root.mkdir()
    content = b"%PDF-1.4"
    first_file = first_root / "book-one.pdf"
    second_file = second_root / "book-two.pdf"
    first_file.write_bytes(content)
    second_file.write_bytes(content)
    storage_root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root)),
    )
    _configure_storage(tmp_path)
    _stub_provider(monkeypatch, title="book")

    first_location = _create_source_root(first_root)
    second_location = _create_source_root(second_root)

    call_command(
        "ingest_sources",
        "--source-id",
        str(first_location.id),
        "--source-id",
        str(second_location.id),
    )

    first_job = ImportJob.objects.get(source_path=str(first_file.resolve()))
    second_job = ImportJob.objects.get(source_path=str(second_file.resolve()))

    assert first_job.state in {ImportJob.State.COMPLETE, ImportJob.State.LOW_CONFIDENCE}
    assert second_job.state == first_job.state
    assert first_job.file_variant_id == second_job.file_variant_id


@pytest.mark.django_db(transaction=True)
def test_failed_source_import_leaves_file_recoverable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "incoming-fail"
    source_root.mkdir()
    source_file = source_root / "book.pdf"
    source_file.write_bytes(b"%PDF-1.4")
    source_location = _create_source_root(source_root)
    source_location.transfer_mode = SourceLocation.TransferMode.MOVE
    source_location.save(update_fields=["transfer_mode"])
    job = create_source_import_job(
        source_file,
        source_location,
        uploader=intake._make_system_user(),
    )

    def _fail_archive(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("archive unavailable")

    monkeypatch.setattr("ebook_server.core.intake._archive_source", _fail_archive)
    with pytest.raises(RuntimeError):
        intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.FAILED
    assert source_file.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("transfer_mode", SourceLocation.TransferMode.values)
@pytest.mark.parametrize("archive_state", ["valid", "corrupt", "missing"])
def test_duplicate_cleanup_verifies_declared_backend(
    tmp_path: Path,
    transfer_mode: str,
    archive_state: str,
) -> None:
    payload = b"A book already archived on a separate network mount."
    source_root = tmp_path / "incoming"
    source_root.mkdir()
    source_file = source_root / "book.txt"
    source_file.write_bytes(payload)
    source_location = _create_source_root(source_root)
    source_location.transfer_mode = transfer_mode
    source_location.save(update_fields=["transfer_mode"])
    archive_location = StorageLocation.objects.create(
        identifier="separate-mount",
        backend=StorageLocation.BackendType.NFS,
        base_path=str(tmp_path / "network-mount"),
    )
    archive = LocalFilesystemStorage(archive_location.base_path)
    destination = "books/existing/book.txt"
    if archive_state != "missing":
        archive.store(str(source_file), destination)
        if archive_state == "corrupt":
            archive.resolve_location(destination).write_bytes(b"corrupt archive")

    # A valid file at the same ID on the default backend must not authorize deletion.
    default = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    default.store(str(source_file), destination)
    variant = FileVariant.objects.create(
        book=Book.objects.create(title="Already identified"),
        format="txt",
        mime_type="text/plain",
        original_filename=source_file.name,
        storage_location=archive_location,
        storage_identifier=destination,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )
    job = create_source_import_job(
        source_file, source_location, uploader=intake._make_system_user()
    )
    with override_settings(APP_DATA_DIR=tmp_path):
        intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.COMPLETE
    assert job.file_variant_id == variant.id
    assert job.storage_location_id == archive_location.id
    assert FileVariant.objects.count() == 1
    should_remove = transfer_mode == SourceLocation.TransferMode.MOVE and archive_state == "valid"
    assert source_file.exists() is not should_remove
    if source_file.exists():
        assert source_file.read_bytes() == payload
