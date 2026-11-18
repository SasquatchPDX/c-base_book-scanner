"""Connection-local ScanTailor event cursor backed by persistent stage state.

On PostgreSQL an idle tick returns one database-computed fingerprint row, never the
large parameter JSON or artifact fields. Other backends read the narrow token vector.
A changed tick serializes only changed stages. Tokens are compared per row, not with
a max timestamp: a transaction with an older timestamp that commits late must still
be delivered. No shared cache or writer-side locks are needed. Every connection
starts with a complete snapshot. Stage mutations must update updated_at (as normal
model saves, worker status writes and invalidations do).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from django.db import connection
from django.db.models.expressions import RawSQL

from .models import ScanJob, ScanTailorStageState

StageToken = tuple[int, str, datetime]


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
        self._fingerprint: str | None = None

    def poll(self) -> WorkspaceUpdate:
        jobs = ScanJob.objects.filter(pk=self.job_id).only("id", "state", "updated_at")
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
        fingerprint = getattr(job, "_workspace_fingerprint", None)
        unchanged_stages = (
            postgres and self._tokens is not None and fingerprint == self._fingerprint
        )
        if unchanged_stages and job_token == self._job_token:
            return WorkspaceUpdate(True, None)
        query = ScanTailorStageState.objects.filter(scan_job_id=self.job_id)
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
            if job_token == self._job_token and tokens == self._tokens:
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
        self._fingerprint = fingerprint
        return WorkspaceUpdate(
            True,
            {
                "job_id": str(job.pk),
                "state": job.state,
                "updated_at": job.updated_at.isoformat(),
                "stages": [self.serialize_stage(state) for state in states],
            },
        )
