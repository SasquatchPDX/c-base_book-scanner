from __future__ import annotations

import io
import time
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any, cast

import img2pdf
import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone
from PIL import Image

from ebook_server.core import conversion, scan_pipeline
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    ScanJob,
    StorageLocation,
)
from ebook_server.jobs import (
    ConfiguredQueue,
    InlineQueue,
    is_stale_running,
    scheduler_active,
)

# ---------------------------------------------------------------- jobs.py


def test_inline_queue_does_not_forward_rq_options() -> None:
    seen: dict[str, Any] = {}

    def job(value: str, *, raise_on_error: bool = False) -> None:
        seen.update(value=value, raise_on_error=raise_on_error)

    InlineQueue().enqueue(job, "x", job_timeout=7200, retry=None, raise_on_error=True)
    assert seen == {"value": "x", "raise_on_error": True}


class _FakeRedisQueue:
    name = "ebook-server"

    def __init__(self, *, scheduler_lock: bool) -> None:
        self.scheduler_lock = scheduler_lock
        self.captured: dict[str, Any] = {}

        outer = self

        class Conn:
            def exists(self, key: str) -> int:
                assert key == "rq:scheduler-lock:ebook-server"
                return int(outer.scheduler_lock)

        self.connection = Conn()

    def enqueue(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.captured = kwargs


@override_settings(BOOK_SCANNER_QUEUE_RETRY_MAX=3, BOOK_SCANNER_QUEUE_RETRY_INTERVALS=[30, 120])
def test_retries_use_delay_only_when_a_scheduler_is_running() -> None:
    without = _FakeRedisQueue(scheduler_lock=False)
    ConfiguredQueue(cast(Any, without)).enqueue(str, 1)
    assert without.captured["retry"].max == 3
    assert without.captured["retry"].intervals == [0]
    assert scheduler_active(cast(Any, without)) is False

    with_scheduler = _FakeRedisQueue(scheduler_lock=True)
    ConfiguredQueue(cast(Any, with_scheduler)).enqueue(str, 1)
    assert with_scheduler.captured["retry"].intervals == [30, 120]


# ---------------------------------------------------------------- conversion


def test_converter_stderr_tail_is_in_error() -> None:
    with pytest.raises(conversion.ConversionError, match="boom-from-stderr"):
        conversion._run_conversion_command(
            ["sh", "-c", "echo boom-from-stderr >&2; exit 3"],
            check=True,
        )


def test_converter_timeout_kills_process_group() -> None:
    started = time.monotonic()
    with pytest.raises(conversion.ConversionError, match="timed out"):
        conversion._run_conversion_command(
            ["sh", "-c", "sleep 30 & sleep 30"],
            check=True,
            timeout=0.5,
        )
    assert time.monotonic() - started < 10


def test_converter_success_returns_stdout_when_captured() -> None:
    result = conversion._run_conversion_command(
        ["sh", "-c", "echo hello"], check=True, capture_output=True
    )
    assert result.stdout.strip() == "hello"


@pytest.mark.django_db(transaction=True)
def test_conversion_dispatch_failure_marks_job_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_conversion import StubConverter, _configure_storage, _create_book_with_variant

    storage = _configure_storage(tmp_path)
    variant = _create_book_with_variant(
        storage, source_fmt="epub", source_payload=b"epub", source_file_name="b.epub"
    )

    class DeadQueue:
        def enqueue(self, *args: Any, **kwargs: Any) -> None:
            raise ConnectionError("redis down")

    monkeypatch.setattr(conversion, "get_queue", lambda: DeadQueue())
    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": StubConverter(b"x")})
    job = conversion.request_conversion(source_variant=variant, target_format="pdf")
    assert job.state == ProcessingJob.JobState.FAILED
    assert "redis down" in job.details["error"]


# ---------------------------------------------------------------- PDF build


def test_build_pdf_streams_and_flattens_alpha(tmp_path: Path) -> None:
    rgb = tmp_path / "a.png"
    Image.new("RGB", (40, 60), (10, 20, 30)).save(rgb)
    rgba = tmp_path / "b.png"
    Image.new("RGBA", (40, 60), (200, 0, 0, 128)).save(rgba)
    assert b"/SMask" in img2pdf.convert([str(rgba)])  # un-normalised: soft mask / may be rejected

    out = scan_pipeline._build_pdf_from_images([rgb, rgba])
    try:
        data = out.read_bytes()
        assert data.startswith(b"%PDF")
        assert b"/SMask" not in data  # alpha was flattened to RGB
        assert b"/N 2" in data  # two pages
    finally:
        out.unlink(missing_ok=True)


def test_build_pdf_writes_to_stream_not_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rgb = tmp_path / "a.png"
    Image.new("RGB", (8, 8)).save(rgb)
    real = img2pdf.convert
    seen: dict[str, Any] = {}

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen["outputstream"] = kwargs.get("outputstream")
        return real(*args, **kwargs)

    monkeypatch.setattr(img2pdf, "convert", spy)
    out = scan_pipeline._build_pdf_from_images([rgb])
    out.unlink(missing_ok=True)
    assert seen["outputstream"] is not None


# ---------------------------------------------------------------- stale jobs


def _png() -> bytes:
    stream = io.BytesIO()
    Image.new("RGB", (16, 16)).save(stream, format="PNG")
    return stream.getvalue()


@pytest.mark.django_db
def test_is_stale_running_uses_job_timeout_plus_margin() -> None:
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
        state=ProcessingJob.JobState.RUNNING,
        started_at=timezone.now() - timedelta(seconds=30),
    )
    assert is_stale_running(job) is False
    job.started_at = timezone.now() - timedelta(days=2)
    assert is_stale_running(job) is True
    job.state = ProcessingJob.JobState.QUEUED
    assert is_stale_running(job) is False


