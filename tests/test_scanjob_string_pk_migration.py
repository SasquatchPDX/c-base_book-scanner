"""Migration ``core.0037_scanjob_string_pk``: UUID -> string ScanJob primary key (#126).

Runs on SQLite here and, re-collected by
``tests/integration/test_scanjob_string_pk_migration_postgres.py``, on PostgreSQL.
"""

from __future__ import annotations

import importlib
import uuid
from typing import Any

import pytest
from django.apps import apps as global_apps
from django.db import connection
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

BEFORE = [("core", "0036_book_search_document")]
AFTER = [("core", "0037_scanjob_string_pk")]
MIGRATION = importlib.import_module("ebook_server.core.migrations.0037_scanjob_string_pk")

# (model, FK field) pairs that reference ScanJob; the issue names the first three plus
# the review/processing/search tables.
EXPECTED_REFERENCES = {
    "ScanPage",
    "ScanTodo",
    "ScanTailorStageState",
    "MetadataSearch",
    "MetadataCandidate",
    "ReviewTask",
    "LLMSuggestion",
    "ContributionEvent",
}
# Foreign keys to ScanJob created by later migrations: they are string keys from the
# start, so 0038 never converts them, but its discovery (run here on the current models)
# lists them too.
ADDED_AFTER_0038 = {
    "ScanEvent",  # 0039_scan_events (#129)
}


def _migrate(target: list[tuple[str, str]]) -> Any:
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    return executor.loader.project_state(target).apps


def _migrate_to_latest() -> None:
    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


def _seed(apps: Any) -> dict[str, Any]:
    """Create one of everything that references a scan job, in the 0036 (UUID) schema."""
    now = timezone.now()
    user = apps.get_model("auth", "User").objects.create(username="pk-migration-operator")
    book = apps.get_model("core", "Book").objects.create(title="Migrated Book")
    location = apps.get_model("core", "StorageLocation").objects.create(
        identifier="pk-migration-scans", base_path="scan-artifacts"
    )
    scan_job_model = apps.get_model("core", "ScanJob")
    job = scan_job_model.objects.create(operator=user, book=book, notes={"hint": "kept"})
    other_job = scan_job_model.objects.create()
    page = apps.get_model("core", "ScanPage").objects.create(
        scan_job=job, logical_sequence_number=1
    )
    other_page = apps.get_model("core", "ScanPage").objects.create(
        scan_job=other_job, logical_sequence_number=1
    )
    apps.get_model("core", "ScanPageVersion").objects.create(
        scan_page=page,
        storage_location=location,
        storage_identifier=f"scan-jobs/{job.id}/0001.jpg",
        checksum_sha256="a" * 64,
        mime_type="image/jpeg",
        size_bytes=10,
    )
    apps.get_model("core", "ScanTodo").objects.create(scan_job=job, scan_page=page)
    apps.get_model("core", "ScanTailorStageState").objects.create(
        scan_job=job, scan_page=page, stage="deskew"
    )
    apps.get_model("core", "ScanTailorStageState").objects.create(
        scan_job=other_job, scan_page=other_page, stage="deskew"
    )
    search = apps.get_model("core", "MetadataSearch").objects.create(
        scan_job=job, trigger="scan", state="complete"
    )
    apps.get_model("core", "MetadataCandidate").objects.create(
        scan_job=job, search=search, provider_id="openlibrary", title="Migrated Book"
    )
    apps.get_model("core", "ReviewTask").objects.create(scan_job=job, source_filename="scan")
    processing_model = apps.get_model("core", "ProcessingJob")
    # Non-canonical spellings (hex, uppercase) must come out canonical; nested too.
    non_canonical = processing_model.objects.create(
        job_type="scan_identification",
        details={
            "scan_job_id": job.id.hex.upper(),
            "history": [{"scan_job_id": "{" + str(job.id).upper() + "}"}],
            "unrelated": job.id.hex,
        },
    )
    canonical = processing_model.objects.create(
        job_type="ocr", details={"scan_job_id": str(other_job.id), "book_id": book.pk}
    )
    untouched = processing_model.objects.create(job_type="conversion", details={"x": 1})
    apps.get_model("core", "LLMSuggestion").objects.create(
        processing_job=canonical, scan_job=job, trigger="reviewer", outcome="hints_used"
    )
    apps.get_model("core", "ContributionEvent").objects.create(
        user=user,
        kind="scan_completed",
        outcome_key=f"scan:{job.id}:{now.isoformat()}",
        occurred_at=now,
        settle_after=now,
        scan_job=job,
    )
    variant = apps.get_model("core", "FileVariant").objects.create(
        book=book,
        format="pdf",
        mime_type="application/pdf",
        original_filename="scan.pdf",
        storage_location=location,
        storage_identifier="books/scan.pdf",
        size_bytes=1,
        sha256="b" * 64,
        source_type="upload",
        converter_options={"scan_job_id": str(job.id).upper(), "lineage": []},
    )
    return {
        "job": job.id,
        "other_job": other_job.id,
        "page": page.id,
        "non_canonical": non_canonical.pk,
        "canonical": canonical.pk,
        "untouched": untouched.pk,
        "variant": variant.pk,
    }


