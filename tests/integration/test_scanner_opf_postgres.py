"""Run the scanner ``metadata.opf`` suite (#130) against PostgreSQL.

SQLite ignores ``SELECT ... FOR UPDATE``, so the row locks that fence a delayed provider
check to the effective OPF version and protect claimed review evidence (scan job, Book,
review task, in that order) only take effect on the production backend. Every test of
``tests/test_scanner_opf.py`` - including the out-of-order worker and reviewer
sequences - is re-collected here and runs in the infrastructure lane against the pinned
PostgreSQL service.
"""

from __future__ import annotations

import os

import pytest
from django.db import connection

from tests.test_scanner_opf import *  # noqa: F403

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
