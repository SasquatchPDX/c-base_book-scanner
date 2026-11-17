"""Recorded, non-destructive metadata-provider searches shared by intake, review and scan.

Every identification run is persisted as a :class:`MetadataSearch` with the exact
terms, each provider's outcome and the candidates it returned. A new run only
supersedes earlier candidates when it returns candidates itself, so a failed or
empty repeat search never hides a good earlier result. Whether a search is still
in progress is derived from its queued/running ``ProcessingJob`` (with a bounded
staleness window), never from a status flag a killed worker could leave behind.

Searches are fenced: the source (``ImportJob`` / ``ScanJob``) names its current
search job in ``current_search_job_id``. A worker only starts a job that is still
queued and current, and only publishes candidates if, under the source's row lock,
its job is still current, still the attempt it started and the review is still
open. Anything else is stored as a DISCARDED run whose candidates are history.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, TypeGuard

import isbnlib
import structlog
from django.contrib.auth.models import AbstractBaseUser
from django.db import models as django_models
from django.db import transaction
from django.db.models import Prefetch, QuerySet
from django.utils import timezone

from ebook_server.jobs import get_queue, is_stale_running, stale_after, tunable_setting

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
SUPERSEDED_REASON = "a newer search replaced this one"
TAKEN_OVER_REASON = "a later attempt of this search took over"
REVIEW_CLOSED_REASON = "the review was resolved while this search was running"

logger = structlog.get_logger(__name__)


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


def canonical_isbn13(value: Any) -> str | None:
    """The ISBN-13 form of a valid ISBN-10/13 (isbnlib), so both forms compare equal."""
    if not isinstance(value, str | int):
        return None
    compact = isbnlib.canonical(str(value))
    if isbnlib.is_isbn10(compact):
        return isbnlib.to_isbn13(compact) or None
    if isbnlib.is_isbn13(compact):
        return compact
    return None


# Identifier keys and lists as providers return them (Open Library ``isbn_10`` /
# ``isbn_13`` / search ``isbn``; Google Books ``industryIdentifiers``; DNB and
# Hardcover mapped to ``isbn_10`` / ``isbn_13`` from the record itself).
_IDENTIFIER_TYPE_KEYS = ("type", "kind", "scheme")
_IDENTIFIER_VALUE_KEYS = ("identifier", "value", "id")
_MAX_RECORD_DEPTH = 4


def record_isbns(raw: Any) -> set[str]:
    """ISBN-13 forms of every ISBN the provider's own record lists.

    Keys starting with ``_`` are ours, not the provider's (``_queried_isbn`` is the
    lookup query echoed into the payload), so they are never evidence.
    """
    found: set[str] = set()

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_RECORD_DEPTH:
            return
        if isinstance(node, list):
            for item in node:
                walk(item, depth + 1)
            return
        if not isinstance(node, dict):
            return
        type_value = next(
            (node[key] for key in _IDENTIFIER_TYPE_KEYS if isinstance(node.get(key), str)), ""
        )
        if "isbn" in type_value.lower():
            for key in _IDENTIFIER_VALUE_KEYS:
                if (isbn := canonical_isbn13(node.get(key))) is not None:
                    found.add(isbn)
        for key, value in node.items():
            if not isinstance(key, str) or key.startswith("_"):
                continue
            if "isbn" in key.lower():
                for item in value if isinstance(value, list) else [value]:
                    if (isbn := canonical_isbn13(item)) is not None:
                        found.add(isbn)
            elif isinstance(value, dict | list):
                walk(value, depth + 1)

    walk(raw, 0)
    return found


def record_carries_isbn(candidate: MetadataCandidate, isbn: str) -> bool:
    """True when the provider record behind ``candidate`` itself lists ``isbn``.

    Only the stored raw provider payload counts; the candidate's normalized
    ``isbn10``/``isbn13`` may have been filled from the query and prove nothing.
    """
    wanted = canonical_isbn13(isbn)
    return wanted is not None and wanted in record_isbns(candidate.raw_payload)


def verified_isbn_candidates(
    candidates: Iterable[MetadataCandidate],
    isbn: str,
) -> list[MetadataCandidate]:
    """Candidates whose provider record carries ``isbn``, best first."""
    verified = [candidate for candidate in candidates if record_carries_isbn(candidate, isbn)]
    verified.sort(key=lambda candidate: (-candidate.score, candidate.id))
    return verified


def isbn_forms(isbn: str) -> list[str]:
    """``[isbn13, isbn10]`` for a valid ISBN-10/13 (the ISBN-10 only for 978-),
    or ``[]``. The order is the lookup order for library identifiers."""
    isbn13 = canonical_isbn13(isbn)
    if isbn13 is None:
        return []
    isbn10 = isbnlib.to_isbn10(isbn13) if isbn13.startswith("978") else ""
    return [isbn13, isbn10] if isbn10 else [isbn13]


def _named_authors(candidate: MetadataCandidate) -> list[str]:
    authors = candidate.authors if isinstance(candidate.authors, list) else []
    return [name for name in authors if isinstance(name, str) and name.strip()]


def same_book(left: MetadataCandidate, right: MetadataCandidate) -> bool:
    """True when two provider records can describe the same book.

    They are different books when their normalized titles differ, when both name
    authors and none of them is the same person (``identification.authors_conflict``:
    "Selected Poems" by Emily Dickinson vs by William Blake), or when both list
    ISBNs and share none (different editions). Missing authors or ISBNs on one
    side are no conflict.
    """
    if identification.normalize_text(left.title) != identification.normalize_text(right.title):
        return False
    if identification.authors_conflict(_named_authors(left), _named_authors(right)):
        return False
    left_isbns = record_isbns(left.raw_payload)
    right_isbns = record_isbns(right.raw_payload)
    return not (left_isbns and right_isbns and left_isbns.isdisjoint(right_isbns))


def distinct_books(candidates: Iterable[MetadataCandidate]) -> list[list[MetadataCandidate]]:
    """Group records into distinct books (several providers may agree on one).

    A record joins a group only when it is :func:`same_book` as every member, so a
    single group means every pair of records agrees on title, authors and ISBNs.
    """
    groups: list[list[MetadataCandidate]] = []
    for candidate in candidates:
        for group in groups:
            if all(same_book(candidate, member) for member in group):
                group.append(candidate)
                break
        else:
            groups.append([candidate])
    return groups


def isbn_lookup_result(
    search: MetadataSearch | None,
    current_ids: set[int],
) -> dict[str, Any] | None:
    """How the latest run answered an ISBN lookup, or ``None`` if it was no lookup.

    A candidate is *verified* when the provider's own record lists the looked-up
    ISBN (either form, see :func:`record_carries_isbn`); neither the typed value
    nor a query echoed into normalization is trusted. The lookup is *unambiguous*
    when all verified current records describe the same book (title and authors,
    :func:`distinct_books`), so several providers agreeing on one book still count
    as a single match.
    """
    if search is None or not isinstance(search.query, dict) or search.query.get("mode") != "isbn":
        return None
    queried = str((search.query.get("isbns") or [""])[0] or "")
    verified = verified_isbn_candidates(
        [candidate for candidate in search.candidates.all() if candidate.id in current_ids],
        queried,
    )
    books = distinct_books(verified)
    return {
        "isbn": (search.query.get("isbns") or [""])[0],
        "state": search.state,
        "provider_errors": len(search.provider_errors),
        "verified": verified,
        "verified_candidate_ids": [candidate.id for candidate in verified],
        "distinct_books": len(books),
        "unambiguous": len(books) == 1,
        "best": verified[0] if len(books) == 1 else None,
    }


def bound_isbn(candidate: MetadataCandidate) -> str | None:
    """The searched ISBN this candidate's own record carries, as ISBN-13, or ``None``.

    A record can list several ISBNs (other editions, print and ebook). When the
    search that found it asked for one of them (an intake search with the file's
    ISBN, or a reviewer's ISBN lookup) and the provider record itself lists it,
    that ISBN identifies the edition being accepted, not the record's first one.
    """
    search = candidate.search
    query = search.query if search is not None and isinstance(search.query, dict) else {}
    raw_queried = query.get("isbns")
    queried: list[Any] = raw_queried if isinstance(raw_queried, list) else []
    carried = record_isbns(candidate.raw_payload)
    for value in queried:
        if (isbn13 := canonical_isbn13(value)) is not None and isbn13 in carried:
            return isbn13
    return None


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
    discarded: str = "",
    mode: str = "text",
) -> MetadataSearch:
    """Persist one finished run.

    FAILED when ``error`` is given; DISCARDED (results kept as history only) when
    ``discarded`` gives the reason the run was not applied.
    """
    state = MetadataSearch.State.COMPLETE
    if discarded:
        state = MetadataSearch.State.DISCARDED
        error = f"not applied: {discarded}" + (f"; {error}" if error else "")
    elif error:
        state = MetadataSearch.State.FAILED
    return MetadataSearch.objects.create(
        trigger=trigger,
        state=state,
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
    current: bool = True,
    passed_over: Iterable[identification.IdentificationCandidate] = (),
) -> None:
    """Make ``candidates`` the current ones, keeping earlier ones as history.

    Must run inside a transaction. With no candidates nothing is superseded: an
    empty or failed repeat search leaves the earlier candidates acceptable.
    ``match_threshold`` of ``None`` keeps every candidate in REVIEW state, and
    ``passed_over`` candidates (not accepted automatically) stay in REVIEW whatever
    their score.
    ``current=False`` stores them as already superseded history (a discarded run).
    """
    if not candidates:
        return
    stored_at = timezone.now()
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
    if current:
        MetadataCandidate.objects.filter(**source, superseded_at__isnull=True).exclude(
            search=search
        ).update(superseded_at=stored_at)
    passed_over_ids = {id(candidate) for candidate in passed_over}
    seen: set[tuple[str, str]] = set()
    for index, candidate in enumerate(candidates, start=1):
        # Records without a provider key must not collide with each other.
        provider_ref = _fit("provider_ref", candidate.provider_ref) or f"result-{index}"
        if (candidate.provider_id, provider_ref) in seen:
            continue
        seen.add((candidate.provider_id, provider_ref))
        state = MetadataCandidate.CandidateState.REVIEW
        if (
            match_threshold is not None
            and candidate.score >= match_threshold
            and id(candidate) not in passed_over_ids
        ):
            state = MetadataCandidate.CandidateState.MATCH
        MetadataCandidate.objects.create(
            **source,
            search=search,
            superseded_at=None if current else stored_at,
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
    """Mark a stale search job FAILED so a new search can replace it.

    Its queue entry is cancelled (best effort) once this commits; should it still
    be delivered, or its worker still be alive, the fencing in
    :func:`start_refusal` / :func:`publication_refusal` keeps it from publishing.
    """
    details = dict(job.details) if isinstance(job.details, dict) else {}
    details["error"] = "the search did not finish (worker stopped or queue entry lost)"
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = details
    job.save(update_fields=["state", "finished_at", "details"])
    transaction.on_commit(lambda: cancel_queue_entry(job))


# ------------------------------------------------------------------------ fencing


def rq_job_id(job: ProcessingJob) -> str:
    """Deterministic queue-entry id, so a superseded entry can be cancelled."""
    return f"metadata-search-{job.job_id}"


def cancel_queue_entry(job: ProcessingJob) -> None:
    """Best effort: drop a superseded job's queue entry. Fencing makes it safe if not."""
    try:
        queue = get_queue()
        rq_queue = getattr(queue, "queue", None)
        if rq_queue is None:
            return
        from rq.job import Job

        Job.fetch(rq_job_id(job), connection=rq_queue.connection).cancel()
    except Exception as exc:  # already gone, running, or Redis unavailable
        logger.info(
            "metadata_search_queue_entry_not_cancelled",
            processing_job_id=str(job.job_id),
            reason=type(exc).__name__,
        )


def make_current(source: ImportJob | ScanJob, job: ProcessingJob) -> None:
    """Name ``job`` the source's current search (``source`` row must be locked)."""
    if source.current_search_job_id != job.job_id:
        source.current_search_job_id = job.job_id
        source.save(update_fields=["current_search_job_id"])


def attempt_of(job: ProcessingJob) -> int:
    details = job.details if isinstance(job.details, dict) else {}
    try:
        return int(details.get("attempt_count", 0))
    except (TypeError, ValueError):
        return 0


def start_refusal(job: ProcessingJob, current_search_job_id: Any) -> str | None:
    """Why a delivered queue entry must not run, or ``None`` when it may start.

    Only a QUEUED job (or a RUNNING one whose worker died) that is still its
    source's current search may run; a FAILED (abandoned/superseded) or finished
    job never runs again, whatever the queue delivers.
    """
    if job.state == ProcessingJob.JobState.RUNNING:
        if not is_stale_running(job):
            return "already running"
    elif job.state != ProcessingJob.JobState.QUEUED:
        return f"job is {job.state}"
    if current_search_job_id != job.job_id:
        return SUPERSEDED_REASON
    return None


def refuse_superseded_job(job: ProcessingJob) -> None:
    """Close a delivered job that a newer search replaced (``job`` row locked)."""
    if job.state not in ACTIVE_JOB_STATES:
        return
    details = dict(job.details) if isinstance(job.details, dict) else {}
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = {**details, "error": SUPERSEDED_REASON}
    job.save(update_fields=["state", "finished_at", "details"])


def publication_refusal(
    *,
    source: ImportJob | ScanJob,
    job: ProcessingJob,
    attempt: int,
    review_closed: bool,
) -> str:
    """Why a finished run must not become current ("" when it may publish).

    ``source`` and ``job`` must be rows locked and re-read inside the publishing
    transaction, so the answer cannot change before that transaction commits.
    """
    if job.state != ProcessingJob.JobState.RUNNING or attempt_of(job) != attempt:
        return TAKEN_OVER_REASON
    if source.current_search_job_id != job.job_id:
        return SUPERSEDED_REASON
    if review_closed:
        return REVIEW_CLOSED_REASON
    return ""


def owns_job(job: ProcessingJob, attempt: int) -> bool:
    """True while ``job`` (a locked row) is still the attempt this worker started."""
    return job.state == ProcessingJob.JobState.RUNNING and attempt_of(job) == attempt


def finish_discarded_job(job: ProcessingJob, *, attempt: int, reason: str, search_id: Any) -> None:
    """Close a run whose results were kept as history only (``job`` row locked).

    A job taken over by another attempt or already closed is left alone.
    """
    if not owns_job(job, attempt):
        return
    details = dict(job.details) if isinstance(job.details, dict) else {}
    job.state = ProcessingJob.JobState.SUCCESS
    job.finished_at = timezone.now()
    job.details = {
        **details,
        "search_id": str(search_id),
        "published": False,
        "discarded": reason,
        "error": "",
    }
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
