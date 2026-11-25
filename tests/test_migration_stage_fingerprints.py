"""Migration 0053 (#205): results get input fingerprints; "invalidated" goes away."""

from __future__ import annotations

import pytest

BEFORE = [("core", "0052_deskew_angle_clockwise")]
AFTER = [("core", "0053_stage_fingerprints_issue205")]
STAGES = ("deskew", "select_content", "dewarping", "margins", "output")


@pytest.mark.django_db(transaction=True)
def test_successful_results_stay_current_and_invalidated_rows_become_not_run() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    storage = old_apps.get_model("core", "StorageLocation").objects.create(
        identifier="scan-migration-0053", backend="local", base_path="scan"
    )
    job = old_apps.get_model("core", "ScanJob").objects.create(id="20261124T120000Z")
    page = old_apps.get_model("core", "ScanPage").objects.create(
        scan_job=job, logical_sequence_number=1
    )
    version = old_apps.get_model("core", "ScanPageVersion").objects.create(
        scan_page=page,
        storage_location=storage,
        storage_identifier="raw/v1.png",
        checksum_sha256="c" * 64,
        mime_type="image/png",
        size_bytes=1,
        version=1,
        is_active=True,
    )
    stage_model = old_apps.get_model("core", "ScanTailorStageState")
    previous = None
    for index, stage in enumerate(STAGES):
        # Deskew to Dewarping succeeded; Margins and Output were invalidated.
        status = "success" if index < 3 else "invalidated"
        previous = stage_model.objects.create(
            scan_job=job,
            scan_page=page,
            stage=stage,
            status=status,
            revision=2,
            parameters={"setting": stage},
            input_stage_state=previous,
            input_scan_page_version=version if previous is None else None,
            output_storage_identifier=f"{stage}/out.png",
            output_sha256=f"{index}" * 64,
            invalidated_reason="" if status == "success" else "deskew updated",
        )

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new_apps = executor.loader.project_state(AFTER).apps
    rows = {
        row.stage: row
        for row in new_apps.get_model("core", "ScanTailorStageState").objects.filter(
            scan_page_id=page.pk
        )
    }
    assert rows["deskew"].applied_input_sha256 == "c" * 64
    assert rows["select_content"].applied_input_sha256 == "0" * 64
    assert rows["dewarping"].applied_input_sha256 == "1" * 64
    for stage in ("deskew", "select_content", "dewarping"):
        assert rows[stage].status == "success"
        assert rows[stage].applied_revision == 2
        assert rows[stage].applied_parameters == {"setting": stage}
    for stage in ("margins", "output"):
        assert rows[stage].status == "unknown"
        assert rows[stage].applied_revision is None
        # Old artifacts are kept; they are just not a current result.
        assert rows[stage].output_storage_identifier == f"{stage}/out.png"

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())

    from ebook_server.core import scantailor
    from ebook_server.core.models import ScanPage, ScanTailorStageState

    live_page = ScanPage.objects.get(pk=page.pk)
    states = {
        state.stage: state for state in ScanTailorStageState.objects.filter(scan_page=live_page)
    }
    scantailor.link_page_states(states, live_page.active_version())
    for stage in ("deskew", "select_content", "dewarping"):
        assert scantailor.stage_result_is_current(states[stage]), stage
    assert scantailor.stage_waiting_for(states["output"]) == "margins"
