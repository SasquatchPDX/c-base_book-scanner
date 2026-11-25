"""Migration 0052 (#195 step 4): stored deskew angles become clockwise-positive."""

from __future__ import annotations

import pytest

BEFORE = [("core", "0051_stage_state_job_updated_index")]
AFTER = [("core", "0052_deskew_angle_clockwise")]


@pytest.mark.django_db(transaction=True)
def test_deskew_angles_are_negated_and_other_stages_untouched() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    job = old_apps.get_model("core", "ScanJob").objects.create(id="20261119T090000Z")
    pages = old_apps.get_model("core", "ScanPage").objects
    states = old_apps.get_model("core", "ScanTailorStageState").objects
    manual_page = pages.create(scan_job=job, logical_sequence_number=1)
    auto_page = pages.create(scan_job=job, logical_sequence_number=2)
    transform = {"op": "rotate", "angle": 1.5}
    manual = states.create(
        scan_job=job,
        scan_page=manual_page,
        stage="deskew",
        status="success",
        revision=4,
        parameters={
            "manual_angle": 1.5,
            "angle": 1.5,
            "deskew_mode": "manual",
            "transform": transform,
        },
    )
    auto = states.create(
        scan_job=job,
        scan_page=auto_page,
        stage="deskew",
        status="success",
        revision=2,
        parameters={"angle": -2, "angle_min": -5, "angle_max": 5, "angle_step": 0.5},
    )
    content = states.create(
        scan_job=job,
        scan_page=manual_page,
        stage="select_content",
        parameters={"angle": 3.0},
    )

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new_states = (
        executor.loader.project_state(AFTER).apps.get_model("core", "ScanTailorStageState").objects
    )
    migrated = new_states.get(pk=manual.pk)
    # Same rotation, new sign convention; the recorded op is unchanged.
    assert migrated.parameters == {
        "manual_angle": -1.5,
        "angle": -1.5,
        "deskew_mode": "manual",
        "transform": transform,
    }
    assert (migrated.status, migrated.revision) == ("success", 4)
    assert new_states.get(pk=auto.pk).parameters == {"angle": 2.0}
    assert new_states.get(pk=content.pk).parameters == {"angle": 3.0}

    # Reversible: the old convention comes back (the dropped search keys do not).
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_states = (
        executor.loader.project_state(BEFORE).apps.get_model("core", "ScanTailorStageState").objects
    )
    assert old_states.get(pk=manual.pk).parameters["manual_angle"] == 1.5

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
