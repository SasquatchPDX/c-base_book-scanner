"""Source ingestion batching, pausing, rate limiting, and orphan re-queueing (#11)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from ebook_server.core import intake
from ebook_server.core.models import ImportJob, SourceLocation, StorageLocation
from ebook_server.storage import LocalFilesystemStorage

COMMAND_MODULE = "ebook_server.core.management.commands.ingest_sources"


def _source(tmp_path: Path, files: int, **fields: Any) -> SourceLocation:
    root = tmp_path / "incoming"
    root.mkdir()
    for index in range(files):
        (root / f"book-{index}.txt").write_text(f"Plain-text book number {index}.")
    return SourceLocation.objects.create(
        name=f"src-{tmp_path.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
        **fields,
    )


@pytest.fixture
def queued(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record enqueued import job ids instead of running them (a slow worker)."""
    calls: list[str] = []

    def _record(func: Callable[..., Any], job_id: str, **_kwargs: Any) -> None:
        del func
        calls.append(job_id)

    monkeypatch.setattr(f"{COMMAND_MODULE}.enqueue", _record)
    return calls


def _rq_ids(monkeypatch: pytest.MonkeyPatch, ids: set[str] | None) -> None:
    monkeypatch.setattr(f"{COMMAND_MODULE}.rq_pending_job_args", lambda: (ids, True))


def _run(*args: str) -> str:
    out = StringIO()
    call_command("ingest_sources", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_batch_size_caps_jobs_in_flight_per_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: list[str]
) -> None:
    location = _source(tmp_path, 5, ingest_batch_size=2)
    _rq_ids(monkeypatch, set())

    output = _run("--source-id", str(location.id))

    assert len(queued) == 2
    assert ImportJob.objects.filter(source_location=location).count() == 2
    assert "limit reached" in output

    # Both jobs are still waiting in RQ: no new slots, and nothing is re-queued.
    _rq_ids(monkeypatch, set(queued))
    _run("--source-id", str(location.id))
    assert len(queued) == 2
    assert ImportJob.objects.filter(source_location=location).count() == 2

    # Once the batch has drained the next run queues the next slice.
    ImportJob.objects.filter(source_location=location).update(state=ImportJob.State.COMPLETE)
    _run("--source-id", str(location.id))
    assert len(queued) == 4


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_INGEST_BATCH_SIZE=3)
def test_global_batch_default_and_limit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: list[str]
) -> None:
    location = _source(tmp_path, 6)
    _rq_ids(monkeypatch, set())

    _run("--source-id", str(location.id), "--limit", "1")
    assert len(queued) == 1

    _rq_ids(monkeypatch, set(queued))
    _run("--source-id", str(location.id))
    assert len(queued) == 3  # global default of 3 in flight

    _rq_ids(monkeypatch, set(queued))
    _run("--source-id", str(location.id), "--limit", "0")  # unlimited
    assert len(queued) == 6


@pytest.mark.django_db(transaction=True)
def test_hourly_rate_limit_per_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: list[str]
) -> None:
    location = _source(tmp_path, 5, ingest_batch_size=0, ingest_max_per_hour=2)
    _rq_ids(monkeypatch, set())

    _run("--source-id", str(location.id))
    assert len(queued) == 2

    # Even with the batch drained, the rolling hour is used up.
    ImportJob.objects.filter(source_location=location).update(state=ImportJob.State.COMPLETE)
    _run("--source-id", str(location.id))
    assert len(queued) == 2

    ImportJob.objects.filter(source_location=location).update(
        created_at=timezone.now() - timedelta(hours=2),
    )
    _run("--source-id", str(location.id))
    assert len(queued) == 4


@pytest.mark.django_db(transaction=True)
def test_emptied_queue_orphans_are_requeued(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: list[str]
) -> None:
    location = _source(tmp_path, 3, ingest_batch_size=3)
    _rq_ids(monkeypatch, set())
    _run("--source-id", str(location.id))
    first_batch = list(queued)
    assert len(first_batch) == 3

    # `rq empty`: the DISCOVERED rows have no RQ job any more and hold every slot.
    _rq_ids(monkeypatch, set())
    output = _run("--source-id", str(location.id))

    assert sorted(queued[3:]) == sorted(first_batch)
    assert "requeued=3" in output


@pytest.mark.django_db(transaction=True)
def test_paused_source_queues_nothing_and_defers_waiting_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage_root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root)),
    )
    monkeypatch.setattr("ebook_server.core.identification._provider_instances", lambda: [])
    location = _source(tmp_path, 2, ingest_batch_size=0)
    job = intake.create_source_import_job(
        next(Path(location.root_path).iterdir()),
        location,
    )
    location.is_paused = True
    location.save(update_fields=["is_paused"])

    # A job already queued before the pause is not started.
    intake.process_import_job(str(job.id))
    job.refresh_from_db()
    assert job.state == ImportJob.State.DISCOVERED

    output = _run("--source-id", str(location.id))
    assert "paused" in output
    assert ImportJob.objects.filter(source_location=location).count() == 1

    # Resuming re-queues the waiting job and continues discovery (inline queue).
    location.is_paused = False
    location.save(update_fields=["is_paused"])
    _run("--source-id", str(location.id))
    job.refresh_from_db()
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert ImportJob.objects.filter(source_location=location).count() == 2
    assert not ImportJob.objects.filter(state=ImportJob.State.DISCOVERED).exists()


@pytest.mark.django_db(transaction=True)
def test_discovered_job_can_be_claimed_only_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location = _source(tmp_path, 1)
    job = intake.create_source_import_job(next(Path(location.root_path).iterdir()), location)
    stale_copy = ImportJob.objects.get(pk=job.pk)

    assert intake._claim_discovered_job(job) is True
    assert intake._claim_discovered_job(stale_copy) is False

    # A delivery that loaded the job as DISCOVERED but lost the claim does no work.
    job.state = ImportJob.State.DISCOVERED
    job.save(update_fields=["state"])
    monkeypatch.setattr(intake, "_claim_discovered_job", lambda _job: False)

    def _must_not_hash(_path: str) -> str:
        raise AssertionError("lost the claim but processed anyway")

    monkeypatch.setattr(intake, "compute_sha256", _must_not_hash)
    intake.process_import_job(str(job.id))
    job.refresh_from_db()
    assert job.state == ImportJob.State.DISCOVERED


@pytest.mark.django_db(transaction=True)
def test_orphans_not_requeued_when_rq_cannot_be_inspected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, queued: list[str]
) -> None:
    location = _source(tmp_path, 2, ingest_batch_size=2)
    _rq_ids(monkeypatch, set())
    _run("--source-id", str(location.id))
    assert len(queued) == 2

    _rq_ids(monkeypatch, None)
    monkeypatch.setattr(f"{COMMAND_MODULE}.queue_is_async", lambda: True)
    output = _run("--source-id", str(location.id))
    assert len(queued) == 2
    assert "cannot inspect" in output
