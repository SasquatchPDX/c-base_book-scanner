"""Index-in-place sources: reference files where they live, never copy them (#7)."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Iterable
from datetime import datetime
from io import StringIO
from pathlib import Path
from typing import cast

import pytest
from django.conf import settings as django_settings
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.http import StreamingHttpResponse
from django.test import Client

from ebook_server.core import identification, intake, referenced_files
from ebook_server.core.book_deletion import delete_book_safely
from ebook_server.core.models import (
    Book,
    FileVariant,
    ImportJob,
    ProcessingJob,
    ReviewTask,
    SourceLocation,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.core.storage_registry import (
    backend_for_location,
    default_archive_location,
    writable_location_for,
)
from ebook_server.storage import (
    LocalFilesystemStorage,
    ReadOnlyFilesystemStorage,
    ReadOnlyStorageError,
)

BOOK_TEXT = "A plain-text book that already lives on the collection share.\n" * 20


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
def app_storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Attached storage root; the archive lives under it as library-artifacts/."""
    root = tmp_path / "attached"
    root.mkdir()
    monkeypatch.setattr(django_settings, "APP_DATA_DIR", root, raising=False)
    return root


@pytest.fixture
def matched(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        identification,
        "resolve_candidates",
        lambda _signals: [_strong_candidate("Shared Book")],
    )


def _index_source(tmp_path: Path, name: str = "collection") -> tuple[SourceLocation, Path]:
    root = tmp_path / name
    (root / "Some Author" / "Shared Book (12)").mkdir(parents=True)
    book = root / "Some Author" / "Shared Book (12)" / "Shared Book.txt"
    book.write_text(BOOK_TEXT, encoding="utf-8")
    location = SourceLocation.objects.create(
        name=f"src-{name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
    )
    return location, book


def _archive_files(app_storage: Path) -> list[Path]:
    archive = app_storage / "library-artifacts"
    if not archive.exists():
        return []
    return [path for path in archive.rglob("*") if path.is_file()]


def _import(location: SourceLocation, book: Path) -> ImportJob:
    job = intake.create_source_import_job(book, location)
    intake.process_import_job(str(job.id))
    job.refresh_from_db()
    return job


def _download(variant: FileVariant) -> tuple[int, bytes]:
    profile = get_or_create_profile(f"reader-{variant.pk}")
    client = Client()
    client.force_login(profile.user)
    response = client.get(f"/library/books/{variant.book_id}/download/?variant_id={variant.pk}")
    if response.status_code != 200:
        return response.status_code, response.content
    stream = cast(StreamingHttpResponse, response)
    return 200, b"".join(cast(Iterable[bytes], stream.streaming_content))


