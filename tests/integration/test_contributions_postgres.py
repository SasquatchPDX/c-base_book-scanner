"""Run the contribution recognition suite (#27) against PostgreSQL.

SQLite ignores ``SELECT ... FOR UPDATE``, so row-lock mistakes (such as locking the
nullable side of an outer join during settlement) only show up on the production
backend. Every test of ``tests/test_contributions.py`` is re-collected here and runs in
the infrastructure lane against the pinned PostgreSQL service.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from django.db import connection

from tests.test_contributions import *  # noqa: F403

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]


@pytest.fixture(autouse=True)
def contribution_workflow_environment(
    settings: Any, tmp_path: Path, request: pytest.FixtureRequest
) -> None:
    """Keep the ledger workflows synchronous on a real, guarded test storage root.

    These recollected workflow tests assert completed work in their own database
    transaction. The separate infrastructure suite exercises actual RQ delivery;
    an ambient Redis URL must not turn these worker calls into undrained jobs.
    """
    if request.node.name in {
        "test_multipart_confirmation_credits_one_review_not_each_part",
        "test_automatic_reviewer_isbn_worker_does_not_award_points",
    }:
        settings.BOOK_SCANNER_QUEUE_URL = "inline://contributions-postgres"
    settings.APP_DATA_DIR = tmp_path
    settings.STORAGE_REQUIRE_EXISTING_ROOT = True
    settings.STORAGE_SENTINEL_FILE = ".contributions-storage-ready"
    (tmp_path / settings.STORAGE_SENTINEL_FILE).write_text("mounted", encoding="utf-8")
    settings.BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS = 0


@pytest.mark.django_db
def test_suite_runs_on_postgresql() -> None:
    from django.conf import settings

    assert connection.vendor == "postgresql"
    assert settings.STORAGE_REQUIRE_EXISTING_ROOT
    assert (settings.APP_DATA_DIR / settings.STORAGE_SENTINEL_FILE).is_file()