def _assert_references(apps: Any, job_id: Any, expected_pk: Any) -> None:
    scan_job_model = apps.get_model("core", "ScanJob")
    job = scan_job_model.objects.get(pk=job_id)
    assert job.pk == expected_pk
    assert job.pages.count() == 1
    assert job.todos.count() == 1
    assert job.stage_states.count() == 1
    assert job.metadata_searches.count() == 1
    assert job.metadata_candidates.count() == 1
    assert job.review_tasks.count() == 1
    assert job.llm_suggestions.count() == 1
    assert job.contribution_events.count() == 1
    page = apps.get_model("core", "ScanPage").objects.get(scan_job_id=job_id)
    assert page.scan_job.pk == expected_pk
    assert page.versions.get().storage_identifier.startswith("scan-jobs/")


def test_migration_discovers_every_scan_job_foreign_key() -> None:
    discovered = {table for table, _column in MIGRATION.scan_job_fk_columns(global_apps)}
    expected = {
        global_apps.get_model("core", name)._meta.db_table
        for name in EXPECTED_REFERENCES | ADDED_AFTER_0038
    }
    assert discovered == expected
    current_fks = {
        model.__name__
        for model in global_apps.get_models()
        for field in model._meta.local_concrete_fields
        if field.many_to_one and field.related_model is global_apps.get_model("core", "ScanJob")
    }
    assert current_fks == EXPECTED_REFERENCES | ADDED_AFTER_0038


@pytest.mark.django_db(transaction=True)
def test_string_pk_migration_forward_and_backward() -> None:
    try:
        old_apps = _migrate(BEFORE)
        ids = _seed(old_apps)
        job_uuid: uuid.UUID = ids["job"]
        job_key = str(job_uuid)

        new_apps = _migrate(AFTER)
        scan_job_model = new_apps.get_model("core", "ScanJob")
        stored_ids = set(scan_job_model.objects.values_list("pk", flat=True))
        assert stored_ids == {job_key, str(ids["other_job"])}
        _assert_references(new_apps, job_key, job_key)
        # Every referencing column holds the same canonical string.
        for name in EXPECTED_REFERENCES:
            model = new_apps.get_model("core", name)
            values = set(model.objects.values_list("scan_job_id", flat=True))
            assert values <= stored_ids, name
        assert scan_job_model.objects.get(pk=job_key).notes == {"hint": "kept"}
        # New fields exist and are empty for migrated jobs.
        migrated = scan_job_model.objects.get(pk=job_key)
        assert migrated.source == "web"
        assert migrated.scan_started_at is None

        processing = new_apps.get_model("core", "ProcessingJob")
        details = processing.objects.get(pk=ids["non_canonical"]).details
        assert details["scan_job_id"] == job_key
        assert details["history"] == [{"scan_job_id": job_key}]
        assert details["unrelated"] == job_uuid.hex  # only scan_job_id keys are touched
        assert processing.objects.get(pk=ids["canonical"]).details == {
            "scan_job_id": str(ids["other_job"]),
            "book_id": processing.objects.get(pk=ids["canonical"]).details["book_id"],
        }
        assert processing.objects.get(pk=ids["untouched"]).details == {"x": 1}
        variant = new_apps.get_model("core", "FileVariant").objects.get(pk=ids["variant"])
        assert variant.converter_options == {"scan_job_id": job_key, "lineage": []}
        # JSON references resolve to their job.
        assert scan_job_model.objects.filter(pk=details["scan_job_id"]).exists()

        # A scanner-assigned ID blocks the reversal instead of losing the job.
        scan_job_model.objects.create(id="20261117T143201Z")
        with pytest.raises(IrreversibleError, match="20261117T143201Z"):
            _migrate(BEFORE)
        new_apps = _migrate(AFTER)
        new_apps.get_model("core", "ScanJob").objects.filter(pk="20261117T143201Z").delete()

        reverted_apps = _migrate(BEFORE)
        _assert_references(reverted_apps, job_uuid, job_uuid)
        reverted_page = reverted_apps.get_model("core", "ScanPage").objects.get(pk=ids["page"])
        assert reverted_page.scan_job_id == job_uuid
        assert set(
            reverted_apps.get_model("core", "ScanJob").objects.values_list("pk", flat=True)
        ) == {job_uuid, ids["other_job"]}

        # And forward again (a re-run after a rollback).
        again = _migrate(AFTER)
        _assert_references(again, job_key, job_key)
    finally:
        _migrate_to_latest()
