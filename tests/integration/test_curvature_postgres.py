"""Dewarping worker version and replay on PostgreSQL (#171, #195 step 6)."""

import os
from pathlib import Path
from typing import Any

import pytest
from django.db import connection

from tests.test_cylindrical import (
    test_manual_model_replays_through_the_worker_and_auto_returns,  # noqa: F401
)

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires PostgreSQL infrastructure",
    ),
]


@pytest.fixture(autouse=True)
def guarded_workflow_environment(settings: Any, tmp_path: Path) -> None:
    """Run the replay workflow inline on guarded storage, even with CI Redis set."""
    settings.BOOK_SCANNER_QUEUE_URL = "inline://curvature-postgres"
    settings.APP_DATA_DIR = tmp_path
    settings.STORAGE_REQUIRE_EXISTING_ROOT = True
    settings.STORAGE_SENTINEL_FILE = ".curvature-storage-ready"
    (tmp_path / settings.STORAGE_SENTINEL_FILE).write_text("mounted", encoding="utf-8")
    settings.BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS = 0


def test_postgresql_engine() -> None:
    assert connection.vendor == "postgresql"
