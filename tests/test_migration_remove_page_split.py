"""Migration 0049 (#195): Page Split rows go, the remaining stages are relinked.

Jobs that can still be processed have their processed stages invalidated; finished
jobs (finalizing, complete, cancelled) keep their stage results, because the lifecycle
does not allow re-running stages in those states.
"""

from __future__ import annotations

from typing import Any

import pytest

BEFORE = [("core", "0048_worker_failure_reviews")]
AFTER = [("core", "0049_remove_page_split_reorder_stages")]
OLD_ORDER = ("page_split", "deskew", "select_content", "margins", "dewarping", "output")


@pytest.mark.django_db(transaction=True)
def test_page_split_rows_are_removed_and_stages_relinked_in_new_order() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    storage = old_apps.get_model("core", "StorageLocation").objects.create(
        identifier="scan-migration-test", backend="local", base_path="scan"
    )
    job = old_apps.get_model("core", "ScanJob").objects.create(
        id="20261118T120000Z", capture_layout="single_page"
    )
    page = old_apps.get_model("core", "ScanPage").objects.create(
        scan_job=job, logical_sequence_number=1
    )
    versions = old_apps.get_model("core", "ScanPageVersion").objects
    versions.create(
        scan_page=page,
        storage_location=storage,
        storage_identifier="raw/v1.png",
        checksum_sha256="a" * 64,
        mime_type="image/png",
        size_bytes=1,
        version=1,
        is_active=False,
    )
    active = versions.create(
        scan_page=page,
        storage_location=storage,
        storage_identifier="raw/v2.png",
        checksum_sha256="b" * 64,
        mime_type="image/png",
        size_bytes=1,
        version=2,
        is_active=True,
    )
    stage_model = old_apps.get_model("core", "ScanTailorStageState")
    previous = None
    created = {}
    for stage in OLD_ORDER:
        previous = stage_model.objects.create(
            scan_job=job,
            scan_page=page,
            stage=stage,
            status="success" if stage != "output" else "unknown",
            revision=3,
            input_stage_state=previous,
            input_storage_identifier="",
            output_storage_identifier=f"{stage}/out.png",
        )
        created[stage] = previous

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new_apps = executor.loader.project_state(AFTER).apps
    states = {
        state.stage: state
        for state in new_apps.get_model("core", "ScanTailorStageState").objects.filter(
            scan_page_id=page.pk
        )
    }

    assert set(states) == {"deskew", "select_content", "dewarping", "margins", "output"}
    deskew = states["deskew"]
    assert deskew.input_stage_state_id is None
    assert deskew.input_scan_page_version_id == active.pk
    assert deskew.input_storage_identifier == "raw/v2.png"
    assert states["select_content"].input_stage_state_id == deskew.pk
    assert states["dewarping"].input_stage_state_id == states["select_content"].pk
    assert states["margins"].input_stage_state_id == states["dewarping"].pk
    assert states["output"].input_stage_state_id == states["margins"].pk
    for stage in ("deskew", "select_content", "dewarping", "margins"):
        assert states[stage].status == "invalidated"
        assert states[stage].revision == 4
        assert "#195" in states[stage].invalidated_reason
        # Previous artifacts stay recorded; nothing is deleted from storage.
        assert states[stage].output_storage_identifier == f"{stage}/out.png"
    # Never-run stages stay untouched.
    assert states["output"].status == "unknown"
    assert states["output"].revision == 3
    assert not hasattr(new_apps.get_model("core", "ScanJob")(), "capture_layout")

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


class _RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[tuple[Any, ...]] = []

    def enqueue(self, *args: Any, **kwargs: Any) -> None:
        self.enqueued.append(args)


