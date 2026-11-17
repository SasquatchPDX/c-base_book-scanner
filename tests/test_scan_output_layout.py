"""Scanner output lands in the book's archive folder on the attached storage (#7)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core import scan_pipeline
from ebook_server.core.models import Author, Book, FileVariant, ScanJob, ScanLog
from tests.test_scan_jobs import (
    _create_scan_job_with_book,
    _DeferredQueue,
    _upload_scan_page,
    _valid_png_bytes,
)

LOG = b"2026-11-17 10:00:01 capture start\n2026-11-17 10:12:44 capture done, 212 pages\n"


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


@pytest.mark.django_db(transaction=True)
def test_log_for_linked_job_goes_straight_into_the_book_folder(tmp_path: Path) -> None:
    book = _book()
    client, job = _create_scan_job_with_book("scan-log-direct", book_id=book.id)

    response = _post_log(client, job, LOG)
    assert response.status_code == 201, response.content.decode()
    assert response.json()["log"]["in_book_folder"] is True
    log = ScanLog.objects.get(scan_job=job)
    assert log.storage_identifier == f"{FOLDER}/scan.log"
    assert (tmp_path / "library-artifacts" / FOLDER / "scan.log").read_bytes() == LOG

    again = _post_log(client, job, LOG)
    assert again.status_code == 200
    assert ScanLog.objects.filter(scan_job=job).count() == 1

    second = _post_log(client, job, LOG + b"rescan of page 17\n")
    assert second.status_code == 201
    names = sorted(p.name for p in (tmp_path / "library-artifacts" / FOLDER).iterdir())
    assert names[0] == "scan-" + names[0][5:] and names[1] == "scan.log"


@pytest.mark.django_db(transaction=True)
def test_log_before_book_link_moves_into_folder_on_finalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, job = _create_scan_job_with_book("scan-log-later")
    response = _post_log(client, job, LOG)
    assert response.status_code == 201
    assert response.json()["log"]["in_book_folder"] is False
    waiting = ScanLog.objects.get(scan_job=job)
    staged = tmp_path / "scan-artifacts" / waiting.storage_identifier
    assert staged.read_bytes() == LOG

    job.book = _book()
    job.save(update_fields=["book"])
    _finalize(client, job, monkeypatch)

    waiting.refresh_from_db()
    assert waiting.storage_location.identifier == "default"
    assert waiting.storage_identifier == f"{FOLDER}/scan.log"
    assert (tmp_path / "library-artifacts" / FOLDER / "scan.log").read_bytes() == LOG
    assert not staged.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("payload", "error"),
    [
        (b"", "empty"),
        (b"\x89PNG\x00\x00binary", "not a text file"),
        (b"x" * (8 * 1024 * 1024 + 1), "too large"),
    ],
    ids=["empty", "binary", "oversized"],
)
def test_scan_log_upload_rejects_non_logs(payload: bytes, error: str) -> None:
    client, job = _create_scan_job_with_book("scan-log-bad")
    response = _post_log(client, job, payload)
    assert response.status_code == 400
    assert error in response.content.decode()
    assert not ScanLog.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_scan_log_upload_ignores_client_path_in_filename(tmp_path: Path) -> None:
    client, job = _create_scan_job_with_book("scan-log-path", book_id=_book().id)
    response = _post_log(client, job, LOG, name="../../etc/scan.log")
    assert response.status_code == 201
    log = ScanLog.objects.get(scan_job=job)
    assert log.original_filename == "scan.log"
    assert log.storage_identifier == f"{FOLDER}/scan.log"