@pytest.mark.django_db
def test_finalization_reenters_only_stale_running_jobs(monkeypatch: pytest.MonkeyPatch) -> None:
    scan = ScanJob.objects.create(
        provisional_title="Stale",
        state=ScanJob.State.FINALIZING,
    )
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
        state=ProcessingJob.JobState.RUNNING,
        started_at=timezone.now(),
        details={
            "scan_job_id": str(scan.id),
            "create_cleaned_variant": False,
            "prior_scan_state": "open",
            "attempt_count": 1,
        },
    )
    scan_pipeline.process_scan_finalization_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.RUNNING
    assert job.details["attempt_count"] == 1  # untouched: a live worker owns it

    job.started_at = timezone.now() - timedelta(days=2)
    job.save(update_fields=["started_at"])
    scan_pipeline.process_scan_finalization_job(str(job.job_id))
    job.refresh_from_db()
    scan.refresh_from_db()
    assert job.details["attempt_count"] == 2
    assert job.state == ProcessingJob.JobState.SUCCESS
    assert scan.state == ScanJob.State.COMPLETE


@pytest.mark.django_db
def test_recover_stale_jobs_dry_run_then_apply() -> None:
    scan = ScanJob.objects.create(provisional_title="Orphan", state=ScanJob.State.FINALIZING)
    stale = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
        state=ProcessingJob.JobState.RUNNING,
        started_at=timezone.now() - timedelta(days=2),
        details={"scan_job_id": str(scan.id), "prior_scan_state": "paused"},
    )
    fresh = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.OCR,
        state=ProcessingJob.JobState.RUNNING,
        started_at=timezone.now(),
    )

    out = StringIO()
    call_command("recover_stale_jobs", stdout=out)
    assert "would recover 1" in out.getvalue()
    stale.refresh_from_db()
    assert stale.state == ProcessingJob.JobState.RUNNING

    out = StringIO()
    call_command("recover_stale_jobs", "--apply", stdout=out)
    stale.refresh_from_db()
    fresh.refresh_from_db()
    scan.refresh_from_db()
    assert stale.state == ProcessingJob.JobState.FAILED
    assert "recover_stale_jobs" in stale.details["error"]
    assert fresh.state == ProcessingJob.JobState.RUNNING
    assert scan.state == ScanJob.State.PAUSED


