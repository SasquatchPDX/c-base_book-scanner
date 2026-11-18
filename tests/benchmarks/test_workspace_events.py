"""Opt-in 300-page PostgreSQL workspace-poll benchmark; all data is in pytest's DB.

BOOK_SCANNER_BENCHMARK_WORKSPACE_EVENTS=true pytest -s tests/benchmarks/test_workspace_events.py
"""

from __future__ import annotations

import json
import os
import statistics
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from django.db import connection
from django.db.models import F
from django.utils import timezone

from ebook_server.core import scantailor, views
from ebook_server.core.models import ScanJob, ScanPage, ScanTailorStageState

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.getenv("BOOK_SCANNER_BENCHMARK_WORKSPACE_EVENTS") != "true",
        reason="opt-in workspace event benchmark",
    ),
]


def legacy_snapshot(job_id: str) -> dict[str, Any]:
    """The complete read-and-serialize path before #168, including its stage order."""
    job = ScanJob.objects.only("id", "state", "updated_at").get(pk=job_id)
    states = ScanTailorStageState.objects.filter(scan_job_id=job_id).order_by(
        "scan_page__logical_sequence_number", "scan_page__side", "scan_page_id", "stage"
    )
    return {
        "job_id": str(job.pk),
        "state": job.state,
        "updated_at": job.updated_at.isoformat(),
        "stages": [views._serialize_stage_state(state) for state in states],
    }


@contextmanager
def query_cost() -> Iterator[dict[str, float]]:
    totals = {"db_ms": 0.0, "rows_returned": 0.0, "queries": 0.0}

    def measured(execute: Any, sql: str, params: Any, many: bool, context: Any) -> Any:
        started = time.perf_counter()
        result = execute(sql, params, many, context)
        totals["db_ms"] += (time.perf_counter() - started) * 1000
        totals["queries"] += 1
        totals["rows_returned"] += max(context["cursor"].rowcount, 0)
        return result

    with connection.execute_wrapper(measured):
        yield totals


def test_workspace_poll_costs() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("row counts and measurements require PostgreSQL")
    job = ScanJob.objects.create(provisional_title="Workspace benchmark")
    pages = ScanPage.objects.bulk_create(
        [ScanPage(scan_job=job, logical_sequence_number=index + 1) for index in range(300)]
    )
    states = ScanTailorStageState.objects.bulk_create(
        [
            ScanTailorStageState(
                scan_job=job,
                scan_page=page,
                stage=stage,
                parameters={"content_box": [0, 0, 2400, 3200], "provenance": "x" * 512},
            )
            for page in pages
            for stage in scantailor.stage_order()
        ]
    )
    modes: dict[str, Callable[[], Callable[[], dict[str, Any] | None]]] = {
        "before": lambda: lambda: legacy_snapshot(str(job.pk)),
    }
    # Also usable before implementing the optimization, to record the baseline first.
    try:
        from ebook_server.core.workspace_events import WorkspaceEventPoller
    except ImportError:
        pass
    else:

        def optimized() -> Callable[[], dict[str, Any] | None]:
            poller = WorkspaceEventPoller(str(job.pk), views._serialize_stage_state)
            return lambda: poller.poll().payload

        modes["after"] = optimized

    for mode, factory in modes.items():
        if os.getenv("BOOK_SCANNER_BENCHMARK_WORKSPACE_EVENTS_MODE", "all") not in {"all", mode}:
            continue
        for viewers in (1, 5):
            for active in (False, True):
                polls = [factory() for _ in range(viewers)]
                for poll in polls:
                    assert len((poll() or {})["stages"]) == 1800  # initial state
                elapsed: list[float] = []
                db: list[float] = []
                cpu: list[float] = []
                rows: list[float] = []
                serialized = 0
                for _ in range(20):
                    if active:
                        ScanTailorStageState.objects.filter(pk=states[0].pk).update(
                            revision=F("revision") + 1, updated_at=timezone.now()
                        )
                    cpu_start = time.process_time()
                    start = time.perf_counter()
                    with query_cost() as cost:
                        for poll in polls:
                            payload = poll()
                            if payload is not None:
                                serialized += len(payload["stages"])
                                json.dumps(payload, separators=(",", ":"), sort_keys=True)
                    elapsed.append((time.perf_counter() - start) * 1000)
                    cpu.append((time.process_time() - cpu_start) * 1000)
                    db.append(cost["db_ms"])
                    rows.append(cost["rows_returned"])
                print(
                    json.dumps(
                        {
                            "mode": mode,
                            "viewers": viewers,
                            "active": active,
                            "wall_ms_per_tick": round(statistics.median(elapsed), 3),
                            # Windows process_time advances in coarse ticks: use the mean
                            # across the run rather than a misleading median of zero.
                            "cpu_ms_per_tick": round(statistics.mean(cpu), 3),
                            "db_execute_ms_per_tick": round(statistics.median(db), 3),
                            "rows_returned_per_tick": statistics.median(rows),
                            "stages_serialized_per_tick": serialized / 20,
                            "max_poll_completion_ms": round(max(elapsed), 3),
                        }
                    ),
                    flush=True,
                )
