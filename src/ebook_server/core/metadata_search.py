"""Recorded, non-destructive metadata-provider searches shared by intake, review and scan.

Every identification run is persisted as a :class:`MetadataSearch` with the exact
terms, each provider's outcome and the candidates it returned. A new run only
supersedes earlier candidates when it returns candidates itself, so a failed or
empty repeat search never hides a good earlier result. Whether a search is still
in progress is derived from its queued/running ``ProcessingJob`` (with a bounded
staleness window), never from a status flag a killed worker could leave behind.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeGuard

from django.contrib.auth.models import AbstractBaseUser
from django.db import models as django_models
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from ebook_server.jobs import stale_after, tunable_setting

from . import identification
from .models import (
    ImportJob,
    MetadataCandidate,
    MetadataSearch,
    ProcessingJob,
    ReviewTask,
    ScanJob,
)

# A metadata search is a handful of bounded HTTP requests; a RUNNING search job
# older than this is treated as dead (worker killed / OOM) even though the generic
# job timeout is much longer, so a review page never stays locked for an hour.
DEFAULT_SEARCH_STALE_SECONDS = 300
SEARCH_JOB_TYPES = (
    ProcessingJob.JobType.REVIEW_IDENTIFICATION,
    ProcessingJob.JobType.SCAN_IDENTIFICATION,
)
ACTIVE_JOB_STATES = (ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING)
MAX_ERROR_LENGTH = 500


class SearchInProgressError(RuntimeError):
    """A search with other terms is already running for this review."""


# --------------------------------------------------------------------------- terms


def terms_from_payload(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Search terms posted by a reviewer, or ``None`` when none were posted.

    A blank title keeps the saved one (as "Save terms" always did); an authors
    field that is present but blank clears the authors.
    """
    raw_title = payload.get("inferred_title")
    raw_authors = payload.get("inferred_authors")
    if raw_title is None and raw_authors is None:
        return None
    terms: dict[str, Any] = {}
    if isinstance(raw_title, str) and raw_title.strip():
        terms["title"] = raw_title.strip()
    if isinstance(raw_authors, str):
        terms["authors"] = [name.strip() for name in raw_authors.split(",") if name.strip()]
    elif isinstance(raw_authors, list):
        terms["authors"] = [str(name).strip() for name in raw_authors if str(name).strip()]
    return terms


def apply_terms(task: ReviewTask, terms: dict[str, Any]) -> None:
    """Store reviewer terms on the task (the reviewer's saved search terms)."""
    title = terms.get("title")
    if isinstance(title, str) and title.strip():
        field = ReviewTask._meta.get_field("inferred_title")
        task.inferred_title = title.strip()[: getattr(field, "max_length", None) or None]
    authors = terms.get("authors")
    if isinstance(authors, list):
        task.inferred_authors = [str(name).strip() for name in authors if str(name).strip()]


def task_terms(task: ReviewTask) -> dict[str, Any]:
    """The task's saved search terms in the shape stored on search jobs."""
    return {
        "title": task.inferred_title.strip(),
        "authors": [
            str(name).strip()
            for name in (task.inferred_authors if isinstance(task.inferred_authors, list) else [])
            if isinstance(name, str) and name.strip()
        ],
    }


def query_payload(
    signals: identification.IdentificationInput,
    *,
    mode: str = "text",
) -> dict[str, Any]:
    """The terms actually sent to providers, as shown to reviewers."""
    return {
        "mode": mode,
        "title_hints": list(signals.title_hints[:3]),
        "author": signals.best_author(),
        # The first ISBN is the one queried; an ISBN lookup also lists its other form,
        # which counts when verifying a returned record.
        "isbns": list(signals.isbns[:2] if mode == "isbn" else signals.isbns[:1]),
    }


# ------------------------------------------------------------------- ISBN lookup


