"""Scanner output lands in the book's archive folder on the attached storage (#7)."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core import archive_layout, intake, scan_logs, scan_pipeline
from ebook_server.core.models import (
    Author,
    Book,
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanLog,
)
from ebook_server.storage import LocalFilesystemStorage
from tests.test_scan_jobs import (
    _create_scan_job_with_book,
    _DeferredQueue,
    _upload_scan_page,
    _valid_png_bytes,
)

LOG = b"2026-11-17 10:00:01 capture start\n2026-11-17 10:12:44 capture done, 212 pages\n"
LOG_SHA256 = hashlib.sha256(LOG).hexdigest()
# Uploaded logs are stored as scan-upload-<sha8>.log; scan.jsonl is reserved for #129.
LOG_NAME = f"scan-upload-{LOG_SHA256[:8]}.log"


@pytest.fixture(autouse=True)
def _app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    return tmp_path


def _book() -> Book:
    book = Book.objects.create(title="Gödel, Escher, Bach")
    book.authors.add(Author.objects.create(full_name="Douglas Hofstadter"))
    return book


def _post_log(client: Client, job: ScanJob, payload: bytes, name: str = "scan.log") -> Any:
    return client.post(
        f"/scan/jobs/{job.id}/logs/",
        data={"file": SimpleUploadedFile(name, payload, content_type="text/plain")},
    )


def _finalize(client: Client, job: ScanJob, monkeypatch: pytest.MonkeyPatch) -> None:
    queue = _DeferredQueue()
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: queue)
    upload = _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_valid_png_bytes(),
        filename="p1.png",
    )
    assert upload.status_code == 201
    finish = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data={"create_cleaned_variant": "true", "allow_raw_pages": "true"},
    )
    assert finish.status_code == 202, finish.content.decode()
    queue.run_next()
    job.refresh_from_db()
    assert job.state == ScanJob.State.COMPLETE


FOLDER = "Douglas Hofstadter/Gödel, Escher, Bach"


@pytest.mark.django_db(transaction=True)
def test_cleaned_scan_pdf_is_written_into_the_book_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _book()
    client, job = _create_scan_job_with_book("scan-layout", book_id=book.id)
    _finalize(client, job, monkeypatch)

    cleaned = FileVariant.objects.get(book=book, source_type=FileVariant.SourceType.CLEANED)
    assert cleaned.storage_identifier == f"{FOLDER}/Gödel, Escher, Bach - Douglas Hofstadter.pdf"
    assert (tmp_path / "library-artifacts" / cleaned.storage_identifier).is_file()


@pytest.fixture
def log_queue(monkeypatch: pytest.MonkeyPatch) -> _DeferredQueue:
    queue = _DeferredQueue()
    monkeypatch.setattr(scan_logs, "get_queue", lambda: queue)
    return queue


def _run_log_jobs(queue: _DeferredQueue) -> None:
    while queue.calls:
        queue.run_next()


@pytest.mark.django_db(transaction=True)
def test_log_upload_only_stages_and_queues_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_queue: _DeferredQueue
) -> None:
    """The request writes bounded bytes and records a job; no hashing, no archive copy."""
    book = _book()
    client, job = _create_scan_job_with_book("scan-log-async", book_id=book.id)

    def _forbidden(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("hashing or archiving ran inside the request")

    with monkeypatch.context() as patched:
        patched.setattr(intake, "compute_sha256", _forbidden)
        patched.setattr(archive_layout, "publish_derived", _forbidden)
        patched.setattr(LocalFilesystemStorage, "publish_unique", _forbidden)
        patched.setattr(LocalFilesystemStorage, "store", _forbidden)
        patched.setattr(LocalFilesystemStorage, "hash", _forbidden)
        response = _post_log(client, job, LOG)

    assert response.status_code == 202, response.content.decode()
    body = response.json()
    assert body["log"]["state"] == "queued"
    assert body["log"]["sha256"] is None
    assert body["status_url"] == f"/scan/jobs/{job.id}/logs/"
    log = ScanLog.objects.get(scan_job=job)
    assert log.processing_job is not None
    assert log.processing_job.job_type == ProcessingJob.JobType.SCAN_LOG
    assert log.processing_job.state == ProcessingJob.JobState.QUEUED
    assert (tmp_path / "scan-artifacts" / log.staging_identifier).read_bytes() == LOG
    assert not (tmp_path / "library-artifacts" / FOLDER / LOG_NAME).exists()
    assert len(log_queue.calls) == 1  # enqueued once the request transaction committed

    _run_log_jobs(log_queue)
    status = client.get(f"/scan/jobs/{job.id}/logs/").json()["logs"]
    assert [entry["state"] for entry in status] == ["stored"]
    assert status[0]["in_book_folder"] is True
    detail = client.get(f"/scan/jobs/{job.id}/").json()
    assert detail["logs"][0]["state"] == "stored"
    log.refresh_from_db()
    assert log.staging_identifier == ""
    assert log.processing_job is not None
    assert log.processing_job.state == ProcessingJob.JobState.SUCCESS


@pytest.mark.django_db(transaction=True)
def test_log_for_linked_job_goes_straight_into_the_book_folder(
    tmp_path: Path, log_queue: _DeferredQueue
) -> None:
    book = _book()
    client, job = _create_scan_job_with_book("scan-log-direct", book_id=book.id)

    response = _post_log(client, job, LOG)
    assert response.status_code == 202, response.content.decode()
    _run_log_jobs(log_queue)
    log = ScanLog.objects.get(scan_job=job)
    assert log.state == ScanLog.State.STORED
    assert log.storage_identifier == f"{FOLDER}/{LOG_NAME}"
    assert (tmp_path / "library-artifacts" / FOLDER / LOG_NAME).read_bytes() == LOG

    again = _post_log(client, job, LOG)
    assert again.status_code == 202
    _run_log_jobs(log_queue)
    duplicate = ScanLog.objects.exclude(pk=log.pk).get(scan_job=job)
    assert duplicate.state == ScanLog.State.DUPLICATE
    assert duplicate.duplicate_of_id == log.pk
    assert ScanLog.objects.filter(scan_job=job, state=ScanLog.State.STORED).count() == 1

    longer = LOG + b"rescan of page 17\n"
    second = _post_log(client, job, longer)
    assert second.status_code == 202
    _run_log_jobs(log_queue)
    names = sorted(p.name for p in (tmp_path / "library-artifacts" / FOLDER).iterdir())
    longer_name = f"scan-upload-{hashlib.sha256(longer).hexdigest()[:8]}.log"
    assert names == sorted([LOG_NAME, longer_name])
    assert "scan.jsonl" not in names
    # Every staged upload was cleaned up by its worker.
    assert not list((tmp_path / "scan-artifacts" / scan_logs.STAGING_NAMESPACE).iterdir())


@pytest.mark.django_db(transaction=True)
def test_log_before_book_link_moves_into_folder_on_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_queue: _DeferredQueue
) -> None:
    client, job = _create_scan_job_with_book("scan-log-later")
    response = _post_log(client, job, LOG)
    assert response.status_code == 202
    _run_log_jobs(log_queue)
    waiting = ScanLog.objects.get(scan_job=job)
    assert waiting.state == ScanLog.State.STORED
    assert waiting.storage_location is not None
    assert waiting.storage_location.identifier == "scan"
    staged = tmp_path / "scan-artifacts" / waiting.storage_identifier
    assert staged.read_bytes() == LOG

    job.book = _book()
    job.save(update_fields=["book"])
    _finalize(client, job, monkeypatch)

    waiting.refresh_from_db()
    assert waiting.storage_location is not None
    assert waiting.storage_location.identifier == "default"
    assert waiting.storage_identifier == f"{FOLDER}/{LOG_NAME}"
    assert (tmp_path / "library-artifacts" / FOLDER / LOG_NAME).read_bytes() == LOG
    assert not staged.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"", "empty"),
        (b"x" * (8 * 1024 * 1024 + 1), "too large"),
    ],
    ids=["empty", "oversized"],
)
def test_scan_log_upload_rejects_empty_and_oversized_logs_in_the_request(
    tmp_path: Path, payload: bytes, error: str, log_queue: _DeferredQueue
) -> None:
    client, job = _create_scan_job_with_book("scan-log-bad")
    response = _post_log(client, job, payload)
    assert response.status_code == 400
    assert error in response.content.decode()
    assert not ScanLog.objects.exists()
    assert not log_queue.calls
    staging = tmp_path / "scan-artifacts" / scan_logs.STAGING_NAMESPACE
    assert not staging.exists() or not list(staging.iterdir())


@pytest.mark.django_db(transaction=True)
def test_binary_log_fails_on_the_worker_and_reports_it(
    tmp_path: Path, log_queue: _DeferredQueue
) -> None:
    client, job = _create_scan_job_with_book("scan-log-binary", book_id=_book().id)
    response = _post_log(client, job, b"\x89PNG\x00\x00binary")
    assert response.status_code == 202
    _run_log_jobs(log_queue)
    log = ScanLog.objects.get(scan_job=job)
    assert log.state == ScanLog.State.FAILED
    assert "not a text file" in log.error
    assert log.processing_job is not None
    assert log.processing_job.state == ProcessingJob.JobState.FAILED
    assert client.get(f"/scan/jobs/{job.id}/logs/").json()["logs"][0]["state"] == "failed"
    assert not (tmp_path / "library-artifacts" / FOLDER).exists()


@pytest.mark.django_db(transaction=True)
def test_scan_log_upload_ignores_client_path_in_filename(
    tmp_path: Path, log_queue: _DeferredQueue
) -> None:
    client, job = _create_scan_job_with_book("scan-log-path", book_id=_book().id)
    response = _post_log(client, job, LOG, name="../../etc/scan.log")
    assert response.status_code == 202
    _run_log_jobs(log_queue)
    log = ScanLog.objects.get(scan_job=job)
    assert log.original_filename == "scan.log"
    assert log.storage_identifier == f"{FOLDER}/{LOG_NAME}"


def _corrupt_after_publish(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """Make every publication silently write bad bytes, as a faulty network copy would.

    The real publication runs first; the destination is then overwritten before the
    worker gets to verify it. Returns the corrupted paths.
    """
    corrupted: list[Path] = []
    real_publish = LocalFilesystemStorage.publish_unique

    def _publish(self: LocalFilesystemStorage, *args: Any, **kwargs: Any) -> str:
        destination = real_publish(self, *args, **kwargs)
        path = self.resolve_location(destination)
        path.write_bytes(b"CORRUPTED")
        corrupted.append(path)
        return destination

    monkeypatch.setattr(LocalFilesystemStorage, "publish_unique", _publish)
    return corrupted


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("linked", [True, False], ids=["book-folder", "scan-storage"])
def test_corrupted_published_copy_fails_and_keeps_the_staged_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_queue: _DeferredQueue, linked: bool
) -> None:
    """A copy that does not hash to the staged upload is never recorded as stored."""
    book_id = _book().id if linked else None
    client, job = _create_scan_job_with_book(f"scan-log-corrupt-{linked}", book_id=book_id)
    response = _post_log(client, job, LOG)
    assert response.status_code == 202
    log = ScanLog.objects.get(scan_job=job)
    staged = tmp_path / "scan-artifacts" / log.staging_identifier

    with monkeypatch.context() as patched:
        corrupted = _corrupt_after_publish(patched)
        _run_log_jobs(log_queue)
    assert len(corrupted) == 1

    log.refresh_from_db()
    assert log.state == ScanLog.State.FAILED
    assert "SHA-256 verification" in log.error
    assert log.sha256 == ""
    assert log.storage_identifier == ""
    assert log.processing_job is not None
    assert log.processing_job.state == ProcessingJob.JobState.FAILED
    assert log.processing_job.details["retryable"] is True
    assert log.processing_job.details["staging_retained"] is True
    # The staged original is intact and still recorded; only the bad copy was removed.
    assert staged.read_bytes() == LOG
    assert log.staging_identifier
    assert not corrupted[0].exists()
    assert client.get(f"/scan/jobs/{job.id}/logs/").json()["logs"][0]["state"] == "failed"

    # Retrying without the fault stores a verified copy and only then drops staging.
    assert scan_logs.retry_failed_scan_log(log) is True
    _run_log_jobs(log_queue)
    log.refresh_from_db()
    assert log.state == ScanLog.State.STORED
    assert log.sha256 == LOG_SHA256
    assert log.processing_job is not None
    assert log.processing_job.state == ProcessingJob.JobState.SUCCESS
    root = tmp_path / ("library-artifacts" if linked else "scan-artifacts")
    assert (root / log.storage_identifier).read_bytes() == LOG
    assert not staged.exists()


@pytest.mark.django_db(transaction=True)
def test_retry_refuses_a_log_whose_staged_upload_is_gone(
    tmp_path: Path, log_queue: _DeferredQueue
) -> None:
    client, job = _create_scan_job_with_book("scan-log-no-retry", book_id=_book().id)
    assert _post_log(client, job, b"\x89PNG\x00\x00binary").status_code == 202
    _run_log_jobs(log_queue)
    log = ScanLog.objects.get(scan_job=job)
    assert log.state == ScanLog.State.FAILED  # invalid content: staging was discarded
    assert scan_logs.retry_failed_scan_log(log) is False
    log.refresh_from_db()
    assert log.state == ScanLog.State.FAILED
    assert not log_queue.calls


@pytest.mark.django_db(transaction=True)
def test_move_into_book_folder_with_corrupted_copy_keeps_the_scan_storage_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_queue: _DeferredQueue
) -> None:
    client, job = _create_scan_job_with_book("scan-log-move-corrupt")
    assert _post_log(client, job, LOG).status_code == 202
    _run_log_jobs(log_queue)
    waiting = ScanLog.objects.get(scan_job=job)
    original = tmp_path / "scan-artifacts" / waiting.storage_identifier
    job.book = _book()
    job.save(update_fields=["book"])

    with monkeypatch.context() as patched:
        corrupted = _corrupt_after_publish(patched)
        assert scan_logs.place_scan_logs_in_book_folder(job) == 0
    assert len(corrupted) == 1 and not corrupted[0].exists()
    waiting.refresh_from_db()
    assert waiting.state == ScanLog.State.STORED
    assert waiting.storage_location is not None
    assert waiting.storage_location.identifier == "scan"
    assert original.read_bytes() == LOG
