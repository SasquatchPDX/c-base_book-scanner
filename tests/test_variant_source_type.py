"""FileVariant.source_type records where an imported file came from (#20)."""

from __future__ import annotations

import importlib
from datetime import timedelta
from pathlib import Path

import pytest
from django.apps import apps as django_apps
from django.contrib.auth import get_user_model
from django.utils import timezone

from ebook_server.core import identification, intake
from ebook_server.core.models import (
    Book,
    FileVariant,
    ImportJob,
    ReviewTask,
    SourceLocation,
)
from ebook_server.storage import LocalFilesystemStorage


def _strong_candidate(title: str) -> identification.IdentificationCandidate:
    return identification.IdentificationCandidate(
        provider_id="stub",
        provider_ref="stub:1",
        title=title,
        authors=["Source Importer"],
        isbn10=None,
        isbn13=None,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=0.99,
        raw={},
    )


@pytest.fixture
def storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(tmp_path / "library-artifacts")),
    )


def _source_job(tmp_path: Path, source_type: str) -> ImportJob:
    root = tmp_path / source_type
    root.mkdir()
    book = root / "book.txt"
    book.write_text(f"A plain-text book that arrived via {source_type}.")
    location = SourceLocation.objects.create(
        name=f"src-{source_type}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        default_source_type=source_type,
    )
    return intake.create_source_import_job(book, location)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "source_type",
    [SourceLocation.SourceType.INCOMING_FOLDER, SourceLocation.SourceType.NETWORK_SOURCE],
)
def test_matched_source_import_records_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None, source_type: str
) -> None:
    del storage
    monkeypatch.setattr(
        identification,
        "resolve_candidates_with_report",
        lambda _signals: ([_strong_candidate("Book")], []),
    )
    job = _source_job(tmp_path, source_type)

    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.COMPLETE
    assert job.file_variant is not None
    assert job.file_variant.source_type == source_type


@pytest.mark.django_db(transaction=True)
def test_reviewed_source_import_records_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage: None
) -> None:
    del storage
    monkeypatch.setattr(identification, "resolve_candidates_with_report", lambda _signals: ([], []))
    job = _source_job(tmp_path, SourceLocation.SourceType.NETWORK_SOURCE)
    intake.process_import_job(str(job.id))
    job.refresh_from_db()
    assert job.state == ImportJob.State.LOW_CONFIDENCE

    variant = intake.resolve_import_with_review_data(
        job=job,
        review_task=ReviewTask.objects.get(import_job=job),
        book=Book.objects.create(title="Reviewed"),
    )

    assert variant.source_type == FileVariant.SourceType.NETWORK_SOURCE


@pytest.mark.django_db
def test_variant_source_type_mapping_keeps_uploads() -> None:
    job = ImportJob(source_type=ImportJob.SourceType.UPLOAD)
    assert intake.variant_source_type_for_job(job) == FileVariant.SourceType.UPLOAD


@pytest.mark.django_db
def test_migration_backfills_origin_from_first_import_job() -> None:
    migration = importlib.import_module(
        "ebook_server.core.migrations.0019_filevariant_source_type_origins"
    )
    user = get_user_model().objects.create(username="importer")
    from ebook_server.core.models import StorageLocation

    storage_location = StorageLocation.objects.create(identifier="default", base_path="default")
    book = Book.objects.create(title="Backfill")

    def _variant(name: str) -> FileVariant:
        return FileVariant.objects.create(
            book=book,
            format="txt",
            mime_type="text/plain",
            original_filename=name,
            storage_location=storage_location,
            storage_identifier=f"books/{name}",
            size_bytes=1,
            sha256=name.ljust(64, "0"),
            source_type=FileVariant.SourceType.UPLOAD,
        )

    def _job(source_type: str, variant: FileVariant, *, later: bool = False) -> None:
        job = ImportJob.objects.create(
            source_type=source_type,
            uploader=user,
            source_path="/x",
            original_filename="x.txt",
            file_variant=variant,
        )
        if later:
            ImportJob.objects.filter(pk=job.pk).update(
                created_at=timezone.now() + timedelta(minutes=1),
            )

    from_network = _variant("network")
    _job(ImportJob.SourceType.NETWORK_SOURCE, from_network)
    _job(ImportJob.SourceType.UPLOAD, from_network, later=True)  # later duplicate upload
    from_upload = _variant("upload")
    _job(ImportJob.SourceType.UPLOAD, from_upload)
    _job(ImportJob.SourceType.INCOMING_FOLDER, from_upload, later=True)  # later duplicate
    orphan = _variant("orphan")

    migration.backfill_import_origins(django_apps, None)

    for variant in (from_network, from_upload, orphan):
        variant.refresh_from_db()
    assert from_network.source_type == FileVariant.SourceType.NETWORK_SOURCE
    assert from_upload.source_type == FileVariant.SourceType.UPLOAD
    assert orphan.source_type == FileVariant.SourceType.UPLOAD