def parse_isbn(raw: Any) -> str | None:
    """The single valid ISBN-10/13 a reviewer typed, normalized, or ``None``.

    Spaces and hyphens are ignored; the check digit must be valid. Several ISBNs
    in one field are rejected rather than guessed between.
    """
    if not isinstance(raw, str):
        return None
    compact = re.sub(r"[\s\-\u2010-\u2015]", "", raw)
    compact = re.sub(r"^isbn(?:1[03])?:?", "", compact, flags=re.IGNORECASE)
    if not compact or len(compact) > 13:
        return None
    values = identification.normalize_isbns(compact)
    return values[0] if values else None


def isbn_terms(isbn: str) -> dict[str, Any]:
    return {"isbn": isbn}


def is_isbn_terms(terms: Any) -> TypeGuard[dict[str, Any]]:
    return isinstance(terms, dict) and isinstance(terms.get("isbn"), str) and bool(terms["isbn"])


def isbn_signals(isbn: str) -> identification.IdentificationInput:
    """Signals for a lookup by ISBN only: no title, author or folder fallback.

    Both ISBN forms are listed so a record carrying either one scores as an ISBN
    match; providers are queried with the first (the form the reviewer typed).
    """
    return identification.IdentificationInput(
        title_hints=[],
        author_hints=[],
        isbns=identification.normalize_isbns(isbn) or [isbn],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )


def isbn_lookup_result(
    search: MetadataSearch | None,
    current_ids: set[int],
) -> dict[str, Any] | None:
    """How the latest run answered an ISBN lookup, or ``None`` if it was no lookup.

    A candidate is *verified* when the provider record carries the looked-up ISBN
    (in either form); the typed value alone is never trusted. The lookup is
    *unambiguous* when all verified current records name the same title, so
    several providers agreeing on one book still count as a single match.
    """
    if search is None or not isinstance(search.query, dict) or search.query.get("mode") != "isbn":
        return None
    isbns = {str(value) for value in search.query.get("isbns") or [] if value}
    verified = [
        candidate
        for candidate in search.candidates.all()
        if candidate.id in current_ids and ({candidate.isbn10, candidate.isbn13} & isbns)
    ]
    verified.sort(key=lambda candidate: (-candidate.score, candidate.id))
    titles = {identification.normalize_text(candidate.title) for candidate in verified}
    return {
        "isbn": (search.query.get("isbns") or [""])[0],
        "state": search.state,
        "provider_errors": len(search.provider_errors),
        "verified": verified,
        "verified_candidate_ids": [candidate.id for candidate in verified],
        "unambiguous": len(titles) == 1,
        "best": verified[0] if len(titles) == 1 else None,
    }


# ----------------------------------------------------------------------- recording


def _fit(field_name: str, value: str | None) -> str:
    text = value or ""
    max_length = getattr(MetadataCandidate._meta.get_field(field_name), "max_length", None)
    if max_length and len(text) > max_length:
        return text[:max_length].rstrip()
    return text


def record_search(
    *,
    trigger: str,
    signals: identification.IdentificationInput,
    outcomes: list[identification.ProviderOutcome] | None,
    candidate_count: int = 0,
    import_job: ImportJob | None = None,
    scan_job: ScanJob | None = None,
    processing_job_id: Any = None,
    requested_by_id: int | None = None,
    started_at: datetime | None = None,
    error: str = "",
    mode: str = "text",
) -> MetadataSearch:
    """Persist one finished run (FAILED when ``error`` is given)."""
    return MetadataSearch.objects.create(
        trigger=trigger,
        state=MetadataSearch.State.FAILED if error else MetadataSearch.State.COMPLETE,
        import_job=import_job,
        scan_job=scan_job,
        processing_job_id=processing_job_id,
        requested_by_id=requested_by_id,
        query=query_payload(signals, mode=mode),
        providers=[outcome.as_dict() for outcome in outcomes or []],
        candidate_count=candidate_count,
        error=error[:MAX_ERROR_LENGTH],
        started_at=started_at or timezone.now(),
        finished_at=timezone.now(),
    )


