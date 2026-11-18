"""Real PostgreSQL races between collection, joined editor locks and publication."""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.db import connection, connections, transaction
from django.utils import timezone

from ebook_server.core import derivative_retention as retirement
from ebook_server.core import scantailor
from ebook_server.core.models import ProcessingJob, ScanTailorStageState
from ebook_server.storage import LocalFilesystemStorage
from tests.test_derivative_retention import make_retirement_case, plan_for_case

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires PostgreSQL infrastructure",
    ),
]
WAIT = 20


@pytest.fixture(autouse=True)
def guarded_scratch_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _relaxed_storage_root_and_source_age: None
) -> None:
    """Exercise real mount guards against scratch, never the configured site root."""
    (tmp_path / ".review-storage").write_text("scratch", encoding="utf-8")
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    monkeypatch.setattr("django.conf.settings.STORAGE_REQUIRE_EXISTING_ROOT", True)
    monkeypatch.setattr("django.conf.settings.STORAGE_SENTINEL_FILE", ".review-storage")


def in_thread(target: Callable[[], Any], errors: list[BaseException]) -> threading.Thread:
    def run() -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def test_joined_editor_lock_and_new_reference_do_not_deadlock_collection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert connection.vendor == "postgresql"
    location, storage, state, old = make_retirement_case(tmp_path, monkeypatch)
    plan = plan_for_case()
    entered = threading.Event()
    release = threading.Event()
    collector_bound = threading.Event()
    errors: list[BaseException] = []
    results: list[dict[str, Any]] = []
    real_binding = retirement._binding

    def bound_in_transaction(bound_location: Any, root: str) -> None:
        real_binding(bound_location, root)
        if connection.in_atomic_block:
            collector_bound.set()

    monkeypatch.setattr(retirement, "_binding", bound_in_transaction)

    def editor() -> None:
        with transaction.atomic():
            locked = (
                ScanTailorStageState.objects.select_for_update()
                .select_related("scan_job", "scan_page")
                .get(pk=state.pk)
            )
            entered.set()
            assert release.wait(WAIT)
            locked.preview_storage_location = location
            locked.preview_storage_identifier = old
            locked.save(update_fields=["preview_storage_location", "preview_storage_identifier"])

    first = in_thread(editor, errors)
    assert entered.wait(WAIT)

    def collector() -> None:
        results.extend(retirement.collect_plan(plan, apply=True))

    second = in_thread(collector, errors)
    try:
        assert collector_bound.wait(WAIT)
    finally:
        release.set()
        first.join(WAIT)
        second.join(WAIT)
    assert not first.is_alive() and not second.is_alive()
    assert not errors
    assert results[0]["status"] == "skipped"
    assert storage.resolve_location(old).read_bytes() == b"old preview"


def test_two_collectors_never_move_the_same_file_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert connection.vendor == "postgresql"
    _, storage, _, old = make_retirement_case(tmp_path, monkeypatch)
    plan = plan_for_case()
    barrier = threading.Barrier(2)
    hashed = threading.Barrier(2)
    errors: list[BaseException] = []
    results: list[dict[str, Any]] = []
    real_hash = LocalFilesystemStorage.hash

    def both_prechecked(self: LocalFilesystemStorage, identifier: str) -> str:
        digest = real_hash(self, identifier)
        if identifier == old and threading.current_thread() is not threading.main_thread():
            hashed.wait(WAIT)
        return digest

    monkeypatch.setattr(LocalFilesystemStorage, "hash", both_prechecked)

    def collector() -> None:
        barrier.wait(WAIT)
        results.extend(retirement.collect_plan(plan, apply=True))

    threads = [in_thread(collector, errors) for _ in range(2)]
    for thread in threads:
        thread.join(WAIT)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    assert sorted(result["status"] for result in results) == ["quarantined", "skipped"]
    assert not storage.exists(old)
    assert len(list(storage.resolve_location(".quarantine").rglob("artifact"))) == 1


def test_superseded_worker_cannot_recreate_a_quarantined_final_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert connection.vendor == "postgresql"
    location, storage, state, old = make_retirement_case(tmp_path, monkeypatch)
    ScanTailorStageState.objects.filter(pk=state.pk).update(
        revision=1,
        output_storage_identifier="",
        output_storage_location=None,
        preview_storage_identifier="",
        preview_storage_location=None,
    )
    worker = ProcessingJob.objects.create(
        job_type="stage",
        details={
            "scan_job_id": state.scan_job_id,
            "scan_tailor_stage_state_id": state.pk,
            "stage_revision": 1,
            "stage": "deskew",
        },
    )
    copying = threading.Event()
    release = threading.Event()
    first_copy: list[bool] = []
    real_store = LocalFilesystemStorage.store_atomic

    def blocked_copy(self: LocalFilesystemStorage, source: str, destination: str) -> str:
        if destination.endswith(".output.partial") and not first_copy:
            first_copy.append(True)
            copying.set()
            assert release.wait(WAIT)
        return real_store(self, source, destination)

    monkeypatch.setattr(LocalFilesystemStorage, "store_atomic", blocked_copy)
    errors: list[BaseException] = []
    thread = in_thread(lambda: scantailor.process_stage_job(str(worker.job_id)), errors)
    try:
        assert copying.wait(WAIT)
        # A new edit supersedes the still-copying worker. Recovery already closed
        # its ProcessingJob, so DB activity flags alone would not protect the file.
        ScanTailorStageState.objects.filter(pk=state.pk).update(revision=2, status="pending")
        ProcessingJob.objects.filter(pk=worker.pk).update(
            state="failed", finished_at=timezone.now()
        )
        result = retirement.collect_plan(plan_for_case(), apply=True)[0]
        assert result["status"] == "quarantined"
        winner = ProcessingJob.objects.create(
            job_type="stage",
            details={
                "scan_job_id": state.scan_job_id,
                "scan_tailor_stage_state_id": state.pk,
                "stage_revision": 2,
                "stage": "deskew",
            },
        )
        scantailor.process_stage_job(str(winner.job_id))
        winner.refresh_from_db()
        assert winner.state == "success", winner.details
        state.refresh_from_db()
        winning_identifier = state.output_storage_identifier
        winning_bytes = storage.resolve_location(winning_identifier).read_bytes()
    finally:
        release.set()
        thread.join(WAIT)
    assert not thread.is_alive() and not errors
    state.refresh_from_db()
    assert state.status == "success" and state.revision == 2
    assert not storage.exists(old)
    assert storage.resolve_location(winning_identifier).read_bytes() == winning_bytes
    assert (
        storage.resolve_location(
            result["manifest"].replace("manifest.json", "artifact")
        ).read_bytes()
        == b"old preview"
    )
    assert not list(Path(storage.root).rglob("*.partial"))
