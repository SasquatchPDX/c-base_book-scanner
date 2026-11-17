"""Cancelling a source import run cleanly (#21)."""

from __future__ import annotations

from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command

from ebook_server import jobs
from ebook_server.core import identification, intake
from ebook_server.core.management.commands.cancel_source_import import IMPORT_JOB_FUNC
from ebook_server.core.models import Book, ImportJob, ReviewTask, SourceLocation
from ebook_server.storage import LocalFilesystemStorage


@pytest.fixture
def storage_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(root)),
    )
    return root


def _source(tmp_path: Path, files: int) -> SourceLocation:
    root = tmp_path / "incoming"
    root.mkdir()
    for index in range(files):
        (root / f"book-{index}.txt").write_text(f"Plain-text book number {index}.")
    return SourceLocation.objects.create(
        name=f"src-{tmp_path.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.MOVE,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
        ingest_batch_size=0,
    )


def _jobs(location: SourceLocation) -> list[ImportJob]:
    return [
        intake.create_source_import_job(path, location)
        for path in sorted(Path(location.root_path).iterdir())
    ]


@pytest.mark.django_db(transaction=True)
def test_cancel_marks_unarchived_jobs_and_pauses_source(tmp_path: Path) -> None:
    location = _source(tmp_path, 5)
    discovered, hashing, archiving, low_confidence, complete = _jobs(location)
    for job, state in (
        (hashing, ImportJob.State.HASHING),
        (archiving, ImportJob.State.ARCHIVING),
        (low_confidence, ImportJob.State.LOW_CONFIDENCE),
        (complete, ImportJob.State.COMPLETE),
    ):
        ImportJob.objects.filter(pk=job.pk).update(state=state)

    dry_run = StringIO()
    call_command("cancel_source_import", "--source-id", str(location.id), stdout=dry_run)
    assert "would pause and cancel 2 job(s)" in dry_run.getvalue()
    assert not ImportJob.objects.filter(state=ImportJob.State.CANCELLED).exists()

    out = StringIO()
    call_command("cancel_source_import", "--source-id", str(location.id), "--apply", stdout=out)

    states = {
        job.pk: ImportJob.objects.get(pk=job.pk).state
        for job in (discovered, hashing, archiving, low_confidence, complete)
    }
    assert states == {
        discovered.pk: ImportJob.State.CANCELLED,
        hashing.pk: ImportJob.State.CANCELLED,
        archiving.pk: ImportJob.State.ARCHIVING,
        low_confidence.pk: ImportJob.State.LOW_CONFIDENCE,
        complete.pk: ImportJob.State.COMPLETE,
    }
    location.refresh_from_db()
    assert location.is_paused
    assert "cancelled 2 job(s)" in out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_resurrected_cancelled_job_does_nothing(tmp_path: Path, storage_root: Path) -> None:
    location = _source(tmp_path, 1)
    (job,) = _jobs(location)
    intake.cancel_source_imports(location)
    # Resuming the source must not make a stale RQ redelivery run the job either.
    SourceLocation.objects.filter(pk=location.pk).update(is_paused=False)

    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.CANCELLED
    assert not Book.objects.exists()
    assert Path(job.source_path).exists()
    assert not storage_root.exists()


@pytest.mark.django_db(transaction=True)
def test_cancel_during_metadata_lookup_stops_before_archiving(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage_root: Path
) -> None:
    location = _source(tmp_path, 1)
    (job,) = _jobs(location)

    def _lookup_then_cancel(_signals: Any) -> list[Any]:
        intake.cancel_source_imports(location)
        return []

    monkeypatch.setattr(identification, "resolve_candidates", _lookup_then_cancel)

    intake.process_import_job(str(job.id))  # returns cleanly: no RQ retry

    job.refresh_from_db()
    assert job.state == ImportJob.State.CANCELLED
    assert not ReviewTask.objects.exists()
    assert not Book.objects.exists()
    assert Path(job.source_path).exists()  # MOVE source untouched
    assert not storage_root.exists()


