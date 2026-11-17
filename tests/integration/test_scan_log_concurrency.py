"""Two workers storing identical scanner logs at once: threads on separate connections.

Both uploads were staged and queued; their workers race. Exactly one log may end up
``stored`` (the partial unique constraint settles it) and the other ``duplicate``,
with a single file in the book folder and no staged bytes left behind.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections

from ebook_server.core import intake, scan_logs
from ebook_server.core.models import Author, Book, ScanJob, ScanLog

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]

WAIT_SECONDS = 20
LOG = b"10:00:01 capture start\n10:12:44 capture done, 212 pages\n"


def _in_thread(target: Callable[[], Any], errors: list[BaseException]) -> threading.Thread:
    def _run() -> None:
        try:
            target()
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


@pytest.mark.django_db(transaction=True)
def test_identical_logs_processed_concurrently_store_one_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    monkeypatch.setattr(scan_logs, "get_queue", lambda: None)
    book = Book.objects.create(title="Emma")
    book.authors.add(Author.objects.create(full_name="Jane Austen"))
    job = ScanJob.objects.create(book=book)
    logs = [
        scan_logs.stage_scan_log(job, SimpleUploadedFile("scan.jsonl", LOG), None) for _ in range(2)
    ]

    # Both workers hash before either stores, so neither sees the other's row early.
    hashed = threading.Barrier(2)
    real_sha256 = intake.compute_sha256

    def _hash_then_wait(path: str) -> str:
        digest = real_sha256(path)
        hashed.wait(timeout=WAIT_SECONDS)
        return digest

    monkeypatch.setattr(intake, "compute_sha256", _hash_then_wait)
    errors: list[BaseException] = []
    job_ids = []
    for log in logs:
        assert log.processing_job is not None
        job_ids.append(str(log.processing_job.job_id))

    def _worker(processing_job_id: str) -> Callable[[], None]:
        return lambda: scan_logs.process_scan_log_job(processing_job_id)

    threads = [_in_thread(_worker(job_id), errors) for job_id in job_ids]
    for thread in threads:
        thread.join(WAIT_SECONDS)

    assert not errors
    states = sorted(ScanLog.objects.filter(scan_job=job).values_list("state", flat=True))
    assert states == [ScanLog.State.DUPLICATE, ScanLog.State.STORED]
    folder = tmp_path / "library-artifacts" / "Jane Austen" / "Emma"
    assert [p.read_bytes() for p in folder.iterdir()] == [LOG]
    staging = tmp_path / "scan-artifacts" / scan_logs.STAGING_NAMESPACE
    assert not list(staging.iterdir())
