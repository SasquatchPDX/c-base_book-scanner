from __future__ import annotations

import json
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command

from ebook_server.core import intake
from ebook_server.core.intake import (
    create_source_import_job,
    discover_source_jobs_for_location,
)
from ebook_server.core.models import ImportJob, SourceLocation
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
    other_file = source_root / "ignore.txt"
    source_file.write_bytes(b"%PDF-1.4")
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

    assert len(payload) == 1
    assert payload[0]["path"] == str(source_file)


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
    assert job.state == ImportJob.State.COMPLETE
    assert source_file.exists()
    assert job.storage_identifier
    assert (storage_root / job.storage_identifier).exists()


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
    assert job.state == ImportJob.State.COMPLETE
    assert not source_file.exists()
    assert job.storage_identifier
    assert (storage_root / job.storage_identifier).exists()


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

    def _fail_archive(*_args, **_kwargs):
        raise RuntimeError("archive unavailable")

    monkeypatch.setattr("ebook_server.core.intake._archive_source", _fail_archive)
    with pytest.raises(RuntimeError):
        intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.FAILED
    assert source_file.exists()
