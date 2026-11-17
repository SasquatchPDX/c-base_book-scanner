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
from ebook_server.core.scanner_devices import create_device

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
    device, _key = create_device("libreflip-pg")
    return ScanJob.objects.create(
        id=JOB_ID,
        state=ScanJob.State.CAPTURING,
        device=device,
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
