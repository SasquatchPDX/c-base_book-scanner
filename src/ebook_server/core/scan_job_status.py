"""Derived pipeline status for the scan job list (#227).

``ScanJob.state`` alone cannot tell how far a scan has come: processing never changes
it, and a web-created job never leaves ``capturing``. The list therefore derives one
label, the earliest unfinished step, from data that already exists, and shows the
conditions that can occur at any step as separate badges:

====================  ==============================================================
Label                 Condition (first that applies)
====================  ==============================================================
Cancelled             ``state == cancelled``
OCR queued / running  ``state == complete`` and the newest OCR job is queued / running
OCR'd                 ``state == complete`` and the newest OCR job succeeded
Complete              ``state == complete`` (no OCR, or OCR failed: see the badge)
Finalizing            ``state == finalizing``
Paused                ``state == paused``
Capturing             no pages yet, or a scanner job before ``capture-complete``
N / M rendered        some, not all pages have a successful (or blank-skipped) Output
Rendered              every page has one
Metadata available    nothing rendered yet; a scanner OPF or a linked Book exists
Captured              nothing rendered yet, no metadata
====================  ==============================================================

Badges: *failed* (a failed stage, or the newest finalization or OCR job failed),
*waiting for human* (an open review task), *rescan open*, *re-finish needed*, and the
whole-book ScanTailor run state.

Everything is computed with a fixed number of aggregated queries for the whole list,
never per job and never loading pages (#226): the annotated job query, one aggregate
over stage states, one query for finalization jobs and one for OCR jobs.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from django.db.models import Count, Exists, IntegerField, OuterRef, Q, QuerySet, Subquery, Value
from django.db.models.functions import Coalesce

from . import scan_metadata_completion, scan_run, scanner_rescans
from .models import (
    FileVariant,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    ScanJobOpf,
    ScanPage,
    ScanTailorRun,
    ScanTailorStageState,
    ScanTodo,
)

_DONE_OUTPUT = (
    ScanTailorStageState.StageStatus.SUCCESS,
    ScanTailorStageState.StageStatus.SKIPPED,
)
_CAPTURE_STATES = (ScanJob.State.OPEN, ScanJob.State.CAPTURING, ScanJob.State.PAUSED)


@dataclass
class PipelineStatus:
    key: str
    label: str
    css: str = ""
    rendered: int = 0
    page_count: int = 0
    badges: list[dict[str, str]] = field(default_factory=list)

    @property
    def progress(self) -> str:
        """``"N / M rendered"`` while rendering is under way and the label says less."""
        if self.page_count and self.rendered and self.key not in {"rendering", "rendered"}:
            return f"{self.rendered} / {self.page_count} rendered"
        return ""


def derive(
    *,
    state: str,
    source: str,
    page_count: int,
    rendered: int,
    has_metadata: bool,
    ocr_state: str | None = None,
) -> tuple[str, str, str]:
    """``(key, label, css class)`` of the earliest unfinished step."""
    if state == ScanJob.State.CANCELLED:
        return "cancelled", "Cancelled", ""
    if state == ScanJob.State.COMPLETE:
        if ocr_state == ProcessingJob.JobState.QUEUED:
            return "ocr_queued", "OCR queued", "running"
        if ocr_state == ProcessingJob.JobState.RUNNING:
            return "ocr_running", "OCR running", "running"
        if ocr_state == ProcessingJob.JobState.SUCCESS:
            return "ocr_done", "OCR'd", "complete"
        return "complete", "Complete", "complete"
    if state == ScanJob.State.FINALIZING:
        return "finalizing", "Finalizing", "finalizing"
    if state == ScanJob.State.PAUSED:
        return "paused", "Paused", ""
    if page_count == 0 or (source == ScanJob.Source.SCANNER and state in _CAPTURE_STATES):
        return "capturing", "Capturing", ""
    if rendered >= page_count:
        return "rendered", "Rendered", "complete"
    if rendered > 0:
        return "rendering", f"{rendered} / {page_count} rendered", "running"
    if has_metadata:
        return "metadata", "Metadata available", ""
    return "captured", "Captured", ""


def annotated_jobs(queryset: QuerySet[ScanJob]) -> QuerySet[ScanJob]:
    """The list's per-job columns as subqueries of one query (no GROUP BY over jobs)."""
    scanner_opf = ScanJobOpf.objects.filter(
        scan_job_id=OuterRef("pk"), app_generated=False
    ).order_by("-version")
    page_count = (
        ScanPage.objects.filter(scan_job_id=OuterRef("pk"))
        .order_by()
        .values("scan_job_id")
        .annotate(total=Count("pk"))
        .values("total")
    )
    return queryset.annotate(
        page_count=Coalesce(Subquery(page_count, output_field=IntegerField()), Value(0)),
        scanner_metadata=Subquery(scanner_opf.values("fields")[:1]),
        scanner_metadata_opf_id=Subquery(scanner_opf.values("pk")[:1]),
        active_run=Subquery(
            ScanTailorRun.objects.filter(
                scan_job_id=OuterRef("pk"), state__in=scan_run.ACTIVE
            ).values("state")[:1]
        ),
        has_open_rescans=Exists(
            ScanTodo.objects.filter(
                scan_job_id=OuterRef("pk"),
                state__in=scanner_rescans.OPEN_TODO_STATES,
                todo_type=ScanTodo.TodoType.RESCAN,
            )
        ),
        has_pending_review=Exists(
            ReviewTask.objects.filter(
                scan_job_id=OuterRef("pk"),
                state__in=[ReviewTask.State.OPEN, ReviewTask.State.CLAIMED],
            )
        ),
    )


