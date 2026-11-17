"""Exclusive ownership (lease) of an ImportJob across duplicate deliveries (#11, PR #69).

A job can be delivered more than once (orphan re-queue from a stale RQ snapshot, RQ
retries, operator re-queues). Only the delivery that claims the lease may work on it,
whatever phase a later delivery loads it in. Real concurrent connections are covered
in tests/integration/test_import_job_concurrency.py (PostgreSQL).
"""

from __future__ import annotations

from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import override_settings
from django.utils import timezone

from ebook_server.core import intake
from ebook_server.core.models import (
    FileVariant,
    ImportJob,
    ReviewTask,
    SourceLocation,
    StorageLocation,
)
from ebook_server.storage import LocalFilesystemStorage


@pytest.fixture
def source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SourceLocation:
    """A MOVE source with one book, an isolated archive, and no metadata providers."""
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
    (root / "lease-book.txt").write_text("A plain-text book about leases.")
    return SourceLocation.objects.create(
        name=f"lease-{tmp_path.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.MOVE,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
    )


def _job(source: SourceLocation) -> ImportJob:
    return intake.create_source_import_job(next(Path(source.root_path).iterdir()), source)


def _forbid_hashing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _sentinel(_path: str) -> str:
        raise AssertionError("second delivery entered intake while another worker owns it")

    monkeypatch.setattr(intake, "compute_sha256", _sentinel)