@pytest.mark.django_db(transaction=True)
def test_completed_scan_keeps_its_results_and_can_still_be_refinished(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor
    from django.test import Client

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    storage = old_apps.get_model("core", "StorageLocation").objects.create(
        identifier="scan-migration-complete", backend="local", base_path="scan"
    )
    book = old_apps.get_model("core", "Book").objects.create(title="Finished Before #195")
    jobs = old_apps.get_model("core", "ScanJob").objects
    complete = jobs.create(
        id="20261118T130000Z", capture_layout="single_page", state="complete", book=book
    )
    cancelled = jobs.create(id="20261118T130100Z", capture_layout="single_page", state="cancelled")
    captured = jobs.create(id="20261118T130200Z", capture_layout="single_page", state="captured")
    stage_model = old_apps.get_model("core", "ScanTailorStageState")
    pages = {}
    for job in (complete, cancelled, captured):
        page = old_apps.get_model("core", "ScanPage").objects.create(
            scan_job=job, logical_sequence_number=1
        )
        old_apps.get_model("core", "ScanPageVersion").objects.create(
            scan_page=page,
            storage_location=storage,
            storage_identifier=f"raw/{job.pk}.png",
            checksum_sha256="c" * 64,
            mime_type="image/png",
            size_bytes=1,
            version=1,
            is_active=True,
        )
        previous = None
        for stage in OLD_ORDER:
            previous = stage_model.objects.create(
                scan_job=job,
                scan_page=page,
                stage=stage,
                status="success",
                revision=2,
                input_stage_state=previous,
                output_storage_identifier=f"{job.pk}/{stage}/out.png",
            )
        pages[job.pk] = page.pk

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())

    from ebook_server.core import scan_pipeline, scantailor
    from ebook_server.core.models import (
        Book,
        ScanJob,
        ScanPage,
        ScanPageVersion,
        ScanTailorStageState,
        get_or_create_profile,
    )
    from tests.test_scan_jobs import _png_for, _put_scan_page

    def statuses(job_id: str) -> dict[str, tuple[str, int]]:
        return {
            state.stage: (state.status, state.revision)
            for state in ScanTailorStageState.objects.filter(scan_job_id=job_id)
        }

    new_stages = ("deskew", "select_content", "dewarping", "margins", "output")
    # Finished jobs keep their results; the raw scan re-runs once.
    for job_id in (complete.pk, cancelled.pk):
        assert statuses(job_id) == {stage: ("success", 2) for stage in new_stages}
    # Migration 0053 (#205) turns the invalidated rows into "not run".
    assert statuses(captured.pk) == {stage: ("unknown", 3) for stage in new_stages}
    job = ScanJob.objects.get(pk=complete.pk)
    assert job.state == ScanJob.State.COMPLETE
    assert job.book_id == book.pk
    page = ScanPage.objects.get(pk=pages[complete.pk])
    # The published result is still what a re-finish would use for this page.
    output = scan_pipeline._active_output_state(page)
    assert output is not None
    assert output.output_storage_identifier == f"{complete.pk}/output/out.png"
    deskew = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    assert deskew.input_stage_state_id is None
    assert deskew.input_scan_page_version_id is not None

    member = Client()
    member.force_login(get_or_create_profile("migration-0049-member").user)
    # The workspace shows the finished results, not a wall of invalidated stages.
    shown = member.get(f"/scan/jobs/{complete.pk}/pages/{page.pk}/stages/")
    assert shown.status_code == 200, shown.content
    assert {item["stage"]: item["status"] for item in shown.json()["stages"]} == {
        stage: "success" for stage in new_stages
    }
    # Re-running a stage of a complete job is refused, as before the migration...
    refused = member.post(f"/scan/jobs/{complete.pk}/pages/{page.pk}/stages/deskew/")
    assert refused.status_code == 400

    # ...and the documented recovery path still works: a rescan and replacement page
    # reopen the job for a re-finish, the stage re-runs, and the re-finish keeps the book.
    queue = _RecordingQueue()
    monkeypatch.setattr(scantailor, "get_queue", lambda: queue)
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    rescan = member.post(f"/scan/pages/{page.pk}/rescan/", data={"reason": "blurry"})
    assert rescan.status_code == 201, rescan.content
    replacement = _put_scan_page(member, page.pk, _png_for(b"after-0049"))
    assert replacement.status_code == 201, replacement.content
    job.refresh_from_db()
    assert job.state == ScanJob.State.CAPTURED
    assert job.refinish_needed is True
    assert ScanPageVersion.objects.filter(scan_page=page).count() == 2

    rerun = member.post(f"/scan/jobs/{complete.pk}/pages/{page.pk}/stages/deskew/")
    assert rerun.status_code == 202, rerun.content
    assert len(queue.enqueued) == 1

    refinished = member.post(f"/scan/jobs/{complete.pk}/finish/", data={"allow_raw_pages": "true"})
    assert refinished.status_code == 202, refinished.content
    job.refresh_from_db()
    assert job.state == ScanJob.State.COMPLETE
    assert job.refinish_needed is False
    assert job.book_id == book.pk
    assert Book.objects.filter(pk=book.pk).exists()
