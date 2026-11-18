"""Conversion delivery ownership and fenced archive publication."""

from __future__ import annotations

import errno
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from django.utils import timezone

from ebook_server.core import conversion
from ebook_server.core.management.commands.recover_stale_jobs import Command
from ebook_server.core.models import FileVariant, ProcessingJob
from ebook_server.jobs import stale_after
from ebook_server.storage import LocalFilesystemStorage
from tests.test_conversion import StubConverter, _configure_storage, _create_book_with_variant


def create_conversion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[ProcessingJob, LocalFilesystemStorage]:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_ARCHIVE_LAYOUT", "{title} - {author}")
    storage = _configure_storage(tmp_path)
    source = _create_book_with_variant(
        storage, source_fmt="pdf", source_payload=b"original pdf", source_file_name="original.pdf"
    )
    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": StubConverter(b"converted")})
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.CONVERSION,
        file_variant=source,
        details={
            "route": [{"source_format": "pdf", "target_format": "txt", "converter": "calibre"}],
            "target_format": "txt",
            "request_options": {"test": "preserved"},
            "requested_by_user_id": 17,
        },
    )
    return job, storage


@pytest.mark.django_db(transaction=True)
def test_claim_accepts_queued_and_stale_but_rejects_live_terminal_and_other_types(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)
    claim = conversion._claim_conversion_job(str(job.job_id))
    assert claim is not None
    _, first = claim
    assert conversion._claim_conversion_job(str(job.job_id)) is None
    ProcessingJob.objects.filter(pk=job.pk).update(
        started_at=timezone.now() - stale_after(job.job_type) - timedelta(seconds=1)
    )
    replacement = conversion._claim_conversion_job(str(job.job_id))
    assert replacement is not None and replacement[1] != first
    for state in [ProcessingJob.JobState.SUCCESS, ProcessingJob.JobState.FAILED]:
        ProcessingJob.objects.filter(pk=job.pk).update(state=state)
        assert conversion._claim_conversion_job(str(job.job_id)) is None
    ProcessingJob.objects.filter(pk=job.pk).update(
        state=ProcessingJob.JobState.QUEUED, job_type=ProcessingJob.JobType.OCR
    )
    assert conversion._claim_conversion_job(str(job.job_id)) is None


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("complete", [False, True])
def test_enqueue_error_cannot_fail_a_worker_that_already_started(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)

    class AmbiguousQueue:
        def enqueue(self, function: Any, job_id: str) -> None:
            if complete:
                function(job_id)
            else:
                assert conversion._claim_conversion_job(job_id) is not None
            raise RuntimeError("enqueue acknowledgement lost")

    monkeypatch.setattr(conversion, "get_queue", lambda: AmbiguousQueue())
    conversion._dispatch_conversion_job(job)
    job.refresh_from_db()
    assert job.state == (
        ProcessingJob.JobState.SUCCESS if complete else ProcessingJob.JobState.RUNNING
    )
    assert "enqueue acknowledgement" not in job.details.get("error", "")


@pytest.mark.django_db(transaction=True)
def test_enqueue_error_fails_only_an_unclaimed_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)
    assert conversion._fail_conversion_job(job, "enqueue failed")
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.FAILED


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("replacement_completes", [False, True])
def test_recovery_snapshot_cannot_fail_a_newer_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement_completes: bool
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)
    assert conversion._claim_conversion_job(str(job.job_id))
    ProcessingJob.objects.filter(pk=job.pk).update(
        started_at=timezone.now() - stale_after(job.job_type) - timedelta(seconds=1)
    )
    snapshot = ProcessingJob.objects.get(pk=job.pk)
    if replacement_completes:
        conversion.process_conversion_job(str(job.job_id))
    else:
        assert conversion._claim_conversion_job(str(job.job_id))
    Command()._fail(snapshot, "stale earlier snapshot")
    job.refresh_from_db()
    assert job.state == (
        ProcessingJob.JobState.SUCCESS if replacement_completes else ProcessingJob.JobState.RUNNING
    )


@pytest.mark.django_db(transaction=True)
def test_recovery_fails_unchanged_attempt_and_fences_late_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)
    claim = conversion._claim_conversion_job(str(job.job_id))
    assert claim is not None
    _, token = claim
    ProcessingJob.objects.filter(pk=job.pk).update(
        started_at=timezone.now() - stale_after(job.job_type) - timedelta(seconds=1)
    )
    owned = ProcessingJob.objects.get(pk=job.pk)
    assert conversion.recover_stale_conversion_job(owned, "operator recovery")
    assert not conversion._fail_conversion_job(owned, "late worker error", token=token)
    job.refresh_from_db()
    assert job.details["error"] == "operator recovery"


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("layout", ["job", "author_title"])
def test_conversion_keeps_configured_archive_layout_and_naming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, layout: str
) -> None:
    job, storage = create_conversion(tmp_path, monkeypatch)
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_ARCHIVE_LAYOUT", layout)
    conversion.process_conversion_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.SUCCESS, job.details
    output = FileVariant.objects.get(pk=job.details["output_variant_id"])
    path = Path(output.storage_identifier)
    if layout == "job":
        assert path.parent.as_posix() == f"books/{output.book_id}/converted"
        assert UUID(path.stem)
    else:
        assert path.name == "Converted Source - Unknown author.txt"
        assert path.parent.as_posix() == "Unknown author/Converted Source"
    assert storage.hash(output.storage_identifier) == output.sha256


@pytest.mark.django_db(transaction=True)
def test_conversion_without_hard_links_preserves_copy_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, storage = create_conversion(tmp_path, monkeypatch)

    def no_link(*args: Any, **kwargs: Any) -> None:
        raise OSError(errno.EOPNOTSUPP, "mount has no hard links")

    monkeypatch.setattr("ebook_server.storage.os.link", no_link)
    conversion.process_conversion_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.SUCCESS, job.details
    output = FileVariant.objects.get(pk=job.details["output_variant_id"])
    with storage.open(output.storage_identifier) as stream:
        assert stream.read() == b"converted"
    assert not list(Path(storage.root).glob("_conversion_staging/**/*.*"))


@pytest.mark.django_db(transaction=True)
def test_database_failure_after_promotion_removes_only_unpublished_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, storage = create_conversion(tmp_path, monkeypatch)
    original_save = FileVariant.save

    def fail_output(self: FileVariant, *args: Any, **kwargs: Any) -> None:
        if self.source_type == FileVariant.SourceType.CONVERTED:
            raise RuntimeError("database insertion failed")
        original_save(self, *args, **kwargs)

    monkeypatch.setattr(FileVariant, "save", fail_output)
    conversion.process_conversion_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.FAILED
    assert FileVariant.objects.count() == 1
    assert sorted(p.read_bytes() for p in Path(storage.root).rglob("*") if p.is_file()) == [
        b"original pdf",
        b"original pdf",
    ]