@pytest.mark.django_db(transaction=True)
def test_matched_index_import_references_file_without_copying(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched
    location, book = _index_source(tmp_path)
    original_bytes = book.read_bytes()

    job = _import(location, book)

    assert job.state == ImportJob.State.COMPLETE
    variant = FileVariant.objects.select_related("storage_location").get(pk=job.file_variant_id)
    assert variant.storage_location.read_only
    assert variant.storage_identifier == "Some Author/Shared Book (12)/Shared Book.txt"
    assert variant.sha256 == hashlib.sha256(original_bytes).hexdigest()
    assert variant.size_bytes == len(original_bytes)
    assert variant.source_mtime_ns == book.stat().st_mtime_ns
    assert variant.reference_state == FileVariant.ReferenceState.OK
    # The source is untouched and no second copy of the book exists in the archive.
    assert book.read_bytes() == original_bytes
    assert not any(path.read_bytes() == original_bytes for path in _archive_files(app_storage))
    location.refresh_from_db()
    assert location.index_storage_location == variant.storage_location
    assert _download(variant) == (200, original_bytes)


@pytest.mark.django_db(transaction=True)
def test_low_confidence_index_import_is_referenced_after_review(
    tmp_path: Path, app_storage: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(identification, "resolve_candidates", lambda _signals: [])
    location, book = _index_source(tmp_path)

    job = _import(location, book)

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.storage_location is not None and job.storage_location.read_only
    variant = intake.resolve_import_with_review_data(
        job=job,
        review_task=ReviewTask.objects.get(import_job=job),
        book=Book.objects.create(title="Reviewed"),
    )
    assert variant.storage_location.read_only
    assert variant.source_mtime_ns == book.stat().st_mtime_ns
    assert book.exists()
    assert _archive_files(app_storage) == []


@pytest.mark.django_db(transaction=True)
def test_index_rejects_file_outside_configured_storage_location(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    location.index_storage_location = StorageLocation.objects.create(
        identifier="other-share",
        base_path=str(elsewhere),
        read_only=True,
    )
    location.save()
    job = intake.create_source_import_job(book, location)

    with pytest.raises(referenced_files.ReferencedFileError):
        intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.FAILED
    assert book.exists()
    assert not FileVariant.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_drift_check_flags_changed_and_missing_files_and_stops_serving(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    variant = FileVariant.objects.get(pk=_import(location, book).file_variant_id)

    book.write_text(BOOK_TEXT.upper(), encoding="utf-8")  # same size, new bytes
    stat = book.stat()
    os.utime(book, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    with pytest.raises(CommandError):
        call_command("check_referenced_files", stdout=StringIO(), stderr=StringIO())
    variant.refresh_from_db()
    assert variant.reference_state == FileVariant.ReferenceState.CHANGED
    assert _download(variant)[0] == 409
    assert book.exists()  # flagged, never deleted

    book.unlink()
    report = referenced_files.check_referenced_files()
    assert report.missing == [variant.pk]
    variant.refresh_from_db()
    assert variant.reference_state == FileVariant.ReferenceState.MISSING

    # Restoring the recorded content (even with a new mtime) clears the flag.
    book.write_text(BOOK_TEXT, encoding="utf-8")
    report = referenced_files.check_referenced_files()
    assert report.restored == [variant.pk]
    variant.refresh_from_db()
    assert variant.reference_state == FileVariant.ReferenceState.OK
    assert variant.source_mtime_ns == book.stat().st_mtime_ns
    assert _download(variant)[0] == 200


@pytest.mark.django_db(transaction=True)
def test_download_detects_change_before_any_check_runs(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    variant = FileVariant.objects.get(pk=_import(location, book).file_variant_id)

    book.write_text(BOOK_TEXT + "appended", encoding="utf-8")

    assert _download(variant)[0] == 409
    variant.refresh_from_db()
    assert variant.reference_state == FileVariant.ReferenceState.CHANGED


@pytest.mark.django_db(transaction=True)
def test_touched_but_identical_file_stays_ok(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    variant = FileVariant.objects.get(pk=_import(location, book).file_variant_id)
    stat = book.stat()
    os.utime(book, ns=(stat.st_atime_ns, stat.st_mtime_ns + 7_000_000_000))

    report = referenced_files.check_referenced_files()

    assert report.ok == 1 and not report.changed
    variant.refresh_from_db()
    assert variant.reference_state == FileVariant.ReferenceState.OK
    assert variant.source_mtime_ns == book.stat().st_mtime_ns


@pytest.mark.django_db(transaction=True)
def test_unmounted_share_reports_error_without_flagging(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    variant = FileVariant.objects.get(pk=_import(location, book).file_variant_id)
    share = variant.storage_location
    share.base_path = str(tmp_path / "not-mounted")
    share.save()

    report = referenced_files.check_referenced_files()

    assert report.errors and not report.missing
    variant.refresh_from_db()
    assert variant.reference_state == FileVariant.ReferenceState.OK


@pytest.mark.django_db(transaction=True)
def test_duplicate_detection_spans_archived_and_referenced_variants(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    referenced = FileVariant.objects.get(pk=_import(location, book).file_variant_id)

    move_root = tmp_path / "move-incoming"
    move_root.mkdir()
    move_copy = move_root / "same.txt"
    move_copy.write_bytes(book.read_bytes())
    move_source = SourceLocation.objects.create(
        name="move-src",
        root_path=str(move_root),
        transfer_mode=SourceLocation.TransferMode.MOVE,
    )

    job = _import(move_source, move_copy)

    assert job.state == ImportJob.State.COMPLETE
    assert job.file_variant_id == referenced.pk
    assert FileVariant.objects.count() == 1
    # A referenced duplicate lives on storage the app does not own: keep the MOVE source.
    assert move_copy.exists()


@pytest.mark.django_db(transaction=True)
def test_index_import_of_archived_duplicate_reuses_archived_variant(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched
    copy_root = tmp_path / "copy-incoming"
    copy_root.mkdir()
    first = copy_root / "first.txt"
    first.write_text(BOOK_TEXT, encoding="utf-8")
    copy_source = SourceLocation.objects.create(
        name="copy-src", root_path=str(copy_root), transfer_mode=SourceLocation.TransferMode.COPY
    )
    monkeypatch_storage = LocalFilesystemStorage(str(app_storage / "library-artifacts"))
    archived = FileVariant.objects.get(pk=_import(copy_source, first).file_variant_id)
    assert monkeypatch_storage.exists(archived.storage_identifier)

    location, book = _index_source(tmp_path)
    job = _import(location, book)

    assert job.file_variant_id == archived.pk
    assert FileVariant.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_derived_artifacts_go_to_writable_archive_not_the_share(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    variant = FileVariant.objects.select_related("storage_location").get(
        pk=_import(location, book).file_variant_id
    )

    target = writable_location_for(variant.storage_location)

    assert target == default_archive_location()
    assert not target.read_only
    writable = StorageLocation.objects.create(identifier="rw", base_path=str(tmp_path / "rw"))
    assert writable_location_for(writable) == writable
    backend = backend_for_location(variant.storage_location)
    assert isinstance(backend, ReadOnlyFilesystemStorage)
    for call in (
        lambda: backend.store(str(book), "x.txt"),
        lambda: backend.copy(variant.storage_identifier, "x.txt"),
        lambda: backend.move(variant.storage_identifier, "x.txt"),
        lambda: backend.delete(variant.storage_identifier),
        lambda: backend.generate_destination("books", "x.txt"),
    ):
        with pytest.raises(ReadOnlyStorageError):
            call()
    assert book.exists()


@pytest.mark.django_db(transaction=True)
def test_conversion_of_referenced_variant_writes_to_archive(
    tmp_path: Path, app_storage: Path, matched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del matched
    from ebook_server.core import conversion
    from tests.test_conversion import StubConverter

    monkeypatch.setattr(
        conversion, "CONVERTER_REGISTRY", {"pandoc": StubConverter(b"converted epub")}
    )
    location, book = _index_source(tmp_path)
    variant = FileVariant.objects.get(pk=_import(location, book).file_variant_id)
    share_files_before = sorted(p for p in Path(location.root_path).rglob("*"))

    output = conversion._execute_plan(
        source_variant=variant,
        target_format="epub",
        route=(conversion.ConversionStep("txt", "epub", "pandoc"),),
        request_options=None,
        processing_job_id=uuid.uuid4(),
    )

    assert output.storage_location == default_archive_location()
    archive = LocalFilesystemStorage(str(app_storage / "library-artifacts"))
    assert archive.exists(output.storage_identifier)
    assert sorted(p for p in Path(location.root_path).rglob("*")) == share_files_before

    book.write_text("replaced", encoding="utf-8")
    with pytest.raises(conversion.ConversionError):
        conversion._execute_plan(
            source_variant=variant,
            target_format="epub",
            route=(conversion.ConversionStep("txt", "epub", "pandoc"),),
            request_options=None,
            processing_job_id=uuid.uuid4(),
        )


@pytest.mark.django_db(transaction=True)
def test_book_deletion_removes_record_but_leaves_referenced_file(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    variant = FileVariant.objects.get(pk=_import(location, book).file_variant_id)

    delete_book_safely(variant.book)

    assert not FileVariant.objects.filter(pk=variant.pk).exists()
    assert book.read_text(encoding="utf-8") == BOOK_TEXT


@pytest.mark.django_db(transaction=True)
def test_changed_file_at_indexed_path_is_not_silently_repointed(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    location, book = _index_source(tmp_path)
    original = FileVariant.objects.get(pk=_import(location, book).file_variant_id)
    original_sha = hashlib.sha256(book.read_bytes()).hexdigest()

    book.write_text(BOOK_TEXT + "new edition", encoding="utf-8")
    job = intake.create_source_import_job(book, location)
    with pytest.raises(referenced_files.ReferencedFileError):
        intake.process_import_job(str(job.id))

    original.refresh_from_db()
    assert original.sha256 == original_sha
    assert FileVariant.objects.count() == 1


@pytest.mark.django_db
def test_source_location_validation_for_index_storage(tmp_path: Path) -> None:
    share = tmp_path / "share"
    (share / "books").mkdir(parents=True)
    read_only = StorageLocation.objects.create(
        identifier="share", base_path=str(share), read_only=True
    )
    writable = StorageLocation.objects.create(identifier="rw-share", base_path=str(share))

    ok = SourceLocation(
        name="ok",
        root_path=str(share / "books"),
        transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE,
        index_storage_location=read_only,
    )
    ok.full_clean()

    for bad in (
        SourceLocation(
            name="writable",
            root_path=str(share),
            transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE,
            index_storage_location=writable,
        ),
        SourceLocation(
            name="outside",
            root_path=str(tmp_path),
            transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE,
            index_storage_location=read_only,
        ),
        SourceLocation(
            name="copy-mode",
            root_path=str(share),
            transfer_mode=SourceLocation.TransferMode.COPY,
            index_storage_location=read_only,
        ),
    ):
        with pytest.raises(ValidationError):
            bad.full_clean()


@pytest.mark.django_db
def test_existing_sources_default_to_copy() -> None:
    source = SourceLocation.objects.create(name="legacy", root_path="/srv/incoming")
    assert source.transfer_mode == SourceLocation.TransferMode.COPY
    assert source.index_storage_location is None


@pytest.mark.django_db(transaction=True)
def test_index_sources_do_not_consume_archive_space_budget(
    tmp_path: Path, app_storage: Path
) -> None:
    del app_storage
    location, _book = _index_source(tmp_path)
    budget = intake.ByteBudget(remaining=0)

    jobs = intake.discover_source_jobs_for_location(location, byte_budget=budget)

    assert len(jobs) == 1
    assert not budget.exhausted


@pytest.mark.django_db(transaction=True)
def test_ingest_status_reports_pending_work_per_source(tmp_path: Path, app_storage: Path) -> None:
    del app_storage
    location, book = _index_source(tmp_path)
    intake.create_source_import_job(book, location)
    stdout = StringIO()

    call_command(
        "ingest_sources", "--source-id", str(location.id), "--status", "--json", stdout=stdout
    )

    [row] = json.loads(stdout.getvalue())
    assert row["transfer_mode"] == SourceLocation.TransferMode.INDEX_IN_PLACE
    assert row["pending_files"] == 1
    assert row["pending_bytes"] == book.stat().st_size
    assert row["complete"] == 0


@pytest.mark.django_db
def test_reference_fields_only_for_read_only_locations(tmp_path: Path) -> None:
    job = ImportJob(source_mtime_ns=123)
    job.storage_location = StorageLocation.objects.create(identifier="rw", base_path=str(tmp_path))
    assert referenced_files.reference_fields_for_job(job) == {}
    job.storage_location = StorageLocation.objects.create(
        identifier="ro", base_path=str(tmp_path), read_only=True
    )
    fields = referenced_files.reference_fields_for_job(job)
    assert fields["source_mtime_ns"] == 123
    assert isinstance(fields["reference_checked_at"], datetime)


# --- Readiness: an unmounted share is an outage, never mass drift -------------------


def _indexed_variants(tmp_path: Path, count: int = 3) -> tuple[list[FileVariant], Path]:
    """Index ``count`` files from one share; returns their variants and the share root."""
    location, book = _index_source(tmp_path)
    variants = [FileVariant.objects.get(pk=_import(location, book).file_variant_id)]
    root = Path(location.root_path)
    for number in range(1, count):
        extra = root / f"Extra {number}.txt"
        extra.write_text(f"{BOOK_TEXT}extra {number}\n", encoding="utf-8")
        variants.append(FileVariant.objects.get(pk=_import(location, extra).file_variant_id))
    return variants, root


def _unmount(root: Path) -> None:
    """Simulate an unmounted share: the mount point directory exists but is empty."""
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts), reverse=True):
        if path.is_dir():
            path.rmdir()
        else:
            path.unlink()
    assert root.is_dir() and not any(root.iterdir())


def _all_ok(variants: list[FileVariant]) -> bool:
    return all(
        FileVariant.objects.get(pk=v.pk).reference_state == FileVariant.ReferenceState.OK
        for v in variants
    )


@pytest.mark.django_db(transaction=True)
def test_empty_mount_point_is_outage_not_mass_missing(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    variants, root = _indexed_variants(tmp_path)
    _unmount(root)

    report = referenced_files.check_referenced_files()

    assert report.checked == len(variants)
    assert not report.missing and not report.changed
    assert len(report.errors) == len(variants)
    assert report.unavailable_locations == [variants[0].storage_location.identifier]
    assert _all_ok(variants)
    # A download during the outage is refused but does not flag the variant either.
    assert _download(variants[0])[0] == 409
    assert _all_ok(variants)


@pytest.mark.django_db(transaction=True)
def test_missing_sentinel_is_outage_even_when_mount_point_has_files(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    variants, root = _indexed_variants(tmp_path, count=2)
    share = variants[0].storage_location
    share.sentinel_file = ".ebook-index-sentinel"
    share.save()
    (root / ".ebook-index-sentinel").write_text("", encoding="utf-8")
    for variant in variants:  # the files really are gone ...
        (root / variant.storage_identifier).unlink()
    assert referenced_files.check_referenced_files().missing == [v.pk for v in variants]

    FileVariant.objects.filter(pk__in=[v.pk for v in variants]).update(
        reference_state=FileVariant.ReferenceState.OK,
    )
    (root / ".ebook-index-sentinel").unlink()  # ... but now the share itself is gone
    (root / "leftover-on-local-disk").write_text("x", encoding="utf-8")

    report = referenced_files.check_referenced_files()

    assert not report.missing and report.unavailable_locations == [share.identifier]
    assert _all_ok(variants)
    stderr = StringIO()
    with pytest.raises(CommandError):
        call_command("check_referenced_files", stdout=StringIO(), stderr=stderr)
    assert f"UNAVAILABLE storage_location:{share.identifier}" in stderr.getvalue()
    assert _all_ok(variants)


@pytest.mark.django_db
def test_invalid_sentinel_name_counts_as_unavailable(tmp_path: Path) -> None:
    (tmp_path / "file").write_text("x", encoding="utf-8")
    location = StorageLocation.objects.create(
        identifier="ro-share",
        base_path=str(tmp_path),
        read_only=True,
        sentinel_file="../outside",
    )
    backend = LocalFilesystemStorage(str(tmp_path))
    assert referenced_files.location_unavailable_reason(location, backend) is not None
    location.sentinel_file = ""
    assert referenced_files.location_unavailable_reason(location, backend) is None


# --- Scoping and the queued job ------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_check_scoped_to_selected_variants(
    tmp_path: Path, app_storage: Path, matched: None
) -> None:
    del matched, app_storage
    variants, root = _indexed_variants(tmp_path)
    for variant in variants:
        (root / variant.storage_identifier).unlink()

    report = referenced_files.check_referenced_files(variant_ids=[variants[1].pk])

    assert report.checked == 1 and report.missing == [variants[1].pk]
    states = {v.pk: FileVariant.objects.get(pk=v.pk).reference_state for v in variants}
    assert states == {
        variants[0].pk: FileVariant.ReferenceState.OK,
        variants[1].pk: FileVariant.ReferenceState.MISSING,
        variants[2].pk: FileVariant.ReferenceState.OK,
    }


class _RecordingQueue:
    def __init__(self) -> None:
        self.calls: list[tuple[object, tuple[object, ...]]] = []

    def enqueue(self, func: object, *args: object, **kwargs: object) -> None:
        del kwargs
        self.calls.append((func, args))


def _admin_client() -> Client:
    from django.contrib.auth import get_user_model

    user = get_user_model().objects.create_superuser(
        username=f"admin-{uuid.uuid4().hex[:8]}", password="unused", email=""
    )
    client = Client()
    client.force_login(user)
    return client


@pytest.mark.django_db(transaction=True)
def test_admin_action_queues_scoped_job_without_hashing_inline(
    tmp_path: Path, app_storage: Path, matched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del matched, app_storage
    variants, root = _indexed_variants(tmp_path)
    for variant in variants:  # touched: a check would have to re-hash every file
        path = root / variant.storage_identifier
        stat = path.stat()
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 9_000_000_000))
    queue = _RecordingQueue()
    monkeypatch.setattr(referenced_files, "get_queue", lambda: queue)

    def no_inline_io(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("the admin request must not stat or hash referenced files")

    monkeypatch.setattr(LocalFilesystemStorage, "hash", no_inline_io)
    monkeypatch.setattr(referenced_files, "check_referenced_files", no_inline_io)

    selected = [variants[0].pk, variants[2].pk]
    response = _admin_client().post(
        "/admin/core/filevariant/",
        {"action": "recheck_referenced_files", "_selected_action": selected},
        follow=True,
    )

    assert response.status_code == 200
    job = ProcessingJob.objects.get(job_type=ProcessingJob.JobType.REFERENCE_CHECK)
    assert job.state == ProcessingJob.JobState.QUEUED
    assert job.details["variant_ids"] == sorted(selected)
    assert queue.calls == [(referenced_files.process_reference_check_job, (str(job.job_id),))]
    assert str(job.job_id) in response.content.decode()
    monkeypatch.undo()

    # The worker runs the recorded scope and stores the report on the job.
    referenced_files.process_reference_check_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.SUCCESS
    assert job.details["report"]["checked"] == 2
    assert job.details["report"]["ok"] == 2
    assert job.details["attempt_count"] == 1


@pytest.mark.django_db(transaction=True)
def test_location_admin_action_and_cli_run_queued_without_worker(
    tmp_path: Path, app_storage: Path, matched: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    del matched, app_storage
    variants, root = _indexed_variants(tmp_path, count=2)
    share = variants[0].storage_location
    monkeypatch.setattr(referenced_files, "get_queue", lambda: None)

    response = _admin_client().post(
        "/admin/core/storagelocation/",
        {"action": "recheck_location_references", "_selected_action": [share.pk]},
        follow=True,
    )

    assert response.status_code == 200
    assert "--run-queued" in response.content.decode()
    job = ProcessingJob.objects.get(job_type=ProcessingJob.JobType.REFERENCE_CHECK)
    assert job.state == ProcessingJob.JobState.QUEUED  # nothing ran in the request
    assert job.details["storage_location_ids"] == [share.pk]

    _unmount(root)
    with pytest.raises(CommandError):
        call_command("check_referenced_files", "--run-queued", stdout=StringIO())
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.FAILED
    assert "storage unavailable" in job.details["error"]
    assert _all_ok(variants)