def save_candidates(
    *,
    search: MetadataSearch,
    candidates: list[identification.IdentificationCandidate],
    match_threshold: float | None,
) -> None:
    """Make ``candidates`` the current ones, keeping earlier ones as history.

    Must run inside a transaction. With no candidates nothing is superseded: an
    empty or failed repeat search leaves the earlier candidates acceptable.
    ``match_threshold`` of ``None`` keeps every candidate in REVIEW state.
    """
    if not candidates:
        return
    source: dict[str, Any] = (
        {"import_job_id": search.import_job_id}
        if search.import_job_id is not None
        else {"scan_job_id": search.scan_job_id}
    )
    # Serialise concurrent runs for one source so two of them never both insert
    # "current" rows (the partial unique constraints would reject the second).
    if search.import_job_id is not None:
        list(ImportJob.objects.select_for_update().filter(pk=search.import_job_id))
    else:
        list(ScanJob.objects.select_for_update().filter(pk=search.scan_job_id))
    MetadataCandidate.objects.filter(**source, superseded_at__isnull=True).exclude(
        search=search
    ).update(superseded_at=timezone.now())
    seen: set[tuple[str, str]] = set()
    for index, candidate in enumerate(candidates, start=1):
        # Records without a provider key must not collide with each other.
        provider_ref = _fit("provider_ref", candidate.provider_ref) or f"result-{index}"
        if (candidate.provider_id, provider_ref) in seen:
            continue
        seen.add((candidate.provider_id, provider_ref))
        state = MetadataCandidate.CandidateState.REVIEW
        if match_threshold is not None and candidate.score >= match_threshold:
            state = MetadataCandidate.CandidateState.MATCH
        MetadataCandidate.objects.create(
            **source,
            search=search,
            provider_id=candidate.provider_id,
            provider_ref=provider_ref,
            title=_fit("title", candidate.title),
            authors=candidate.authors,
            isbn10=_fit("isbn10", candidate.isbn10),
            isbn13=_fit("isbn13", candidate.isbn13),
            score=Decimal(str(candidate.score)),
            raw_payload=candidate.raw,
            subjects=candidate.subjects,
            language=_fit("language", candidate.language),
            publisher=_fit("publisher", candidate.publisher),
            description=candidate.description or "",
            publication_year=candidate.publication_year,
            state=state,
        )


def discard_intake_runs(import_job: ImportJob) -> None:
    """Drop an earlier intake attempt's runs and candidates (intake retry idempotence)."""
    MetadataCandidate.objects.filter(import_job=import_job).filter(
        django_models.Q(search__isnull=True)
        | django_models.Q(search__trigger=MetadataSearch.Trigger.INTAKE)
    ).delete()
    MetadataSearch.objects.filter(
        import_job=import_job,
        trigger=MetadataSearch.Trigger.INTAKE,
    ).delete()


def snapshot(candidates: list[identification.IdentificationCandidate]) -> list[dict[str, Any]]:
    return [
        {
            "provider_id": candidate.provider_id,
            "provider_ref": candidate.provider_ref,
            "score": float(candidate.score),
        }
        for candidate in candidates
    ]


# -------------------------------------------------------------------------- reading


def _source_filter(task: ReviewTask) -> dict[str, Any] | None:
    if task.import_job_id is not None:
        return {"import_job_id": task.import_job_id}
    if task.scan_job_id is not None:
        return {"scan_job_id": task.scan_job_id}
    return None


def current_candidates(task: ReviewTask) -> QuerySet[MetadataCandidate]:
    source = _source_filter(task)
    if source is None:
        return MetadataCandidate.objects.none()
    return MetadataCandidate.objects.filter(**source, superseded_at__isnull=True).order_by(
        "-score", "id"
    )


def searches_for_task(task: ReviewTask) -> list[MetadataSearch]:
    """All recorded runs for the task's source, newest first, with their candidates."""
    source = _source_filter(task)
    if source is None:
        return []
    return list(
        MetadataSearch.objects.filter(**source)
        .select_related("requested_by")
        .prefetch_related(
            Prefetch("candidates", queryset=MetadataCandidate.objects.order_by("-score", "id"))
        )
        .order_by("-finished_at")
    )


def _search_jobs(task: ReviewTask) -> QuerySet[ProcessingJob]:
    if task.import_job_id is not None:
        return ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION,
            details__review_task_id=task.id,
        )
    if task.scan_job_id is not None:
        return ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.SCAN_IDENTIFICATION,
            details__scan_job_id=str(task.scan_job_id),
        )
    return ProcessingJob.objects.none()


