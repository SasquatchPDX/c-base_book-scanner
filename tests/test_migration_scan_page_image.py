"""Migration 0050 (#195 step 2): only stages of still-processable jobs are invalidated."""

from __future__ import annotations

import pytest

BEFORE = [("core", "0049_remove_page_split_reorder_stages")]
AFTER = [("core", "0050_scan_page_image")]


@pytest.mark.django_db(transaction=True)
def test_stages_of_processable_jobs_are_invalidated_and_terminal_jobs_keep_results() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    job_model = old_apps.get_model("core", "ScanJob")
    page_model = old_apps.get_model("core", "ScanPage")
    stage_model = old_apps.get_model("core", "ScanTailorStageState")

    pages = {}
    for number, job_state in enumerate(
        ("open", "captured", "finalizing", "complete", "cancelled"), start=1
    ):
        job = job_model.objects.create(id=f"20261118T12000{number}Z", state=job_state)
        page = page_model.objects.create(scan_job=job, logical_sequence_number=1)
        stage_model.objects.create(
            scan_job=job,
            scan_page=page,
            stage="deskew",
            status="success",
            revision=2,
            output_storage_identifier=f"{job_state}/deskew.png",
        )
        pages[job_state] = page.pk

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new_apps = executor.loader.project_state(AFTER).apps
    states = new_apps.get_model("core", "ScanTailorStageState").objects

    for job_state in ("open", "captured"):
        state = states.get(scan_page_id=pages[job_state], stage="deskew")
        assert state.status == "invalidated"
        assert state.revision == 3
        assert "#195" in state.invalidated_reason
    # Finalizing, complete and cancelled jobs reject stage re-runs (HTTP 400), so
    # their historical results stay usable instead of becoming permanently stale.
    for job_state in ("finalizing", "complete", "cancelled"):
        state = states.get(scan_page_id=pages[job_state], stage="deskew")
        assert state.status == "success"
        assert state.revision == 2
        assert state.invalidated_reason == ""
        assert state.output_storage_identifier == f"{job_state}/deskew.png"

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