@pytest.mark.django_db(transaction=True)
def test_cancel_during_hashing_is_not_overwritten_by_next_phase(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, storage_root: Path
) -> None:
    location = _source(tmp_path, 1)
    (job,) = _jobs(location)
    original = intake.compute_sha256

    def _hash_then_cancel(path: str) -> str:
        intake.cancel_source_imports(location)
        return original(path)

    monkeypatch.setattr(intake, "compute_sha256", _hash_then_cancel)

    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.CANCELLED
    assert job.sha256 == ""
    assert not storage_root.exists()


@pytest.mark.django_db(transaction=True)
def test_cancelled_files_are_rediscovered_after_resume(tmp_path: Path) -> None:
    location = _source(tmp_path, 2)
    _jobs(location)
    intake.cancel_source_imports(location)
    location.is_paused = False
    location.save(update_fields=["is_paused"])

    rediscovered = intake.discover_source_jobs_for_location(location)

    assert len(rediscovered) == 2


@dataclass
class FakeJob:
    id: str
    func_name: str
    args: tuple[str, ...]
    deleted: bool = False

    def delete(self) -> None:
        self.deleted = True


@dataclass
class FakeConnection:
    members: dict[str, list[str]]

    def zrem(self, key: str, member: str) -> None:
        self.members[key].remove(member)


@dataclass
class FakeRegistry:
    key: str
    connection: FakeConnection

    def get_job_ids(self, cleanup: bool = True) -> list[str]:
        del cleanup
        return list(self.connection.members[self.key])


@dataclass
class FakeQueue:
    jobs: dict[str, FakeJob]
    queued: list[str]
    removed: list[str] = field(default_factory=list)

    def get_job_ids(self) -> list[str]:
        return list(self.queued)

    def fetch_job(self, job_id: str) -> FakeJob | None:
        return self.jobs.get(job_id)

    def remove(self, job_id: str) -> None:
        self.removed.append(job_id)
        if job_id in self.queued:
            self.queued.remove(job_id)


def test_discard_rq_jobs_clears_queue_and_every_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    other_func = "ebook_server.core.conversion.run_conversion"
    fake_jobs = {
        "queued": FakeJob("queued", IMPORT_JOB_FUNC, ("job-a",)),
        "running": FakeJob("running", IMPORT_JOB_FUNC, ("job-b",)),
        "retry": FakeJob("retry", IMPORT_JOB_FUNC, ("job-c",)),
        "keep-other-row": FakeJob("keep-other-row", IMPORT_JOB_FUNC, ("job-z",)),
        "keep-other-func": FakeJob("keep-other-func", other_func, ("job-a",)),
    }
    connection = FakeConnection(
        members={
            "started": ["running:exec-1"],  # rq 2 stores <job_id>:<execution_id>
            "scheduled": ["retry"],
            "deferred": [],
            "failed": ["keep-other-row"],
        },
    )
    queue = FakeQueue(jobs=fake_jobs, queued=["queued", "keep-other-func"])
    registries = [FakeRegistry(key, connection) for key in connection.members]

    @dataclass
    class FakeConfiguredQueue:
        queue: FakeQueue

    monkeypatch.setattr(jobs, "get_queue", lambda: FakeConfiguredQueue(queue))
    monkeypatch.setattr(jobs, "_rq_registries", lambda _queue: registries)

    removed = jobs.discard_rq_jobs(IMPORT_JOB_FUNC, {"job-a", "job-b", "job-c"})

    assert removed == 3
    assert queue.queued == ["keep-other-func"]
    assert connection.members == {
        "started": [],
        "scheduled": [],
        "deferred": [],
        "failed": ["keep-other-row"],
    }
    assert {job.id for job in fake_jobs.values() if job.deleted} == {"queued", "running", "retry"}


def test_discard_rq_jobs_without_queue_reports_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(jobs, "get_queue", lambda: None)
    assert jobs.discard_rq_jobs(IMPORT_JOB_FUNC, {"job-a"}) is None