def search_stale_after() -> timedelta:
    seconds = float(
        tunable_setting("BOOK_SCANNER_METADATA_SEARCH_STALE_SECONDS", DEFAULT_SEARCH_STALE_SECONDS)
    )
    return timedelta(seconds=max(30.0, seconds))


def is_stale_search_job(job: ProcessingJob, *, now: datetime | None = None) -> bool:
    """True for a queued/running search job that can no longer finish by itself.

    RUNNING: older than the (short) search window, so the worker died. QUEUED:
    older than the generic job window, so the queue entry was lost; a queued job
    merely waiting behind a long conversion is not stale.
    """
    current = now or timezone.now()
    if job.state == ProcessingJob.JobState.RUNNING:
        started = job.started_at or job.scheduled_at
        limit = min(search_stale_after(), stale_after(job.job_type))
        return current - started > limit
    if job.state == ProcessingJob.JobState.QUEUED:
        return current - job.scheduled_at > stale_after(job.job_type)
    return False


def active_search_job(task: ReviewTask) -> ProcessingJob | None:
    """The live queued/running search job for this review, ignoring stale ones."""
    now = timezone.now()
    for job in _search_jobs(task).filter(state__in=ACTIVE_JOB_STATES).order_by("-scheduled_at"):
        if not is_stale_search_job(job, now=now):
            return job
    return None


def latest_search_job(task: ReviewTask) -> ProcessingJob | None:
    return _search_jobs(task).order_by("-scheduled_at").first()


def abandon_stale_job(job: ProcessingJob) -> None:
    """Mark a stale search job FAILED so a new search can replace it."""
    details = dict(job.details) if isinstance(job.details, dict) else {}
    details["error"] = "the search did not finish (worker stopped or queue entry lost)"
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = details
    job.save(update_fields=["state", "finished_at", "details"])


def reuse_or_replace_active_job(
    active: ProcessingJob | None,
    terms: dict[str, Any],
) -> ProcessingJob | None:
    """Decide what to do with an existing queued/running job (row must be locked).

    Returns the job to reuse, or ``None`` when a new job must be created. Stale
    jobs are failed and replaced; a queued job takes the newly submitted terms;
    a running job with other terms raises :class:`SearchInProgressError`.
    """
    if active is None:
        return None
    if is_stale_search_job(active):
        abandon_stale_job(active)
        return None
    details = dict(active.details) if isinstance(active.details, dict) else {}
    if details.get("terms") == terms:
        return active
    if active.state == ProcessingJob.JobState.QUEUED:
        active.details = {**details, "terms": terms}
        active.save(update_fields=["details"])
        return active
    raise SearchInProgressError(
        "a search is already running; search again with the new terms once it finishes"
    )


def summary(search: MetadataSearch) -> str:
    """One-line outcome, e.g. "3 candidates; openlibrary: error (timed out)"."""
    if search.state == MetadataSearch.State.FAILED:
        return f"failed: {search.error}"
    count = search.candidate_count
    parts = [f"{count} candidate{'s' if count != 1 else ''}"]
    for row in search.provider_errors:
        parts.append(f"{row.get('provider_id')}: error ({row.get('error')})")
    return "; ".join(parts)


def serialize_search(search: MetadataSearch) -> dict[str, Any]:
    requested_by: AbstractBaseUser | None = search.requested_by
    return {
        "id": str(search.id),
        "trigger": search.trigger,
        "state": search.state,
        "requested_by": requested_by.get_username() if requested_by is not None else None,
        "processing_job_id": str(search.processing_job_id) if search.processing_job_id else None,
        "query": search.query,
        "providers": search.providers,
        "candidate_count": search.candidate_count,
        "candidate_ids": [candidate.id for candidate in search.candidates.all()],
        "error": search.error,
        "summary": summary(search),
        "started_at": search.started_at.isoformat(),
        "finished_at": search.finished_at.isoformat(),
    }