@pytest.mark.django_db(transaction=True)
def test_second_delivery_of_already_hashing_job_returns_without_work(
    source: SourceLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's repro: worker A claimed the job, delivery B loads it as HASHING."""
    job = _job(source)
    assert intake.claim_import_job(job) is True  # worker A
    owner = job.lease_owner

    _forbid_hashing(monkeypatch)
    intake.process_import_job(str(job.id))  # delivery B

    job.refresh_from_db()
    assert job.state == ImportJob.State.HASHING
    assert job.lease_owner == owner
    assert Path(job.source_path).exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("phase", intake.ACTIVE_IMPORT_STATES)
def test_duplicate_delivery_in_any_active_phase_does_nothing(
    source: SourceLocation, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    job = _job(source)
    assert intake.claim_import_job(job) is True
    ImportJob.objects.filter(pk=job.pk).update(state=phase)

    _forbid_hashing(monkeypatch)
    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == phase
    assert Path(job.source_path).exists()  # MOVE source untouched
    assert not FileVariant.objects.exists()
    assert not ReviewTask.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_expired_lease_is_not_taken_over_by_a_delivery(
    source: SourceLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stale jobs are recovered explicitly (recover_stale_jobs), never by a delivery."""
    job = _job(source)
    assert intake.claim_import_job(job) is True
    ImportJob.objects.filter(pk=job.pk).update(
        lease_expires_at=timezone.now() - timedelta(minutes=1),
    )

    _forbid_hashing(monkeypatch)
    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.HASHING


@pytest.mark.django_db(transaction=True)
def test_worker_that_loses_its_lease_stops_without_overwriting(
    source: SourceLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A worker whose lease is taken away mid-run writes nothing and deletes nothing."""
    job = _job(source)
    real_sha256 = intake.compute_sha256

    def _hash_then_lose_lease(path: str) -> str:
        digest = real_sha256(path)
        ImportJob.objects.filter(pk=job.pk).update(
            state=ImportJob.State.FAILED,
            lease_owner="",
            last_error="recovered elsewhere",
        )
        return digest

    monkeypatch.setattr(intake, "compute_sha256", _hash_then_lose_lease)
    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.FAILED
    assert job.last_error == "recovered elsewhere"
    assert job.sha256 == ""  # the METADATA_EXTRACTION write was refused
    assert Path(job.source_path).exists()
    assert not FileVariant.objects.exists()
    assert not ReviewTask.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_lease_is_released_when_the_job_leaves_the_active_phases(
    source: SourceLocation,
) -> None:
    job = _job(source)
    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.lease_owner == ""
    assert job.lease_expires_at is None
    # A late duplicate delivery of the finished job does nothing.
    intake.process_import_job(str(job.id))
    assert ReviewTask.objects.filter(import_job=job).count() == 1


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_QUEUE_RETRY_MAX=3)
def test_failed_job_with_retry_due_is_claimed_once(source: SourceLocation) -> None:
    job = _job(source)
    ImportJob.objects.filter(pk=job.pk).update(
        state=ImportJob.State.FAILED,
        attempt_count=1,
        next_retry_at=timezone.now(),
    )
    first = ImportJob.objects.get(pk=job.pk)
    second = ImportJob.objects.get(pk=job.pk)

    assert intake.claim_import_job(first) is True
    assert intake.claim_import_job(second) is False
    assert ImportJob.objects.get(pk=job.pk).lease_owner == first.lease_owner


def _recover(*args: str) -> str:
    out = StringIO()
    call_command("recover_stale_jobs", *args, stdout=out)
    return out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_recover_stale_jobs_fails_expired_lease_and_retry_failed_requeues(
    source: SourceLocation, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _job(source)
    assert intake.claim_import_job(job) is True  # worker that will "die"
    ImportJob.objects.filter(pk=job.pk).update(
        state=ImportJob.State.ARCHIVING,
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )

    output = _recover()
    assert f"stale import {job.id}" in output
    assert ImportJob.objects.get(pk=job.pk).state == ImportJob.State.ARCHIVING  # dry-run

    _recover("--apply")
    recovered = ImportJob.objects.get(pk=job.pk)
    assert recovered.state == ImportJob.State.FAILED
    assert recovered.lease_owner == ""
    assert recovered.next_retry_at is None
    assert "import lease expired in archiving" in recovered.last_error

    # The dead worker, should it wake up, cannot write any more.
    job.state = ImportJob.State.COMPLETE
    with pytest.raises(intake.ImportLeaseLostError):
        intake._save_job(job, ["state", "updated_at"])
    assert ImportJob.objects.get(pk=job.pk).state == ImportJob.State.FAILED

    # The explicit retry path re-queues it, and it can be claimed again.
    queued: list[str] = []

    def _record(_func: Any, job_id: str, **_kwargs: Any) -> None:
        queued.append(job_id)

    monkeypatch.setattr("ebook_server.core.management.commands.ingest_sources.enqueue", _record)
    call_command(
        "ingest_sources", "--source-id", str(source.id), "--retry-failed", stdout=StringIO()
    )
    assert queued == [str(job.id)]
    retried = ImportJob.objects.get(pk=job.pk)
    assert retried.state == ImportJob.State.DISCOVERED
    assert intake.claim_import_job(retried) is True


@pytest.mark.django_db(transaction=True)
def test_recover_stale_jobs_leaves_live_leases_alone(source: SourceLocation) -> None:
    job = _job(source)
    assert intake.claim_import_job(job) is True

    _recover("--apply")

    job.refresh_from_db()
    assert job.state == ImportJob.State.HASHING
    assert job.lease_owner != ""


@pytest.mark.django_db(transaction=True)
def test_recover_skips_a_job_whose_lease_was_renewed_meanwhile(source: SourceLocation) -> None:
    job = _job(source)
    assert intake.claim_import_job(job) is True
    ImportJob.objects.filter(pk=job.pk).update(
        lease_expires_at=timezone.now() - timedelta(seconds=1),
    )
    [stale] = intake.stale_import_jobs()
    # The worker renews its lease between the scan and the recovery.
    intake._save_job(job, ["state", "updated_at"])

    assert intake.recover_stale_import_job(stale, "test") is False
    assert ImportJob.objects.get(pk=job.pk).state == ImportJob.State.HASHING


@pytest.mark.django_db(transaction=True)
def test_unleased_active_job_counts_as_stale_after_a_lease_duration(
    source: SourceLocation,
) -> None:
    """Rows left mid-flight by a worker from before leases existed are recoverable."""
    job = _job(source)
    ImportJob.objects.filter(pk=job.pk).update(state=ImportJob.State.HASHING)
    assert intake.stale_import_jobs() == []

    ImportJob.objects.filter(pk=job.pk).update(
        updated_at=timezone.now() - intake.import_lease_duration() - timedelta(seconds=1),
    )
    assert [stale.pk for stale in intake.stale_import_jobs()] == [job.pk]


@override_settings(BOOK_SCANNER_INGEST_LEASE_SECONDS=0, BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS=600)
def test_default_lease_outlives_the_queue_job_timeout() -> None:
    assert intake.import_lease_duration() == timedelta(seconds=900)


@override_settings(BOOK_SCANNER_INGEST_LEASE_SECONDS=120)
def test_lease_duration_can_be_configured() -> None:
    assert intake.import_lease_duration() == timedelta(seconds=120)
