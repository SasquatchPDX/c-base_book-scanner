"""ImportJob ownership under real concurrency: threads on separate PostgreSQL connections.

Deterministic, single-connection versions of these cases are in
tests/test_import_job_lease.py.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.db import connections

from ebook_server.core import intake
from ebook_server.core.models import (
    ImportJob,
    ReviewTask,
    SourceLocation,
    StorageLocation,
)
from ebook_server.storage import LocalFilesystemStorage

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]

WAIT_SECONDS = 20


def _in_thread(target: Callable[[], Any], errors: list[BaseException]) -> threading.Thread:
    """Run ``target`` on its own thread, hence its own database connection."""

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


@pytest.fixture
def source_job(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ImportJob:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage_root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        intake, "_default_storage", lambda: LocalFilesystemStorage(str(storage_root))
    )
    monkeypatch.setattr("ebook_server.core.identification._provider_instances", lambda: [])
    root = tmp_path / "incoming"
    root.mkdir()
    (root / "concurrent-book.txt").write_text("A plain-text book read by two workers.")
    location = SourceLocation.objects.create(
        name=f"concurrency-{tmp_path.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.MOVE,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
    )
    return intake.create_source_import_job(next(root.iterdir()), location)


@pytest.mark.django_db(transaction=True)
def test_concurrent_claims_on_postgresql_have_exactly_one_winner(
    source_job: ImportJob,
) -> None:
    workers = 8
    barrier = threading.Barrier(workers)
    results: list[bool] = []
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _claim() -> None:
        copy = ImportJob.objects.get(pk=source_job.pk)  # every delivery reads DISCOVERED
        barrier.wait(timeout=WAIT_SECONDS)
        won = intake.claim_import_job(copy)
        with lock:
            results.append(won)

    threads = [_in_thread(_claim, errors) for _ in range(workers)]
    for thread in threads:
        thread.join(WAIT_SECONDS)

    assert not errors
    assert sorted(results) == [False] * (workers - 1) + [True]
    assert ImportJob.objects.get(pk=source_job.pk).state == ImportJob.State.HASHING


@pytest.mark.django_db(transaction=True)
def test_duplicate_delivery_while_first_worker_is_hashing_on_postgresql(
    source_job: ImportJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Worker A is inside HASHING; delivery B, on another connection, must do nothing."""
    real_sha256 = intake.compute_sha256
    hashing_started = threading.Event()
    release_a = threading.Event()
    calls: list[str] = []

    def _blocking_sha256(path: str) -> str:
        calls.append(threading.current_thread().name)
        hashing_started.set()
        assert release_a.wait(WAIT_SECONDS)
        return real_sha256(path)

    monkeypatch.setattr(intake, "compute_sha256", _blocking_sha256)
    errors: list[BaseException] = []
    worker_a = _in_thread(lambda: intake.process_import_job(str(source_job.id)), errors)
    assert hashing_started.wait(WAIT_SECONDS)
    assert ImportJob.objects.get(pk=source_job.pk).state == ImportJob.State.HASHING

    delivery_b = _in_thread(lambda: intake.process_import_job(str(source_job.id)), errors)
    delivery_b.join(WAIT_SECONDS)
    assert not delivery_b.is_alive()
    assert len(calls) == 1  # B returned without hashing

    release_a.set()
    worker_a.join(WAIT_SECONDS)
    assert not worker_a.is_alive()
    assert not errors

    job = ImportJob.objects.get(pk=source_job.pk)
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.lease_owner == ""
    assert ReviewTask.objects.filter(import_job=job).count() == 1


