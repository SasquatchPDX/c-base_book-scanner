"""``scan.jsonl`` event batches under real concurrency on PostgreSQL (#129).

Threads use separate database connections, so the job row lock and the unique
``(job, line_no)`` constraint are exercised for real. Single-connection cases are in
tests/test_scan_events.py.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from django.db import connections

from ebook_server.core import scan_events
from ebook_server.core.models import ScanEvent, ScanJob
from ebook_server.storage import LocalFilesystemStorage

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]

WAIT_SECONDS = 30
JOB_ID = "20261117T143201Z"


def _line(line_no: int) -> str:
    return f'{{"type":"attempt","sequence_number":{line_no},"pickup_result":"success"}}'


def _run_concurrently(targets: list[Callable[[], Any]]) -> list[BaseException]:
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(targets))

    def _wrap(target: Callable[[], Any]) -> Callable[[], None]:
        def _run() -> None:
            try:
                barrier.wait(timeout=WAIT_SECONDS)
                target()
            except BaseException as exc:  # pragma: no cover - surfaced by the caller
                errors.append(exc)
            finally:
                connections.close_all()

        return _run

    threads = [threading.Thread(target=_wrap(target), daemon=True) for target in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(WAIT_SECONDS)
        assert not thread.is_alive()
    return errors


@pytest.fixture
def job(tmp_path: Path, settings: Any) -> ScanJob:
    settings.APP_DATA_DIR = tmp_path
    return ScanJob.objects.create(
        id=JOB_ID,
        state=ScanJob.State.CAPTURING,
        source=ScanJob.Source.SCANNER,
        scan_started_at=datetime(2026, 11, 17, 14, 32, 1, tzinfo=UTC),
    )


def _assert_contiguous(count: int) -> None:
    numbers = list(
        ScanEvent.objects.filter(scan_job_id=JOB_ID, origin="sans")
        .order_by("id")
        .values_list("line_no", flat=True)
    )
    assert numbers == list(range(1, count + 1))
    raws = list(
        ScanEvent.objects.filter(scan_job_id=JOB_ID).order_by("id").values_list("raw", flat=True)
    )
    assert raws == [_line(number) for number in range(1, count + 1)]


@pytest.mark.django_db(transaction=True)
def test_concurrent_overlapping_batches_store_each_line_once(job: ScanJob) -> None:
    lines = [_line(number) for number in range(1, 201)]
    # Eight senders: the same batch twice, overlapping ranges, and repeats of a prefix.
    batches = [(1, 120), (1, 120), (50, 200), (1, 200), (1, 10), (100, 200), (1, 60), (1, 200)]

    def send(first: int, last: int) -> Callable[[], Any]:
        return lambda: scan_events.append_sans_lines(JOB_ID, first, lines[first - 1 : last])

    errors = _run_concurrently([send(first, last) for first, last in batches])
    # A batch that started beyond the mark before its predecessor committed is a gap,
    # which the scanner resolves by resending from expected_first_line_no.
    assert all(isinstance(error, scan_events.EventGap) for error in errors), errors
    assert scan_events.sans_high_water_mark(JOB_ID) == 200
    _assert_contiguous(200)


@pytest.mark.django_db(transaction=True)
def test_concurrent_next_batches_never_leave_a_gap(job: ScanJob) -> None:
    lines = [_line(number) for number in range(1, 101)]
    scan_events.append_sans_lines(JOB_ID, 1, lines[:10])
    results: list[int] = []

    def send(first: int, last: int) -> Callable[[], None]:
        def _send() -> None:
            try:
                result = scan_events.append_sans_lines(JOB_ID, first, lines[first - 1 : last])
                results.append(result.sans_last_line_no)
            except scan_events.EventGap as exc:
                results.append(-int(exc.details["expected_first_line_no"]))

        return _send

    errors = _run_concurrently([send(11, 50), send(51, 100), send(11, 50), send(1, 50)])
    assert errors == []
    high = scan_events.sans_high_water_mark(JOB_ID)
    _assert_contiguous(high)
    assert high in {50, 100}
    if high == 50:
        # The tail batch saw the mark before the head committed: told where to resume.
        assert -51 in results or -11 in results


@pytest.mark.django_db(transaction=True)
def test_serif_events_and_scanner_batches_interleave_without_loss(job: ScanJob) -> None:
    lines = [_line(number) for number in range(1, 41)]
    targets: list[Callable[[], Any]] = [
        lambda: scan_events.append_sans_lines(JOB_ID, 1, lines),
        lambda: scan_events.append_serif_event(JOB_ID, "rescan_flag", {"sequence_number": 1}),
        lambda: scan_events.append_sans_lines(JOB_ID, 1, lines),
        lambda: scan_events.append_serif_event(JOB_ID, "rescan_flag", {"sequence_number": 2}),
    ]
    assert _run_concurrently(targets) == []
    events = list(ScanEvent.objects.filter(scan_job_id=JOB_ID).order_by("id"))
    assert len(events) == 42
    assert [event.line_no for event in events if event.origin == "sans"] == list(range(1, 41))
    assert sum(1 for event in events if event.origin == "serif") == 2
    assert scan_events.render_scan_jsonl(JOB_ID) == b"".join(
        event.raw.encode() + b"\n" for event in events
    )


@pytest.mark.django_db(transaction=True)
def test_an_older_render_is_fenced_out_by_a_newer_one(
    job: ScanJob, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDXDJN #140 finding 2 on real row locks: a render that staged an older snapshot
    and stalls while a newer render publishes must not rename its snapshot over it."""
    scan_events.append_sans_lines(JOB_ID, 1, [_line(1)])
    job_file = tmp_path / "scan-artifacts" / "scan-jobs" / JOB_ID / "scan.jsonl"
    job_file.unlink(missing_ok=True)  # stale: the older render must write
    staged = threading.Event()
    release = threading.Event()
    commits: list[str] = []
    original_stage = LocalFilesystemStorage.stage_bytes
    original_commit = LocalFilesystemStorage.commit_staged

    def stalling(self: LocalFilesystemStorage, destination: str, data: bytes) -> str:
        result = original_stage(self, destination, data)
        if threading.current_thread().name == "older-render":
            staged.set()
            assert release.wait(WAIT_SECONDS)
        return result

    def counting(self: LocalFilesystemStorage, staged_id: str, destination: str) -> str:
        commits.append(threading.current_thread().name)
        return original_commit(self, staged_id, destination)

    monkeypatch.setattr(LocalFilesystemStorage, "stage_bytes", stalling)
    monkeypatch.setattr(LocalFilesystemStorage, "commit_staged", counting)
    errors: list[BaseException] = []

    def older() -> None:
        try:
            scan_events.write_scan_jsonl(JOB_ID)
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=older, name="older-render", daemon=True)
    thread.start()
    assert staged.wait(WAIT_SECONDS)
    # Stored and committed while the older render holds its staged one-line snapshot;
    # the on-commit render (inline here) is the newer worker.
    scan_events.append_sans_lines(JOB_ID, 2, [_line(2)])
    scan_events.write_scan_jsonl(JOB_ID)
    release.set()
    thread.join(WAIT_SECONDS)
    assert not thread.is_alive()
    assert errors == []
    assert "older-render" not in commits
    assert job_file.read_bytes() == scan_events.render_scan_jsonl(JOB_ID)
    assert job_file.read_text(encoding="utf-8") == f"{_line(1)}\n{_line(2)}\n"
    assert not list(job_file.parent.glob("*.partial"))
    job.refresh_from_db()
    assert job.scan_jsonl_job_rendered_through == ScanEvent.objects.latest("id").pk