@pytest.mark.django_db
def test_recover_stale_jobs_returns_finalizing_scan_without_any_job() -> None:
    scan = ScanJob.objects.create(provisional_title="NoJob", state=ScanJob.State.FINALIZING)
    call_command("recover_stale_jobs", "--apply", stdout=StringIO())
    scan.refresh_from_db()
    assert scan.state == ScanJob.State.PAUSED


class _ChunkJob:
    def __init__(self, job_id: str, func_name: str, args: tuple[Any, ...]) -> None:
        self.id = job_id
        self.func_name = func_name
        self.args = args


class _ChunkRegistry:
    def __init__(self, members: list[str]) -> None:
        self.members = members

    def get_job_ids(self, cleanup: bool = True) -> list[str]:
        del cleanup
        return list(self.members)


class _ChunkConnection:
    def exists(self, key: str) -> int:
        del key
        return 1


class _ChunkRQQueue:
    """Just enough of an rq.Queue for ``rq_pending_job_args``."""

    name = "default"

    def __init__(self, queued: list[_ChunkJob], started: list[_ChunkJob]) -> None:
        self.queued = queued
        self.jobs = {job.id: job for job in [*queued, *started]}
        self.started = _ChunkRegistry([f"{job.id}:exec-1" for job in started])
        self.connection = _ChunkConnection()

    def get_jobs(self) -> list[_ChunkJob]:
        return list(self.queued)

    def fetch_job(self, job_id: str) -> _ChunkJob | None:
        return self.jobs.get(job_id)


def _stage_job(age: timedelta) -> ProcessingJob:
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.STAGE,
        state=ProcessingJob.JobState.QUEUED,
    )
    ProcessingJob.objects.filter(pk=job.pk).update(scheduled_at=timezone.now() - age)
    return job


@pytest.mark.django_db
def test_recover_stale_jobs_keeps_every_member_of_a_queued_stage_chunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#195: a batch chunk queues ``process_stage_jobs([id, id, ...])`` as one RQ job."""
    from ebook_server import jobs

    old = timedelta(days=2)
    queued_a, queued_b = _stage_job(old), _stage_job(old)
    running_a, running_b = _stage_job(old), _stage_job(old)
    lost = _stage_job(old)
    func = "ebook_server.core.scantailor.process_stage_jobs"
    rq_queue = _ChunkRQQueue(
        queued=[_ChunkJob("chunk-1", func, ([str(queued_a.job_id), str(queued_b.job_id)],))],
        started=[_ChunkJob("chunk-2", func, ([str(running_a.job_id), str(running_b.job_id)],))],
    )

    class _Configured:
        queue = rq_queue

    monkeypatch.setattr(jobs, "get_queue", lambda name=None: _Configured())
    monkeypatch.setattr(
        jobs,
        "_live_registries",
        lambda inspected: [inspected.started, _ChunkRegistry([]), _ChunkRegistry([])],
    )

    ids, scheduler = jobs.rq_pending_job_args()
    assert ids == {
        str(queued_a.job_id),
        str(queued_b.job_id),
        str(running_a.job_id),
        str(running_b.job_id),
    }
    assert scheduler is True

    out = StringIO()
    call_command("recover_stale_jobs", "--apply", stdout=out)
    for job in (queued_a, queued_b, running_a, running_b, lost):
        job.refresh_from_db()
    assert [job.state for job in (queued_a, queued_b, running_a, running_b)] == [
        ProcessingJob.JobState.QUEUED
    ] * 4
    assert lost.state == ProcessingJob.JobState.FAILED
    assert "recovered 1 stale job(s)" in out.getvalue()


def test_rq_job_row_ids_expands_batch_arguments() -> None:
    from ebook_server.jobs import rq_job_row_ids

    assert rq_job_row_ids(_ChunkJob("a", "f", ("row-1",))) == ["row-1"]
    assert rq_job_row_ids(_ChunkJob("b", "f", (["row-1", "row-2"],))) == ["row-1", "row-2"]
    assert rq_job_row_ids(_ChunkJob("c", "f", (("row-3",), "extra"))) == ["row-3"]
    assert rq_job_row_ids(_ChunkJob("d", "f", ())) == []


# Silence unused-import linting for names kept for readability in fixtures.
_ = (Book, FileVariant, StorageLocation)
