"""Migration 0056 (#219): the boolean blank declaration becomes ``page_disposition``."""

from __future__ import annotations

import pytest

BEFORE = [("core", "0055_scantailor_run_all")]
AFTER = [("core", "0056_issue219_page_disposition")]


@pytest.mark.django_db(transaction=True)
def test_blank_declarations_and_their_audit_events_become_dispositions() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    job = old_apps.get_model("core", "ScanJob").objects.create(id="20261126T090000Z")
    pages = old_apps.get_model("core", "ScanPage").objects
    blank = pages.create(scan_job=job, logical_sequence_number=1, declared_blank=True)
    normal = pages.create(scan_job=job, logical_sequence_number=2)
    location = old_apps.get_model("core", "StorageLocation").objects.create(
        identifier="migration-0056-scan", base_path="/tmp/scan"
    )
    version = old_apps.get_model("core", "ScanPageVersion").objects.create(
        scan_page=blank,
        storage_location=location,
        storage_identifier="scan/1.png",
        checksum_sha256="0" * 64,
        mime_type="image/png",
        size_bytes=1,
        version=1,
    )
    events = old_apps.get_model("core", "ScanPageBlankEvent").objects
    declared = events.create(page=blank, version=version, previous_value=False, declared_blank=True)
    cleared = events.create(page=blank, version=version, previous_value=True, declared_blank=False)

    executor = MigrationExecutor(connection)
    executor.migrate(AFTER)
    new_apps = executor.loader.project_state(AFTER).apps
    new_pages = new_apps.get_model("core", "ScanPage").objects
    assert new_pages.get(pk=blank.pk).page_disposition == "blank"
    assert new_pages.get(pk=normal.pk).page_disposition == "normal"
    new_events = new_apps.get_model("core", "ScanPageDispositionEvent").objects
    assert (
        new_events.get(pk=declared.pk).previous_disposition,
        new_events.get(pk=declared.pk).disposition,
    ) == ("normal", "blank")
    assert (
        new_events.get(pk=cleared.pk).previous_disposition,
        new_events.get(pk=cleared.pk).disposition,
    ) == ("blank", "normal")

    # Reversible.
    executor = MigrationExecutor(connection)
    executor.migrate(BEFORE)
    old_apps = executor.loader.project_state(BEFORE).apps
    assert old_apps.get_model("core", "ScanPage").objects.get(pk=blank.pk).declared_blank
    reverted = old_apps.get_model("core", "ScanPageBlankEvent").objects.get(pk=cleared.pk)
    assert reverted.previous_value and not reverted.declared_blank

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
