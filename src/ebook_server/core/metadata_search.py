"""Recorded metadata-provider searches (``MetadataSearch``) shared by intake, review and scan."""

from __future__ import annotations

from typing import Any

from django.db.models import Prefetch
from django.utils import timezone

from . import identification
from .models import ImportJob, MetadataCandidate, MetadataSearch, ReviewTask, ScanJob

STATUS_LABELS = MetadataSearch.PROVIDER_STATUS_LABELS


def query_payload(signals: identification.IdentificationInput) -> dict[str, Any]:
    """The search terms actually used, as shown to reviewers."""
    return {
        "title_hints": list(signals.title_hints[:3]),
        "author": signals.best_author(),
        "author_hints": list(signals.author_hints),
        "isbns": list(signals.isbns),
        "language": signals.language,
        "publisher": signals.publisher,
    }


def start_search(
    *,
    trigger: str,
    signals: identification.IdentificationInput,
    import_job: ImportJob | None = None,
    scan_job: ScanJob | None = None,
    review_task: ReviewTask | None = None,
    requested_by_id: int | None = None,
    processing_job_id: Any = None,
) -> MetadataSearch:
    return MetadataSearch.objects.create(
        trigger=trigger,
        import_job=import_job,
        scan_job=scan_job,
        review_task=review_task,
        requested_by_id=requested_by_id,
        processing_job_id=processing_job_id,
        query=query_payload(signals),
        providers=[
            {**provider, "status": "pending"} for provider in identification.configured_providers()
        ],
    )


def run_search(
    search: MetadataSearch,
    signals: identification.IdentificationInput,
) -> list[identification.IdentificationCandidate]:
    """Ask the providers, record every provider's outcome, return ranked candidates."""
    try:
        candidates, outcomes = identification.resolve_candidates_with_report(signals)
    except Exception as exc:
        fail_search(search, str(exc) or type(exc).__name__)
        raise
    search.providers = [outcome.as_dict() for outcome in outcomes]
    search.candidate_count = len(candidates)
    search.state = MetadataSearch.State.COMPLETE
    search.finished_at = timezone.now()
    search.save(update_fields=["providers", "candidate_count", "state", "finished_at"])
    return candidates


def fail_search(search: MetadataSearch, error: str) -> None:
    search.state = MetadataSearch.State.FAILED
    search.error = error[:2000]
    search.finished_at = timezone.now()
    search.save(update_fields=["state", "error", "finished_at"])


def searches_for_task(task: ReviewTask) -> list[MetadataSearch]:
    if task.import_job_id is not None:
        queryset = MetadataSearch.objects.filter(import_job_id=task.import_job_id)
    elif task.scan_job_id is not None:
        queryset = MetadataSearch.objects.filter(scan_job_id=task.scan_job_id)
    else:
        return []
    return list(
        queryset.select_related("requested_by")
        .prefetch_related(
            Prefetch("candidates", queryset=MetadataCandidate.objects.order_by("-score", "id"))
        )
        .order_by("-started_at")
    )


def summary(search: MetadataSearch) -> str:
    """One-line outcome, e.g. "3 candidates; Open Library: error (timed out)"."""
    if search.state == MetadataSearch.State.RUNNING:
        return "running"
    if search.state == MetadataSearch.State.FAILED:
        return f"failed: {search.error}"
    parts = [f"{search.candidate_count} candidate{'s' if search.candidate_count != 1 else ''}"]
    for provider in search.provider_errors:
        parts.append(f"{provider.get('label')}: error ({provider.get('error')})")
    return "; ".join(parts)


def serialize_search(search: MetadataSearch) -> dict[str, Any]:
    return {
        "id": str(search.id),
        "trigger": search.trigger,
        "state": search.state,
        "requested_by": search.requested_by.get_username() if search.requested_by else None,
        "query": search.query,
        "providers": search.providers,
        "candidate_count": search.candidate_count,
        "candidate_ids": [candidate.id for candidate in search.candidates.all()],
        "error": search.error,
        "summary": summary(search),
        "started_at": search.started_at.isoformat(),
        "finished_at": search.finished_at.isoformat() if search.finished_at else None,
    }