def _stage_counts(job_ids: list[str]) -> dict[str, dict[str, int]]:
    """Per job: rendered pages (Output done) and failed stage rows, in one query."""
    counts: dict[str, dict[str, int]] = defaultdict(lambda: {"rendered": 0, "failed": 0})
    rows = (
        ScanTailorStageState.objects.filter(scan_job_id__in=job_ids)
        .filter(
            Q(stage=ScanTailorStageState.Stage.OUTPUT, status__in=_DONE_OUTPUT)
            | Q(status=ScanTailorStageState.StageStatus.FAILED)
        )
        .order_by()
        .values("scan_job_id", "stage", "status")
        .annotate(total=Count("pk"))
    )
    for row in rows:
        entry = counts[str(row["scan_job_id"])]
        if row["status"] == ScanTailorStageState.StageStatus.FAILED:
            entry["failed"] += row["total"]
        else:
            entry["rendered"] += row["total"]
    return counts


def _latest_states(rows: Iterable[tuple[Any, str]]) -> dict[str, str]:
    """The first state seen per job id (rows come newest first)."""
    latest: dict[str, str] = {}
    for job_id, state in rows:
        if job_id is not None:
            latest.setdefault(str(job_id), state)
    return latest


def _finalization_states(job_ids: list[str]) -> dict[str, str]:
    rows = (
        ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
            details__scan_job_id__in=job_ids,
        )
        .order_by("-scheduled_at", "-pk")
        .values_list("details__scan_job_id", "state")
    )
    return _latest_states(rows)


def _ocr_states(job_ids: list[str]) -> dict[str, str]:
    """Newest OCR job per scan: on its current cleaned variant, or on the scan itself."""
    rows = (
        ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.OCR)
        .filter(
            Q(details__source_type="scan_job", details__scan_job_id__in=job_ids)
            | Q(
                file_variant__source_type=FileVariant.SourceType.CLEANED,
                file_variant__processing_state=FileVariant.ProcessingState.COMPLETE,
                file_variant__converter_options__scan_job_id__in=job_ids,
            )
        )
        .order_by("-scheduled_at", "-pk")
        .values_list(
            "details__scan_job_id", "file_variant__converter_options__scan_job_id", "state"
        )
    )
    return _latest_states((direct or via_variant, state) for direct, via_variant, state in rows)


def _display_metadata(job: ScanJob) -> dict[str, Any] | None:
    fields = job.scanner_metadata  # type: ignore[attr-defined]
    if not isinstance(fields, dict):
        return None
    completed = {
        name: value
        for name, value in scan_metadata_completion.completion_for(
            job.notes,
            job.scanner_metadata_opf_id,  # type: ignore[attr-defined]
        ).items()
        if not fields.get(name)
    }
    merged = {**fields, **completed}
    return {
        "title": merged.get("title"),
        "authors": list(merged.get("authors") or []),
        "isbn": merged.get("isbn"),
        "language": merged.get("language"),
        "completed": sorted(completed),
    }


def list_jobs(queryset: QuerySet[ScanJob]) -> list[ScanJob]:
    """Evaluate the list and attach ``pipeline`` and ``scanner_metadata`` to each job."""
    jobs = list(annotated_jobs(queryset))
    job_ids = [str(job.pk) for job in jobs]
    if not job_ids:
        return jobs
    stage_counts = _stage_counts(job_ids)
    finalization = _finalization_states(job_ids)
    ocr = _ocr_states(job_ids)
    for job in jobs:
        job_id = str(job.pk)
        counts = stage_counts.get(job_id, {"rendered": 0, "failed": 0})
        page_count = int(job.page_count or 0)  # type: ignore[attr-defined]
        rendered = min(counts["rendered"], page_count)
        metadata = _display_metadata(job)
        job.scanner_metadata = metadata  # type: ignore[attr-defined]
        key, label, css = derive(
            state=job.state,
            source=job.source,
            page_count=page_count,
            rendered=rendered,
            has_metadata=metadata is not None or job.book_id is not None,
            ocr_state=ocr.get(job_id),
        )
        failures: Counter[str] = Counter()
        if counts["failed"]:
            failures["stage"] = counts["failed"]
        if finalization.get(job_id) == ProcessingJob.JobState.FAILED:
            failures["finalization"] = 1
        if ocr.get(job_id) == ProcessingJob.JobState.FAILED:
            failures["ocr"] = 1
        badges: list[dict[str, str]] = []
        if failures:
            parts = []
            if failures["stage"]:
                parts.append(f"{failures['stage']} stage{'s' if failures['stage'] != 1 else ''}")
            parts.extend(name for name in ("finalization", "ocr") if failures[name])
            badges.append(
                {"key": "failed", "label": "Failed: " + ", ".join(parts), "css": "failed"}
            )
        if job.has_pending_review:  # type: ignore[attr-defined]
            badges.append({"key": "review", "label": "Waiting for review", "css": ""})
        if job.has_open_rescans:  # type: ignore[attr-defined]
            badges.append({"key": "rescan", "label": "Rescan open", "css": ""})
        if job.refinish_needed:
            badges.append({"key": "refinish", "label": "Re-finish needed", "css": ""})
        active_run = job.active_run  # type: ignore[attr-defined]
        if active_run:
            badges.append(
                {
                    "key": "run",
                    "label": f"ScanTailor {active_run}",
                    "css": "running",
                }
            )
        job.pipeline = PipelineStatus(  # type: ignore[attr-defined]
            key=key,
            label=label,
            css=css,
            rendered=rendered,
            page_count=page_count,
            badges=badges,
        )
    return jobs
