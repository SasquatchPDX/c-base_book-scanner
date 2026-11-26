"""Connection-local ScanTailor event cursor backed by persistent stage state.

On PostgreSQL an idle tick returns one database-computed fingerprint row, never the
large parameter JSON or artifact fields. Other backends read the narrow token vector.
A changed tick serializes only changed stages. Tokens are compared per row, not with
a max timestamp: a transaction with an older timestamp that commits late must still
be delivered. No shared cache or writer-side locks are needed. Every connection
starts with a complete snapshot. Stage mutations must update updated_at (as normal
model saves, worker status writes and invalidations do).

Page markers (#228) travel with the stages: whenever the job's rescan todos change
(a member requests a rescan, the scanner claims it or a replacement completes it), the
payload carries ``pages`` with every page's open rescan count and the scanner's page
number check of its active version. The todo token is part of the job row query, so an
idle tick still costs the same queries.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection
from django.db.models import Count, Max, OuterRef, Q, Subquery
from django.db.models.expressions import RawSQL

from . import scantailor
from .models import ScanJob, ScanPage, ScanPageVersion, ScanTailorStageState, ScanTodo

StageToken = tuple[int, str, datetime]

# Rescan todos still waiting for a replacement (the scanner may have claimed one).
OPEN_RESCAN_STATES = (ScanTodo.State.OPEN, ScanTodo.State.CLAIMED)


def _rescan_todos_of_job() -> Any:
    return ScanTodo.objects.filter(
        scan_job_id=OuterRef("pk"), todo_type=ScanTodo.TodoType.RESCAN
    ).order_by()


def page_markers(job_id: str) -> list[dict[str, Any]]:
    """Every page's open rescan count and page-number check, in one query (#228).

    ``page_number_verified`` is the scanner's check of the active version: ``False``
    only for a rescan replacement whose printed page number could not be confirmed.
    """
    active_verified = (
        ScanPageVersion.objects.filter(scan_page_id=OuterRef("pk"), is_active=True)
        .order_by("-version")
        .values("page_number_verified")[:1]
    )
    pages = (
        ScanPage.objects.filter(scan_job_id=job_id)
        .annotate(
            open_rescans=Count(
                "todos",
                filter=Q(
                    todos__todo_type=ScanTodo.TodoType.RESCAN,
                    todos__state__in=OPEN_RESCAN_STATES,
                ),
            ),
            active_page_number_verified=Subquery(active_verified),
        )
        .order_by("logical_sequence_number", "side", "pk")
        .values("pk", "open_rescans", "active_page_number_verified")
    )
    return [
        {
            "id": str(page["pk"]),
            "open_rescan_todos": page["open_rescans"],
            "page_number_verified": page["active_page_number_verified"],
        }
        for page in pages
    ]


@dataclass(frozen=True)
class WorkspaceUpdate:
    exists: bool
    payload: dict[str, Any] | None


class WorkspaceEventPoller:
    def __init__(
        self,
        job_id: str,
        serialize_stage: Callable[[ScanTailorStageState], dict[str, Any]],
    ) -> None:
        self.job_id = job_id
        self.serialize_stage = serialize_stage
        self._tokens: dict[int, StageToken] | None = None
        self._job_token: tuple[str, datetime] | None = None
        self._todo_token: tuple[Any, ...] | None = None
        self._fingerprint: str | None = None

    def poll(self) -> WorkspaceUpdate:
        rescans = _rescan_todos_of_job()
        jobs = (
            ScanJob.objects.filter(pk=self.job_id)
            .only("id", "state", "refinish_needed", "updated_at")
            .annotate(
                # Count and newest change of the job's rescan todos (#228): a request,
                # a claim and a completed replacement each move one of them.
                _rescan_count=Subquery(
                    rescans.values("scan_job_id").annotate(n=Count("pk")).values("n")[:1]
                ),
                _rescan_open=Subquery(
                    rescans.filter(state__in=OPEN_RESCAN_STATES)
                    .values("scan_job_id")
                    .annotate(n=Count("pk"))
                    .values("n")[:1]
                ),
                _rescan_changed=Subquery(
                    rescans.values("scan_job_id").annotate(m=Max("updated_at")).values("m")[:1]
                ),
            )
        )
        postgres = connection.vendor == "postgresql"
        if postgres:
            # Reduce idle transfer to one row. Hash only cheap metadata, never the
            # parameters JSON. The ordered vector detects deletions and late commits
            # too; a max(updated_at) watermark would miss the latter.
            quote = connection.ops.quote_name
            stage_table = quote(ScanTailorStageState._meta.db_table)
            job_table = quote(ScanJob._meta.db_table)
            jobs = jobs.annotate(
                _workspace_fingerprint=RawSQL(
                    "(SELECT md5(string_agg(json_build_array(s.id, s.revision, s.status, "
                    "s.updated_at)::text, ',' ORDER BY s.id)) "
                    f"FROM {stage_table} s WHERE s.scan_job_id = {job_table}.id)",
                    [],
                )
            )
        job = jobs.first()
        if job is None:
            return WorkspaceUpdate(False, None)
        job_token = (job.state, job.updated_at)
        todo_token = (
            getattr(job, "_rescan_count", None),
            getattr(job, "_rescan_open", None),
            getattr(job, "_rescan_changed", None),
        )
        todos_changed = todo_token != self._todo_token
        fingerprint = getattr(job, "_workspace_fingerprint", None)
        unchanged_stages = (
            postgres and self._tokens is not None and fingerprint == self._fingerprint
        )
        if unchanged_stages and job_token == self._job_token and not todos_changed:
            return WorkspaceUpdate(True, None)
        # The first stage serializes its input version's checksum (#205); an aligned
        # Margins row its predecessor's output and the job's common page size (#208).
        query = ScanTailorStageState.objects.filter(scan_job_id=self.job_id).select_related(
            "input_scan_page_version", "input_stage_state"
        )
        if self._tokens is None:
            states = list(
                query.order_by(
                    "scan_page__logical_sequence_number", "scan_page__side", "scan_page_id", "stage"
                )
            )
            tokens = {
                state.pk: (state.revision, state.status, state.updated_at) for state in states
            }
        elif unchanged_stages:
            tokens = self._tokens
            states = []
        else:
            tokens = {
                pk: (revision, status, updated_at)
                for pk, revision, status, updated_at in query.order_by("pk").values_list(
                    "pk", "revision", "status", "updated_at"
                )
            }
            if job_token == self._job_token and tokens == self._tokens and not todos_changed:
                # A previous payload fetch may have already observed a newer row
                # version than its fingerprint query. Advance the cheap cursor too.
                self._fingerprint = fingerprint
                return WorkspaceUpdate(True, None)
            changed = [pk for pk, token in tokens.items() if self._tokens.get(pk) != token]
            states = list(query.filter(pk__in=changed).order_by("pk")) if changed else []
        # Keep the tokens observed before loading payloads. A concurrent change can
        # cause one harmless duplicate next tick, but cannot be hidden by this fetch.
        self._tokens = tokens
        self._job_token = job_token
        self._todo_token = todo_token
        self._fingerprint = fingerprint
        # The common page size is computed once per tick, not once per Margins row.
        scantailor.attach_margins_aggregate_if_needed(states)
        payload: dict[str, Any] = {
            "job_id": str(job.pk),
            "state": job.state,
            "refinish_needed": job.refinish_needed,
            "updated_at": job.updated_at.isoformat(),
            "stages": [self.serialize_stage(state) for state in states],
        }
        if todos_changed:
            payload["pages"] = page_markers(self.job_id)
        return WorkspaceUpdate(True, payload)
