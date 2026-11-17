"""Run the contribution recognition suite (#27) against PostgreSQL.

SQLite ignores ``SELECT ... FOR UPDATE``, so row-lock mistakes (such as locking the
nullable side of an outer join during settlement) only show up on the production
backend. Every test of ``tests/test_contributions.py`` is re-collected here and runs in
the infrastructure lane against the pinned PostgreSQL service.
"""

from __future__ import annotations

import os

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


@pytest.mark.django_db
def test_suite_runs_on_postgresql() -> None:
    assert connection.vendor == "postgresql"