@pytest.mark.django_db(transaction=True)
def test_owner_check_holds_the_row_lock_until_its_save_commits(
    source_job: ImportJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recovery that takes the lease waits for an in-flight owned save, then wins.

    The owned save locks the row by primary key (SELECT ... FOR UPDATE) before it
    compares the owner, so nothing can change the owner between that check and the
    save. The concurrent takeover blocks until the save commits; the worker's next
    save then fails instead of overwriting the takeover.
    """
    assert intake.claim_import_job(source_job) is True
    original_save = ImportJob.save
    locked = threading.Event()
    proceed = threading.Event()
    takeover_done = threading.Event()
    errors: list[BaseException] = []
    pausing = threading.local()

    def _pausing_save(self: ImportJob, *args: Any, **kwargs: Any) -> None:
        # Runs inside _save_job's transaction, after the row lock and owner check.
        if getattr(pausing, "active", False):
            locked.set()
            assert proceed.wait(WAIT_SECONDS)
        original_save(self, *args, **kwargs)

    def _worker_save() -> None:
        pausing.active = True
        source_job.state = ImportJob.State.METADATA_EXTRACTION
        intake._save_job(source_job, ["state", "updated_at"])

    def _takeover() -> None:
        ImportJob.objects.filter(pk=source_job.pk).update(
            state=ImportJob.State.FAILED,
            lease_owner="",
            last_error="taken over",
        )
        takeover_done.set()

    monkeypatch.setattr(ImportJob, "save", _pausing_save)
    worker = _in_thread(_worker_save, errors)
    assert locked.wait(WAIT_SECONDS)
    rival = _in_thread(_takeover, errors)
    # The takeover's UPDATE blocks on the worker's row lock.
    assert not takeover_done.wait(1.0)
    proceed.set()
    worker.join(WAIT_SECONDS)
    rival.join(WAIT_SECONDS)
    monkeypatch.setattr(ImportJob, "save", original_save)

    assert not errors
    assert takeover_done.is_set()
    job = ImportJob.objects.get(pk=source_job.pk)
    assert job.state == ImportJob.State.FAILED
    assert job.last_error == "taken over"
    source_job.state = ImportJob.State.SIGNAL_NORMALIZATION
    with pytest.raises(intake.ImportLeaseLostError):
        intake._save_job(source_job, ["state", "updated_at"])


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("next_phase", "expect_cancelled"),
    [
        (ImportJob.State.SIGNAL_NORMALIZATION, True),
        (ImportJob.State.ARCHIVING, False),
    ],
)
def test_cancellation_cannot_commit_between_owner_check_and_phase_save(
    source_job: ImportJob,
    monkeypatch: pytest.MonkeyPatch,
    next_phase: str,
    expect_cancelled: bool,
) -> None:
    """Review of #75: the worker's check-and-save must exclude a concurrent cancel.

    The worker is paused inside its phase save, after the row lock and the state
    check. ``cancel_source_imports`` on another connection must wait for that save to
    commit, then decide on the committed state: a pre-archive phase is cancelled (and
    the worker cannot write again), a job that reached ARCHIVING is left to finish.
    Either way the worker never overwrites CANCELLED.
    """
    assert intake.claim_import_job(source_job) is True
    location = source_job.source_location
    assert location is not None
    original_save = ImportJob.save
    locked = threading.Event()
    proceed = threading.Event()
    cancel_done = threading.Event()
    cancelled_ids: list[str] = []
    errors: list[BaseException] = []
    pausing = threading.local()

    def _pausing_save(self: ImportJob, *args: Any, **kwargs: Any) -> None:
        if getattr(pausing, "active", False):
            locked.set()
            assert proceed.wait(WAIT_SECONDS)
        original_save(self, *args, **kwargs)

    def _worker_phase_save() -> None:
        pausing.active = True
        source_job.state = next_phase
        intake._save_job(source_job, ["state", "updated_at"])

    def _cancel() -> None:
        cancelled_ids.extend(intake.cancel_source_imports(location))
        cancel_done.set()

    monkeypatch.setattr(ImportJob, "save", _pausing_save)
    worker = _in_thread(_worker_phase_save, errors)
    assert locked.wait(WAIT_SECONDS)
    canceller = _in_thread(_cancel, errors)
    # The cancellation blocks on the row lock the worker holds for its check-and-save.
    assert not cancel_done.wait(1.0)
    proceed.set()
    worker.join(WAIT_SECONDS)
    canceller.join(WAIT_SECONDS)
    monkeypatch.setattr(ImportJob, "save", original_save)

    assert not errors
    assert cancel_done.is_set()
    job = ImportJob.objects.get(pk=source_job.pk)
    if expect_cancelled:
        assert cancelled_ids == [str(source_job.pk)]
        assert job.state == ImportJob.State.CANCELLED
        assert job.lease_owner == ""
        # The worker's next phase save is refused; CANCELLED is never overwritten.
        source_job.state = ImportJob.State.ARCHIVING
        with pytest.raises(intake.ImportCancelledError):
            intake._save_job(source_job, ["state", "updated_at"])
        assert ImportJob.objects.get(pk=source_job.pk).state == ImportJob.State.CANCELLED
    else:
        assert cancelled_ids == []
        assert job.state == ImportJob.State.ARCHIVING
        assert job.lease_owner == source_job.lease_owner


@pytest.mark.django_db(transaction=True)
def test_cancel_while_worker_runs_intake_on_postgresql(
    source_job: ImportJob, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: cancel from another connection while the worker is hashing."""
    real_sha256 = intake.compute_sha256
    hashing_started = threading.Event()
    release = threading.Event()

    def _blocking_sha256(path: str) -> str:
        hashing_started.set()
        assert release.wait(WAIT_SECONDS)
        return real_sha256(path)

    monkeypatch.setattr(intake, "compute_sha256", _blocking_sha256)
    errors: list[BaseException] = []
    worker = _in_thread(lambda: intake.process_import_job(str(source_job.id)), errors)
    assert hashing_started.wait(WAIT_SECONDS)

    location = source_job.source_location
    assert location is not None
    canceller = _in_thread(lambda: intake.cancel_source_imports(location), errors)
    canceller.join(WAIT_SECONDS)
    release.set()
    worker.join(WAIT_SECONDS)

    assert not errors  # the worker returned cleanly (no FAILED, no RQ retry)
    job = ImportJob.objects.get(pk=source_job.pk)
    assert job.state == ImportJob.State.CANCELLED
    assert job.sha256 == ""
    assert Path(job.source_path).exists()
    assert not ReviewTask.objects.filter(import_job=job).exists()
