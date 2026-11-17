"""Run the ScanJob string-primary-key migration tests (#126) against PostgreSQL.

On PostgreSQL the ``AlterField`` casts ``uuid::varchar`` and rewrites every table and
index that references ``core_scanjob.id``; SQLite instead rebuilds the tables and needs
the hex-to-canonical rewrite. Both paths have to keep jobs, pages, todos, ScanTailor
stage states and JSON references resolvable, so the SQLite tests are re-collected here,
plus a timing check that reports how long the rewrite takes for a seeded data set.
"""

from __future__ import annotations

import os
import time
import uuid

import pytest
from django.db import connection

from tests.test_scanjob_string_pk_migration import *  # noqa: F403
from tests.test_scanjob_string_pk_migration import AFTER, BEFORE, _migrate, _migrate_to_latest

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]

# Small enough for CI; set BOOK_SCANNER_PK_MIGRATION_JOBS/_PAGES for a sizing run.
JOBS = int(os.environ.get("BOOK_SCANNER_PK_MIGRATION_JOBS", "200"))
PAGES_PER_JOB = int(os.environ.get("BOOK_SCANNER_PK_MIGRATION_PAGES", "50"))


@pytest.mark.django_db(transaction=True)
def test_string_pk_migration_types_and_timing_on_postgresql(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert connection.vendor == "postgresql"
    try:
        old_apps = _migrate(BEFORE)
        job_model = old_apps.get_model("core", "ScanJob")
        page_model = old_apps.get_model("core", "ScanPage")
        stage_model = old_apps.get_model("core", "ScanTailorStageState")
        jobs = job_model.objects.bulk_create(
            [job_model(id=uuid.uuid4()) for _ in range(JOBS)], batch_size=1000
        )
        pages = page_model.objects.bulk_create(
            [
                page_model(id=uuid.uuid4(), scan_job=job, logical_sequence_number=number)
                for job in jobs
                for number in range(PAGES_PER_JOB)
            ],
            batch_size=5000,
        )
        stage_model.objects.bulk_create(
            [
                stage_model(scan_job_id=page.scan_job_id, scan_page=page, stage="deskew")
                for page in pages
            ],
            batch_size=5000,
        )
        with connection.cursor() as cursor:
            cursor.execute("ANALYZE")

        started = time.perf_counter()
        new_apps = _migrate(AFTER)
        elapsed = time.perf_counter() - started

        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT table_name, data_type FROM information_schema.columns "
                "WHERE (table_name = 'core_scanjob' AND column_name = 'id') "
                "OR column_name = 'scan_job_id' ORDER BY table_name"
            )
            column_types = dict(cursor.fetchall())
            cursor.execute(
                "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
                "AND confrelid = 'core_scanjob'::regclass"
            )
            (foreign_keys,) = cursor.fetchone()
        assert set(column_types.values()) == {"character varying"}, column_types
        assert len(column_types) == 10  # core_scanjob.id plus nine referencing tables
        assert foreign_keys == 9

        new_job = new_apps.get_model("core", "ScanJob")
        sample = str(jobs[len(jobs) // 2].id)
        assert new_job.objects.get(pk=sample).pages.count() == PAGES_PER_JOB
        assert new_job.objects.get(pk=sample).stage_states.count() == PAGES_PER_JOB
        with capsys.disabled():
            print(
                f"\n[0041 on PostgreSQL] {JOBS} jobs, {JOBS * PAGES_PER_JOB} pages, "
                f"{JOBS * PAGES_PER_JOB} stage states: {elapsed:.2f}s"
            )
    finally:
        _migrate_to_latest()
