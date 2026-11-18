"""Migration 0049 (#195): Page Split rows go, the remaining stages are relinked."""

from __future__ import annotations

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
