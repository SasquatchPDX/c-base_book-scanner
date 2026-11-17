"""How much is still left to import, per intake channel (#7).

Shared by ``ingest_sources --status`` and the admin intake-progress page. Database
aggregates only: no filesystem walk and no hashing, so it is cheap enough for a web
request. "Pending" counts files already discovered and queued; files a source has not
been scanned for yet are not known until the next ``ingest_sources`` run.
"""

from __future__ import annotations

from typing import Any

from django.db.models import Count, Q, QuerySet, Sum
from django.db.models.functions import Coalesce

from . import intake
from .models import ImportJob, ScanJob, SourceLocation


def _job_counts(jobs: QuerySet[ImportJob]) -> dict[str, int]:
    totals = jobs.aggregate(
        pending_files=Count("id", filter=Q(state__in=intake.PENDING_ARCHIVE_STATES)),
        pending_bytes=Sum(
            Coalesce("source_size", "size_bytes"),
            filter=Q(state__in=intake.PENDING_ARCHIVE_STATES),
        ),
        needs_review=Count("id", filter=Q(state=ImportJob.State.LOW_CONFIDENCE)),
        complete=Count("id", filter=Q(state=ImportJob.State.COMPLETE)),
        failed=Count("id", filter=Q(state=ImportJob.State.FAILED)),
    )
    counts = {key: int(value or 0) for key, value in totals.items()}
    known = counts["pending_files"] + counts["needs_review"] + counts["complete"] + counts["failed"]
    counts["percent_done"] = round(100 * counts["complete"] / known) if known else 100
    return counts


def source_status(location: SourceLocation) -> dict[str, Any]:
    """Progress of one registered source (incoming folder or network path).

    ``pending_bytes`` is what is still to copy for COPY/MOVE sources and still to hash
    for index-in-place sources.
    """
    return {
        "source_location_id": location.id,
        "name": location.name,
        "transfer_mode": location.transfer_mode,
        "is_active": location.is_active,
        **_job_counts(ImportJob.objects.filter(source_location=location)),
    }


def upload_status() -> dict[str, Any]:
    """Progress of web uploads (they have no source location)."""
    return _job_counts(
        ImportJob.objects.filter(
            source_location__isnull=True,
            source_type=ImportJob.SourceType.UPLOAD,
        ),
    )


def scan_status() -> dict[str, int]:
    """Scan jobs by state that are still in progress."""
    active = (
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
        ScanJob.State.FINALIZING,
    )
    rows = (
        ScanJob.objects.filter(state__in=active).values("state").annotate(n=Count("id")).order_by()
    )
    counts = {str(state): 0 for state in active}
    counts.update({row["state"]: int(row["n"]) for row in rows})
    return counts


def intake_progress(*, active_only: bool = False) -> dict[str, Any]:
    """All intake channels: registered sources, web uploads and the scanner."""
    sources = SourceLocation.objects.order_by("name")
    if active_only:
        sources = sources.filter(is_active=True)
    return {
        "sources": [source_status(location) for location in sources],
        "uploads": upload_status(),
        "scan_jobs": scan_status(),
    }
