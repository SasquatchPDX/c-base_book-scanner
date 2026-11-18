"""Metadata review queue, candidate selection and manual recovery workflows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, cast
from urllib.parse import urlencode

import structlog
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import F, OuterRef, Q, Subquery
from django.db.models.functions import Coalesce
from django.http import (
    HttpRequest,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import (
    contributions,
    genres,
    intake,
    llm,
    llm_identification,
    local_identity,
    metadata_search,
    multipart,
    review_accept,
    review_batch,
    review_identification,
    scan_identification,
    scanner_opf,
    worker_failure_views,
)
from .models import (
    AppUserProfile,
    Book,
    FileVariant,
    Genre,
    LLMSuggestion,
    MetadataCandidate,
    MetadataSearch,
    ProcessingJob,
    ReviewDecisionCorrection,
    ReviewTask,
    SourceLocation,
)
from .view_helpers import (
    _active_library_profile,
    _as_positive_int,
    _coerce_int,
    _json_body,
    _profile_for_request,
    _request_payload,
    _wants_html,
)

logger = structlog.get_logger(__name__)


def _can_manage_review_task(
    profile: AppUserProfile,
    task: ReviewTask,
    *,
    allow_worker_failure: bool = False,
) -> bool:
    if task.processing_job_id is not None:
        return allow_worker_failure and (
            profile.role == "admin" or task.uploader_id == profile.user_id
        )
    if profile.role == "admin":
        return True
    import_job = task.import_job
    if import_job is not None:
        return import_job.uploader_id == profile.user_id
    scan_job = task.scan_job
    if scan_job is not None:
        return scan_job.operator_id == profile.user_id
    return False


def _review_owner_id(task: ReviewTask) -> int | None:
    if task.processing_job_id is not None:
        return task.uploader_id
    import_job = task.import_job
    if import_job is not None:
        return import_job.uploader_id
    scan_job = task.scan_job
    if scan_job is not None:
        return scan_job.operator_id
    return None


def _note_review_redecided(task: ReviewTask, profile: AppUserProfile) -> None:
    """A resolved review decided again: the earlier decision no longer stands (#27)."""
    contributions.record_review_reversal(
        task,
        source=ReviewDecisionCorrection.Source.RE_RESOLVED,
        found_by=profile.user,
    )


def _review_candidates(task: ReviewTask) -> Any:
    """Current candidates only; superseded runs are history, not acceptable choices."""
    return metadata_search.current_candidates(task)


def _review_search_context(task: ReviewTask) -> dict[str, Any]:
    """Recorded search runs plus the live/failed state of the latest search job."""
    searches = metadata_search.searches_for_task(task)
    active_job = metadata_search.active_search_job(task)
    latest_job = metadata_search.latest_search_job(task)
    # A discarded run (superseded, or finished after the review was resolved) is
    # history only; the latest search shown is the latest one that was applied.
    applied = [search for search in searches if search.state != MetadataSearch.State.DISCARDED]
    latest_search = applied[0] if applied else None
    current_ids = set(_review_candidates(task).values_list("id", flat=True))
    # A failed job that recorded no run (dispatch failure, stale worker) must still
    # be visible; one that did record a run is shown through that run.
    failed_job = None
    if (
        active_job is None
        and latest_job is not None
        and latest_job.state != ProcessingJob.JobState.SUCCESS
        and not any(search.processing_job_id == latest_job.job_id for search in searches)
    ):
        failed_job = latest_job
    stale_job = (
        latest_job is not None
        and active_job is None
        and latest_job.state in (metadata_search.ACTIVE_JOB_STATES)
    )
    return {
        "searches": searches,
        "latest_search": latest_search,
        "earlier_searches": [search for search in searches if search is not latest_search],
        "active_search_job": active_job,
        "search_running": (
            active_job is not None and active_job.state == ProcessingJob.JobState.RUNNING
        ),
        "failed_search_job": failed_job,
        "failed_search_error": (
            "the search did not finish (worker stopped or queue entry lost)"
            if stale_job
            else str((failed_job.details or {}).get("error") or "")
            if failed_job is not None
            else ""
        ),
        "latest_search_has_current_candidates": (
            latest_search is not None
            and any(candidate.id in current_ids for candidate in latest_search.candidates.all())
        ),
        "isbn_lookup": metadata_search.isbn_lookup_result(latest_search, current_ids),
    }


def _serialize_task(task: ReviewTask) -> dict[str, Any]:
    source_type = "import" if task.import_job_id is not None else "scan"
    source_id = task.import_job_id or task.scan_job_id
    if task.processing_job_id is not None:
        source_type = "processing"
        processing_job = task.processing_job
        assert processing_job is not None
        source_id = processing_job.job_id
    return {
        "id": str(task.id),
        "state": task.state,
        "decision": task.decision,
        "task_type": task.task_type,
        "source_filename": task.source_filename,
        "source_path_hint": task.source_path_hint,
        "inferred_title": task.inferred_title,
        "inferred_authors": task.inferred_authors,
        "embedded_metadata": task.embedded_metadata,
        "provider_snapshot": task.provider_snapshot,
        "source_type": source_type,
        "source_id": str(source_id) if source_id is not None else None,
        "uploader_id": _review_owner_id(task),
        "reviewer_id": task.reviewer_id,
        "resolved_book_id": str(task.resolved_book_id) if task.resolved_book_id else None,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


# Moved to review_accept so workers resolve reviews through the same path (#59).
_resolve_review_source = review_accept.resolve_review_source


def _serialize_candidate(candidate: MetadataCandidate) -> dict[str, Any]:
    return {
        "id": str(candidate.id),
        "provider_id": candidate.provider_id,
        "title": candidate.title,
        "authors": candidate.authors,
        "isbn10": candidate.isbn10,
        "isbn13": candidate.isbn13,
        "score": float(candidate.score),
        "language": candidate.language,
        "publisher": candidate.publisher,
        "publication_year": candidate.publication_year,
        "description": candidate.description,
        "subjects": candidate.subjects,
        "state": candidate.state,
    }


def _review_action_response(
    request: HttpRequest,
    payload: dict[str, Any],
    *,
    task_id: int | None = None,
) -> HttpResponseBase:
    if _wants_html(request):
        if task_id is not None:
            return redirect("review-detail", task_id=task_id)
        return redirect("review-queue")
    return JsonResponse(payload)


def _parse_author_list(raw_authors: Any) -> list[str]:
    """Authors from a comma-separated form value or a JSON list."""
    if isinstance(raw_authors, str):
        return [entry.strip() for entry in raw_authors.split(",") if entry.strip()]
    if isinstance(raw_authors, list):
        return [str(name).strip() for name in raw_authors if str(name).strip()]
    return []


_AUTO_ACCEPT_BLOCK_MESSAGES = {
    "no_provider_authors": (
        "The best match scored high enough to be accepted automatically, but the "
        "metadata provider lists no authors for it. Confirm or edit the suggested "
        "authors from the file before accepting it."
    ),
    "author_conflict": (
        "The best match scored high enough to be accepted automatically, but its "
        "authors differ from the authors in the file's own metadata."
    ),
    "stronger_title_evidence_disagrees": (
        "The best match scored high enough to be accepted automatically, but its "
        "title matches only a weaker hint (such as the filename) while the file's "
        "stronger title evidence names another book."
    ),
    "llm_derived": (
        "The best match was found through search terms suggested by the local AI "
        "server. AI suggestions are unverified, so a person must confirm the match."
    ),
    "ambiguous_tie": (
        "Several different books tied for the best score, so none was accepted "
        "automatically. Pick the right one below."
    ),
    "edition_conflict": (
        "The best match scored high enough to be accepted automatically, but it "
        "describes another edition than the file: its ISBN, language or publisher "
        "does not match the file's own metadata."
    ),
}


def _auto_accept_block(task: ReviewTask) -> dict[str, Any] | None:
    """Why an otherwise auto-accepted import was routed to review, for the review page."""
    evidence = task.embedded_metadata if isinstance(task.embedded_metadata, dict) else {}
    reason = evidence.get(intake.AUTO_ACCEPT_BLOCKED_KEY)
    if not isinstance(reason, str) or not reason:
        return None
    details = evidence.get(intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY)
    return {
        "reason": reason,
        "message": _AUTO_ACCEPT_BLOCK_MESSAGES.get(
            reason, "The best match was not accepted automatically."
        ),
        "details": details if isinstance(details, dict) else {},
        "suggested_authors": _parse_author_list(evidence.get(intake.SUGGESTED_AUTHORS_KEY)),
    }


def _local_suggestion(
    task: ReviewTask, candidates: list[MetadataCandidate]
) -> dict[str, Any] | None:
    """The file's local-consensus suggestion (``local_identity``), for the review page.

    ``confirmed_candidate`` is the current provider candidate the suggestion confirms;
    without one the suggestion pre-fills manual metadata. Never accepted by itself.
    """
    evidence = task.embedded_metadata if isinstance(task.embedded_metadata, dict) else {}
    local = evidence.get(local_identity.LOCAL_IDENTITY_KEY)
    if not isinstance(local, dict) or not isinstance(local.get("title"), str):
        return None
    confirms = local.get("confirms") if isinstance(local.get("confirms"), dict) else None
    confirmed = None
    if confirms is not None:
        confirmed = next(
            (
                candidate
                for candidate in candidates
                if candidate.provider_id == confirms.get("provider_id")
                and candidate.provider_ref == confirms.get("provider_ref")
            ),
            None,
        )
    outage = local.get("provider_outage")
    origins = local.get("title_origins")
    return {
        "title": local["title"],
        "authors": _parse_author_list(local.get("authors")),
        "language": local.get("language") if isinstance(local.get("language"), str) else "",
        "title_origins": [o for o in origins if isinstance(o, str)]
        if isinstance(origins, list)
        else [],
        "author_source": local.get("author_source"),
        "provider_outage": [p for p in outage if isinstance(p, str)]
        if isinstance(outage, list)
        else [],
        "confirmed_candidate": confirmed,
        "confirmed_candidate_id": confirmed.pk if confirmed is not None else None,
    }


def _reviewer_genre_choice(request: HttpRequest, payload: dict[str, Any]) -> list[Genre] | None:
    """Genres a reviewer picked, or ``None`` when they made no choice.

    JSON clients send ``genre_ids`` (a list, possibly empty). The review form sends
    ``genres_reviewed=1`` plus one ``genre_ids`` checkbox value per chosen genre, so
    unticking every suggestion is an explicit "no genres". Raises ``ValueError`` for
    ids that are not existing genres.
    """
    if isinstance(payload.get("genre_ids"), list):
        raw_ids: list[Any] = payload["genre_ids"]
    elif payload.get("genres_reviewed"):
        raw_ids = request.POST.getlist("genre_ids")
    else:
        return None
    ids: list[int] = []
    for raw in raw_ids:
        value = _coerce_int(raw, minimum=1)
        if value is None:
            raise ValueError("invalid genre id")
        if value not in ids:
            ids.append(value)
    found = {genre.pk: genre for genre in Genre.objects.filter(pk__in=ids)}
    if len(found) != len(ids):
        raise ValueError("unknown genre")
    return [found[genre_id] for genre_id in ids]


_apply_review_genres = review_accept.apply_review_genres


def _attach_genre_suggestions(task: ReviewTask, candidates: list[MetadataCandidate]) -> None:
    """Set ``candidate.genre_suggestion`` for the review page's genre picker."""
    index = genres.build_index()
    embedded = genres.embedded_subjects(task.embedded_metadata)
    for candidate in candidates:
        candidate.genre_suggestion = genres.suggest(  # type: ignore[attr-defined]
            candidate.subjects, embedded, index
        )


def _serialize_genre_suggestion(suggestion: genres.GenreSuggestion) -> dict[str, Any]:
    return {
        "source": suggestion.source,
        "genre_ids": suggestion.genre_ids,
        "tags": [
            {
                "tag": match.tag,
                "genre_id": match.genre.pk if match.genre else None,
                "genre": match.genre.name if match.genre else None,
            }
            for match in suggestion.matches
        ],
    }


def _build_manual_book_from_payload(payload: dict[str, Any]) -> Book:
    from . import identification

    raw_title = payload.get("title")
    if not isinstance(raw_title, str):
        raise ValueError("title required")
    title = raw_title.strip()
    if not title:
        raise ValueError("title required")

    authors = _parse_author_list(payload.get("authors", []))

    publication_year: int | None = None
    raw_year = payload.get("publication_year")
    if isinstance(raw_year, int) and raw_year > 0:
        publication_year = raw_year
    elif isinstance(raw_year, str) and raw_year.strip().isdigit():
        publication_year = int(raw_year.strip())

    isbn10: str | None = None
    raw_isbn10 = payload.get("isbn10")
    if isinstance(raw_isbn10, str) and raw_isbn10.strip():
        normalized_isbns = identification.normalize_isbns(raw_isbn10)
        isbn10 = next((value for value in normalized_isbns if len(value) == 10), None)

    isbn13: str | None = None
    raw_isbn13 = payload.get("isbn13")
    if isinstance(raw_isbn13, str) and raw_isbn13.strip():
        normalized_isbns = identification.normalize_isbns(raw_isbn13)
        isbn13 = next((value for value in normalized_isbns if len(value) == 13), None)

    return intake._provision_book_from_manual_metadata(
        title=title,
        authors=authors,
        publisher=str(payload.get("publisher", "") or ""),
        language=str(payload.get("language", "") or ""),
        description=str(payload.get("description", "") or ""),
        edition=str(payload.get("edition", "") or ""),
        publication_year=publication_year,
        isbn10=isbn10,
        isbn13=isbn13,
    )


REVIEW_QUEUE_SORTS: Final = {
    "-created": ("-created_at", "-id"),
    "created": ("created_at", "id"),
    "-confidence": ("-top_score", "-created_at"),
    "confidence": ("top_score", "-created_at"),
}
REVIEW_QUEUE_STATES: Final = (ReviewTask.State.OPEN, ReviewTask.State.CLAIMED)


def _parse_score(raw: str | None) -> float | None:
    if raw is None or not raw.strip():
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not 0.0 <= value <= 1.0:  # also rejects NaN
        return None
    return value


def _review_queue_queryset(profile: AppUserProfile) -> Any:
    """Open/claimed review tasks visible to ``profile``, annotated with their top score."""

    def top_score(candidates: Any) -> Subquery:
        return Subquery(
            candidates.filter(superseded_at__isnull=True).order_by("-score").values("score")[:1]
        )

    tasks = (
        ReviewTask.objects.select_related(
            "import_job",
            "import_job__uploader",
            "import_job__source_location",
            "scan_job",
            "scan_job__operator",
            "processing_job",
        )
        .filter(state__in=REVIEW_QUEUE_STATES)
        .annotate(
            top_score=Coalesce(
                top_score(MetadataCandidate.objects.filter(import_job=OuterRef("import_job"))),
                top_score(MetadataCandidate.objects.filter(scan_job=OuterRef("scan_job"))),
            ),
        )
    )
    if profile.role != "admin":
        tasks = tasks.filter(
            Q(import_job__uploader=profile.user)
            | Q(scan_job__operator=profile.user)
            | Q(processing_job__isnull=False, uploader=profile.user)
        )
    return tasks


def _review_queue_filters(request: HttpRequest) -> dict[str, Any]:
    task_type = request.GET.get("task_type", "").strip()
    if task_type not in ReviewTask.TaskType.values:
        task_type = ""
    state = request.GET.get("state", "").strip()
    if state not in {choice.value for choice in REVIEW_QUEUE_STATES}:
        state = ""
    has_candidates = request.GET.get("has_candidates", "").strip().lower()
    if has_candidates not in {"yes", "no"}:
        has_candidates = ""
    sort = request.GET.get("sort", "-created").strip()
    if sort not in REVIEW_QUEUE_SORTS:
        sort = "-created"
    source = request.GET.get("source", "").strip()
    if source not in {"upload", "scan"} and not source.isdigit():
        source = ""
    return {
        "task_type": task_type,
        "source": source,
        "state": state,
        "min_score": _parse_score(request.GET.get("min_score")),
        "max_score": _parse_score(request.GET.get("max_score")),
        "has_candidates": has_candidates,
        "sort": sort,
    }


def _apply_review_queue_filters(tasks: Any, filters: dict[str, Any]) -> Any:
    if filters["task_type"]:
        tasks = tasks.filter(task_type=filters["task_type"])
    source = filters["source"]
    if source == "upload":
        tasks = tasks.filter(import_job__isnull=False, import_job__source_location__isnull=True)
    elif source == "scan":
        tasks = tasks.filter(scan_job__isnull=False)
    elif source:
        tasks = tasks.filter(import_job__source_location_id=int(source))
    if filters["state"]:
        tasks = tasks.filter(state=filters["state"])
    if filters["min_score"] is not None:
        tasks = tasks.filter(top_score__gte=Decimal(str(filters["min_score"])))
    if filters["max_score"] is not None:
        tasks = tasks.filter(top_score__lte=Decimal(str(filters["max_score"])))
    if filters["has_candidates"] == "yes":
        tasks = tasks.filter(top_score__isnull=False)
    elif filters["has_candidates"] == "no":
        tasks = tasks.filter(top_score__isnull=True)
    ordering: list[Any] = []
    for field in REVIEW_QUEUE_SORTS[filters["sort"]]:
        if field == "top_score":
            ordering.append(F("top_score").asc(nulls_last=True))
        elif field == "-top_score":
            ordering.append(F("top_score").desc(nulls_last=True))
        else:
            ordering.append(field)
    return tasks.order_by(*ordering)


def _review_queue_querystring(filters: dict[str, Any], page_size: int) -> str:
    """Filter state for pagination links, so paging never drops the active filters."""
    params: dict[str, str] = {"view": "html", "page_size": str(page_size)}
    for key in ("source", "state", "has_candidates", "sort", "task_type"):
        if filters[key]:
            params[key] = filters[key]
    for key in ("min_score", "max_score"):
        if filters[key] is not None:
            params[key] = f"{filters[key]:g}"
    return urlencode(params)


@require_GET
@login_required
def review_queue(request: HttpRequest) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    filters = _review_queue_filters(request)
    tasks = _apply_review_queue_filters(_review_queue_queryset(profile), filters)

    page_size = min(
        _as_positive_int(request.GET.get("page_size"), review_batch.queue_page_size()),
        review_batch.MAX_QUEUE_PAGE_SIZE,
    )
    paginator = Paginator(tasks, page_size)
    page_object = paginator.get_page(_as_positive_int(request.GET.get("page"), 1))

    min_score = review_batch.batch_min_score()
    margin = review_batch.batch_ambiguity_margin()
    rows: list[dict[str, Any]] = []
    payload: list[dict[str, Any]] = []
    task_list = list(page_object.object_list)
    for task in task_list:
        candidates = list(_review_candidates(task))
        eligibility = review_batch.evaluate_top_candidates(
            [candidate.score for candidate in candidates],
            min_score=min_score,
            ambiguity_margin=margin,
        )
        rows.append(
            {
                "task": task,
                "top_candidate": candidates[0] if candidates else None,
                "candidate_count": len(candidates),
                "eligibility": eligibility,
            },
        )
        payload.append(
            {
                **_serialize_task(task),
                "source_location_id": (
                    task.import_job.source_location_id if task.import_job is not None else None
                ),
                "top_score": eligibility.top_score,
                "batch_accept_eligibility": eligibility.reason,
                "candidate_count": len(candidates),
                "candidates": [_serialize_candidate(candidate) for candidate in candidates],
            },
        )
    # Formats of one book on this page collapse into one entry (#49); only the primary
    # entry offers a batch checkbox, its siblings are re-assessed when it is accepted.
    groups = _group_sibling_review_tasks(task_list, payload)
    row_by_task = {row["task"].id: row for row in rows}
    group_rows = [{**row_by_task[group.primary.id], "group": group} for group in groups]
    if _wants_html(request):
        return render(
            request,
            "core/review_queue.html",
            {
                "rows": group_rows,
                "groups": groups,
                "count": paginator.count,
                "page_object": page_object,
                "filters": filters,
                "querystring": _review_queue_querystring(filters, page_size),
                "source_locations": SourceLocation.objects.order_by("name"),
                "task_types": ReviewTask.TaskType.choices,
                "batch_min_score": min_score,
                "batch_ambiguity_margin": margin,
                "batch_max_tasks": review_batch.batch_max_tasks(),
            },
        )
    return JsonResponse(
        {
            "items": payload,
            "count": paginator.count,
            "page": page_object.number,
            "num_pages": paginator.num_pages,
            "page_size": page_size,
            "group_count": len(groups),
        },
    )


# Formats most likely to carry real embedded metadata first: the primary entry of a
# sibling group should be the most informative file to review (#49).
REVIEW_FORMAT_PRIORITY = ("epub", "azw3", "mobi", "fb2", "pdf", "djvu", "html", "rtf", "txt")


@dataclass
class ReviewQueueGroup:
    primary: ReviewTask
    primary_item: dict[str, Any]
    others: list[tuple[ReviewTask, dict[str, Any]]]

    @property
    def other_count(self) -> int:
        return len(self.others)


def _group_sibling_review_tasks(
    tasks: list[ReviewTask],
    items: list[dict[str, Any]],
) -> list[ReviewQueueGroup]:
    """Collapse open review tasks for formats of one book into one queue entry.

    Uses the sibling key of ``intake._sibling_jobs`` (source, folder, case-folded name;
    only for sources with ``group_sibling_formats``). The primary entry is the task with
    the best candidate score, then the most metadata-rich format, then the oldest. Each
    item in ``items`` gains ``sibling_group`` (``None`` when it stands alone).
    """
    keyed: dict[tuple[int, str, str], list[int]] = {}
    order: list[tuple[int, str, str] | int] = []
    for index, task in enumerate(tasks):
        key = intake.sibling_group_key(task.import_job) if task.import_job else None
        if key is None:
            order.append(index)
            continue
        if key not in keyed:
            keyed[key] = []
            order.append(key)
        keyed[key].append(index)

    def rank(index: int) -> tuple[float, int, datetime]:
        scores = [candidate["score"] for candidate in items[index]["candidates"]]
        job = tasks[index].import_job
        fmt = job.detected_format if job is not None else ""
        priority = (
            REVIEW_FORMAT_PRIORITY.index(fmt)
            if fmt in REVIEW_FORMAT_PRIORITY
            else len(REVIEW_FORMAT_PRIORITY)
        )
        return (-max(scores, default=0.0), priority, tasks[index].created_at)

    groups: list[ReviewQueueGroup] = []
    for entry in order:
        members = sorted(keyed[entry], key=rank) if isinstance(entry, tuple) else [entry]
        primary, *others = members
        if len(members) > 1:
            annotation = {
                "primary_task_id": str(tasks[primary].id),
                "task_ids": [str(tasks[index].id) for index in members],
                "size": len(members),
            }
            for index in members:
                items[index]["sibling_group"] = annotation
        else:
            items[primary]["sibling_group"] = None
        groups.append(
            ReviewQueueGroup(
                primary=tasks[primary],
                primary_item=items[primary],
                others=[(tasks[index], items[index]) for index in others],
            )
        )
    return groups


@require_GET
@login_required
def review_detail(request: HttpRequest, task_id: str) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task, allow_worker_failure=True):
        return HttpResponseForbidden()
    if task.processing_job_id is not None:
        return worker_failure_views.detail(request, task)

    payload = _serialize_task(task)
    candidates = list(_review_candidates(task))
    _attach_genre_suggestions(task, candidates)
    payload["candidates"] = [
        {
            **_serialize_candidate(candidate),
            "genre_suggestion": _serialize_genre_suggestion(candidate.genre_suggestion),
        }
        for candidate in candidates
    ]
    auto_accept_block = _auto_accept_block(task)
    payload["auto_accept_block"] = auto_accept_block
    local_suggestion = _local_suggestion(task, candidates)
    payload["local_suggestion"] = (
        {key: value for key, value in local_suggestion.items() if key != "confirmed_candidate"}
        if local_suggestion is not None
        else None
    )
    llm_panel = _llm_panel(task)
    payload["llm_suggestion"] = llm_panel["suggestion_payload"] if llm_panel else None
    multipart_group = multipart.group_for_task(task)
    payload["multipart"] = multipart_group
    search_context = _review_search_context(task)
    active_job = search_context["active_search_job"]
    payload["searches"] = [
        metadata_search.serialize_search(search) for search in search_context["searches"]
    ]
    payload["search_in_progress"] = active_job is not None
    payload["active_search_job"] = (
        {"job_id": str(active_job.job_id), "state": active_job.state}
        if active_job is not None
        else None
    )
    payload["failed_search_error"] = search_context["failed_search_error"]
    scanner_opf_review = scanner_opf.review_context(task)
    payload["scanner_opf"] = (
        {
            "kind": scanner_opf_review["kind"],
            "rows": scanner_opf_review.get("rows", []),
            "preselected": scanner_opf_review.get("preselected"),
            "reason": scanner_opf_review.get("reason"),
            # Send back with action=resolve; a review whose evidence changed is rejected.
            "revision": scanner_opf_review.get("revision"),
            "open": scanner_opf_review.get("open"),
            "obsolete": scanner_opf_review.get("obsolete"),
            "possible_duplicate_book_ids": [
                book.pk for book in scanner_opf_review.get("duplicates", [])
            ],
        }
        if scanner_opf_review is not None
        else None
    )
    isbn_lookup = search_context["isbn_lookup"]
    payload["isbn_lookup"] = (
        {
            "isbn": isbn_lookup["isbn"],
            "state": isbn_lookup["state"],
            "verified_candidate_ids": [str(pk) for pk in isbn_lookup["verified_candidate_ids"]],
            "unambiguous": isbn_lookup["unambiguous"],
            "best_candidate_id": (
                str(isbn_lookup["best"].id) if isbn_lookup["best"] is not None else None
            ),
            "auto_accepted": (
                task.state == ReviewTask.State.RESOLVED
                and isinstance(task.decision_payload, dict)
                and task.decision_payload.get("resolution_source") == "reviewer_isbn"
            ),
        }
        if isbn_lookup is not None
        else None
    )
    if _wants_html(request):
        return render(
            request,
            "core/review_detail.html",
            {
                "task": task,
                "payload": payload,
                "auto_accept_block": auto_accept_block,
                "local_suggestion": local_suggestion,
                "multipart": multipart_group,
                "candidates": candidates,
                "all_genres": list(Genre.objects.order_by("name")),
                "books": Book.objects.order_by("title")[:250],
                "llm_panel": llm_panel,
                "llm_candidate_ids": _llm_candidate_ids(task),
                "scanner_opf_review": scanner_opf_review,
                **search_context,
            },
        )
    return JsonResponse(payload)


def _llm_candidate_ids(task: ReviewTask) -> set[int]:
    """Candidates found only through LLM-proposed search terms (labelled on the page)."""
    if task.import_job_id is None:
        return set()
    return {
        candidate.pk
        for candidate in MetadataCandidate.objects.filter(import_job_id=task.import_job_id)
        if isinstance(candidate.raw_payload, dict)
        and candidate.raw_payload.get(llm_identification.HINT_SOURCE_KEY)
        == llm_identification.HINT_SOURCE_LLM
    }


def _llm_suggestion_payload(suggestion: LLMSuggestion) -> dict[str, Any]:
    output = suggestion.output if isinstance(suggestion.output, dict) else {}
    grounding = suggestion.grounding if isinstance(suggestion.grounding, dict) else {}
    return {
        "id": suggestion.pk,
        "label": "AI suggestion, unverified",
        "trigger": suggestion.trigger,
        "outcome": suggestion.outcome,
        "model": suggestion.model,
        "model_version": suggestion.model_version,
        "prompt_template_version": suggestion.prompt_template_version,
        "created_at": suggestion.created_at.isoformat(),
        "new_candidate_count": suggestion.new_candidate_count,
        "output": output,
        "grounding": grounding,
    }


def _llm_panel(task: ReviewTask) -> dict[str, Any] | None:
    """The "AI suggestion (unverified)" panel for an import review task, or None."""
    if task.import_job_id is None:
        return None
    suggestion = llm_identification.latest_suggestion(task)
    enabled = llm.llm_enabled()
    if suggestion is None and not enabled:
        return None
    grounding = (
        suggestion.grounding if suggestion and isinstance(suggestion.grounding, dict) else {}
    )
    output = suggestion.output if suggestion and isinstance(suggestion.output, dict) else {}
    active = llm_identification.active_job(task)
    return {
        "enabled": enabled,
        "suggestion": suggestion,
        "suggestion_payload": _llm_suggestion_payload(suggestion) if suggestion else None,
        "output": output,
        "title_hints": grounding.get("title_hints", []),
        "author_hints": grounding.get("author_hints", []),
        "isbn_hints_kept": grounding.get("isbn_hints_kept", []),
        "isbn_hints_dropped": grounding.get("isbn_hints_dropped", []),
        "first_title": (output.get("title_hints") or [""])[0],
        "authors_text": ", ".join(output.get("author_hints") or []),
        "active_job": active,
    }


_book_for_candidate = review_accept.book_for_candidate


def _accept_review_candidate(
    *,
    task: ReviewTask,
    candidate: MetadataCandidate,
    reviewer: Any,
    decision_notes: str = "",
    decision_payload: dict[str, Any] | None = None,
    reviewer_authors: list[str] | None = None,
    reviewer_genres: list[Genre] | None = None,
    inline_cover_fallback: bool = True,
) -> tuple[Book, FileVariant | None]:
    """Resolve ``task`` with ``candidate``; shared by single and batch accept.

    ``reviewer_authors`` only fill a Book that has no authors yet (provider authors
    always win). ``reviewer_genres`` is an explicit reviewer choice; ``None`` assigns
    genres automatically from provider/embedded subjects. Front-cover extraction is
    a background job queued after commit; batch accept passes
    ``inline_cover_fallback=False`` so it never extracts in the request, even without
    a configured queue.
    """
    return review_accept.accept_candidate(
        task=task,
        candidate=candidate,
        reviewer=reviewer,
        reviewer_authors=reviewer_authors,
        reviewer_genres=reviewer_genres,
        decision_notes=decision_notes,
        extra_payload=decision_payload,
        inline_cover_fallback=inline_cover_fallback,
    )


@require_POST
@login_required
def review_accept_candidate(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")

    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    blocker = multipart.confirmation_blocker(task)
    if blocker is not None:
        return HttpResponseBadRequest(blocker)

    payload = _request_payload(request)
    candidate_id = _coerce_int(payload.get("candidate_id"), minimum=1)
    if candidate_id is None:
        return HttpResponseBadRequest("candidate_id required")

    try:
        candidate = MetadataCandidate.objects.get(pk=candidate_id)
    except MetadataCandidate.DoesNotExist:
        return HttpResponseBadRequest("candidate not found")
    if candidate.import_job_id != task.import_job_id or candidate.scan_job_id != task.scan_job_id:
        return HttpResponseBadRequest("candidate not found")
    if candidate.superseded_at is not None:
        return HttpResponseBadRequest(
            "candidate is from an earlier search; accept one of the current candidates"
        )
    try:
        reviewer_genres = _reviewer_genre_choice(request, payload)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))

    try:
        _note_review_redecided(task, profile)
        book, variant = review_accept.accept_candidate(
            task=task,
            candidate=candidate,
            reviewer=profile.user,
            reviewer_authors=_parse_author_list(payload.get("authors")),
            reviewer_genres=reviewer_genres,
            decision_notes=f"Accepted candidate {candidate.id}",
        )
        contributions.record_review_resolution(task)
        return _review_action_response(
            request,
            {
                "task_id": str(task.id),
                "source_id": str(task.import_job_id or task.scan_job_id),
                "book_id": str(book.id),
                "file_variant_id": str(variant.id) if variant is not None else None,
            },
        )
    except Exception as exc:  # pragma: no cover
        return HttpResponseBadRequest(str(exc))


def _batch_task_ids(request: HttpRequest) -> list[int] | None:
    """Ordered, de-duplicated task ids from a form (``task_ids``) or JSON body."""
    payload = _json_body(request)
    raw: Any = payload.get("task_ids") if payload else request.POST.getlist("task_ids")
    if not isinstance(raw, list):
        return None
    task_ids: list[int] = []
    for value in raw:
        task_id = _coerce_int(value, minimum=1)
        if task_id is None:
            return None
        if task_id not in task_ids:
            task_ids.append(task_id)
    return task_ids


def _batch_accept_one(
    *,
    profile: AppUserProfile,
    task_id: int,
    min_score: float,
    ambiguity_margin: float,
) -> dict[str, Any]:
    """Accept one task's top candidate if it is clearly eligible; report the outcome."""
    result: dict[str, Any] = {"task_id": str(task_id)}
    try:
        with transaction.atomic():
            try:
                task = ReviewTask.objects.select_for_update().get(pk=task_id)
            except ReviewTask.DoesNotExist:
                return {**result, "outcome": "not_found"}
            # Same rule as every single-task review action; checked before anything
            # about the task (title, state, candidates) is revealed.
            if not _can_manage_review_task(profile, task):
                return {**result, "outcome": "forbidden"}
            result["title"] = task.inferred_title or task.source_filename
            if task.state not in REVIEW_QUEUE_STATES:
                return {**result, "outcome": "closed"}
            if task.task_type == ReviewTask.TaskType.MULTIPART_WORK:
                return {
                    **result,
                    "outcome": "requires_individual_review",
                    "detail": "Confirm multipart membership and reading order on its review page.",
                }
            candidates = list(_review_candidates(task))
            eligibility = review_batch.evaluate_top_candidates(
                [candidate.score for candidate in candidates],
                min_score=min_score,
                ambiguity_margin=ambiguity_margin,
            )
            result["top_score"] = eligibility.top_score
            result["runner_up_score"] = eligibility.runner_up_score
            if not eligibility.eligible:
                return {**result, "outcome": eligibility.reason}
            candidate = candidates[0]
            book, variant = _accept_review_candidate(
                task=task,
                candidate=candidate,
                reviewer=profile.user,
                decision_notes=(
                    f"Batch-accepted top candidate {candidate.id} "
                    f"(score {eligibility.top_score:.2f})"
                ),
                decision_payload={
                    "batch_accept": True,
                    "candidate_id": candidate.id,
                    "top_score": eligibility.top_score,
                    "runner_up_score": eligibility.runner_up_score,
                    "min_score": min_score,
                    "ambiguity_margin": ambiguity_margin,
                },
                # Up to BATCH_MAX_TASKS books: covers are background jobs only.
                inline_cover_fallback=False,
            )
            contributions.record_review_resolution(task)
    except Exception as exc:  # one failing task must not abort the rest of the batch
        logger.warning(
            "review_batch_accept_task_failed",
            review_task_id=task_id,
            error_type=type(exc).__name__,
        )
        return {**result, "outcome": "error", "detail": str(exc)}
    return {
        **result,
        "outcome": "accepted",
        "candidate_id": str(candidate.id),
        "book_id": str(book.id),
        "file_variant_id": str(variant.id) if variant is not None else None,
    }


@require_POST
@login_required
def review_batch_accept(request: HttpRequest) -> HttpResponseBase:
    """Accept the top candidate of each selected task that is clearly unambiguous.

    Every task is checked individually: permissions, open state, a top score at or
    above the batch minimum, and a runner-up outside the ambiguity margin. Anything
    else is reported and left in the queue for individual review.
    """
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()

    task_ids = _batch_task_ids(request)
    if not task_ids:
        return HttpResponseBadRequest("task_ids required")
    max_tasks = review_batch.batch_max_tasks()
    if len(task_ids) > max_tasks:
        return HttpResponseBadRequest(f"at most {max_tasks} tasks per batch")

    payload = _request_payload(request)
    raw_min_score = payload.get("min_score")
    min_score = review_batch.effective_min_score(
        _parse_score(str(raw_min_score)) if raw_min_score is not None else None,
    )
    margin = review_batch.batch_ambiguity_margin()

    results = [
        _batch_accept_one(
            profile=profile,
            task_id=task_id,
            min_score=min_score,
            ambiguity_margin=margin,
        )
        for task_id in task_ids
    ]
    outcomes: dict[str, int] = {}
    for item in results:
        outcomes[item["outcome"]] = outcomes.get(item["outcome"], 0) + 1
    logger.info(
        "review_batch_accept",
        reviewer_id=profile.user_id,
        requested=len(task_ids),
        min_score=min_score,
        ambiguity_margin=margin,
        **{f"outcome_{key}": value for key, value in sorted(outcomes.items())},
    )
    response_payload = {
        "accepted": outcomes.get("accepted", 0),
        "skipped": len(results) - outcomes.get("accepted", 0),
        "min_score": min_score,
        "ambiguity_margin": margin,
        "results": results,
    }
    if _wants_html(request):
        return render(request, "core/review_batch_result.html", response_payload)
    return JsonResponse(response_payload)


@require_POST
@login_required
def review_mark_unidentified(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    payload = _request_payload(request)
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    _note_review_redecided(task, profile)
    task.state = ReviewTask.State.RESOLVED
    task.decision = ReviewTask.Decision.MARK_UNIDENTIFIED
    task.reviewer = profile.user
    task.decision_notes = str(payload.get("notes", ""))
    task.decision_payload = {"mark": "unidentified"}
    task.decided_at = timezone.now()
    task.save(
        update_fields=[
            "state",
            "decision",
            "decision_notes",
            "decision_payload",
            "reviewer_id",
            "decided_at",
            "updated_at",
        ],
    )
    contributions.record_review_resolution(task)
    return _review_action_response(request, {"task_id": str(task.id), "state": task.state})


def _save_review_terms(task: ReviewTask, terms: dict[str, Any]) -> None:
    """Store reviewer terms on the task and, for scans, as the scan's hints.

    The task's terms are the reviewer's saved search terms; later automatic scan
    identifications use the scan job's hints, so both are kept in step.
    """
    metadata_search.apply_terms(task, terms)
    scan_job = task.scan_job if task.scan_job_id is not None else None
    if scan_job is not None:
        scan_job.provisional_title = task.inferred_title
        scan_job.provisional_authors = task.inferred_authors
        scan_job.save(update_fields=["provisional_title", "provisional_authors", "updated_at"])


def _closed_or_forbidden_review(
    request: HttpRequest,
    task_id: str,
) -> tuple[AppUserProfile, ReviewTask] | HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
        return HttpResponseBadRequest("review task is already closed")
    return profile, task


def _queue_review_search(
    request: HttpRequest,
    *,
    profile: AppUserProfile,
    task: ReviewTask,
    terms: dict[str, Any],
    decision_payload: dict[str, Any],
    notes: str,
    busy_prefix: str,
) -> HttpResponseBase:
    """Claim the task and queue a provider search with ``terms`` (text or ISBN)."""
    task.state = ReviewTask.State.CLAIMED
    task.decision = ReviewTask.Decision.SEARCH_AGAIN
    task.reviewer = profile.user
    task.decision_notes = notes
    task.decided_at = timezone.now()
    task.decision_payload = decision_payload
    task.save(
        update_fields=[
            "state",
            "decision",
            "decision_notes",
            "decision_payload",
            "inferred_title",
            "inferred_authors",
            "reviewer_id",
            "decided_at",
            "updated_at",
        ],
    )
    try:
        if task.scan_job_id is not None:
            scan_job = task.scan_job
            if scan_job is None:
                raise scan_identification.ScanIdentificationError("scan job is unavailable")
            processing_job = scan_identification.request_scan_identification(
                scan_job=scan_job,
                requested_by_user_id=profile.user_id,
                terms=terms,
            )
            job_payload = scan_identification.job_payload(processing_job)
        else:
            processing_job = review_identification.request_review_identification(
                review_task=task,
                requested_by_user_id=profile.user_id,
                terms=terms,
            )
            job_payload = review_identification.job_payload(processing_job)
    except metadata_search.SearchInProgressError as exc:
        task.state = ReviewTask.State.OPEN
        task.save(update_fields=["state", "updated_at"])
        if _wants_html(request):
            messages.warning(request, f"{busy_prefix} {exc}.")
            return redirect("review-detail", task_id=task.id)
        return JsonResponse({"task_id": str(task.id), "error": str(exc)}, status=409)
    except (
        review_identification.ReviewIdentificationError,
        scan_identification.ScanIdentificationError,
    ) as exc:
        task.state = ReviewTask.State.OPEN
        task.save(update_fields=["state", "updated_at"])
        if _wants_html(request):
            messages.error(request, f"The search could not be started: {exc}")
            return redirect("review-detail", task_id=task.id)
        return HttpResponseBadRequest(str(exc))

    if _wants_html(request):
        # The review page shows the search while it runs and its outcome afterwards.
        return redirect("review-detail", task_id=task.id)
    return JsonResponse(
        {
            "task_id": str(task.id),
            "state": task.state,
            "identification_job": job_payload,
        },
        status=202,
    )


@require_POST
@login_required
def review_ask_llm(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    """Queue the local-LLM identification assist for one import review task (#26)."""
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if not llm.llm_enabled():
        return HttpResponseBadRequest("the AI identification assist is disabled")
    try:
        processing_job = llm_identification.request_llm_identification(
            review_task=task,
            trigger=LLMSuggestion.Trigger.REVIEWER,
            requested_by_user_id=profile.user_id,
        )
    except llm_identification.LLMIdentificationError as exc:
        return HttpResponseBadRequest(str(exc))
    if _wants_html(request):
        if processing_job.state in {
            ProcessingJob.JobState.SUCCESS,
            ProcessingJob.JobState.FAILED,
        }:
            return redirect("review-detail", task_id=task.id)
        return redirect("llm-identification-job-status", job_id=processing_job.job_id)
    return JsonResponse(
        {
            "task_id": str(task.id),
            "llm_job": llm_identification.job_payload(processing_job),
        },
        status=202,
    )


@require_GET
@login_required
def llm_identification_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.LLM_IDENTIFICATION:
        return HttpResponseNotFound()
    payload = llm_identification.job_payload(job)
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(
            pk=payload.get("review_task_id"),
        )
    except (ReviewTask.DoesNotExist, ValueError, TypeError):
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if _wants_html(request):
        if job.state in {ProcessingJob.JobState.SUCCESS, ProcessingJob.JobState.FAILED}:
            return redirect("review-detail", task_id=task.id)
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "AI identification assist"},
        )
    return JsonResponse(payload)


@require_POST
@login_required
def review_search_again(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    """Save the posted search terms (if any) and queue a provider search with them."""
    loaded = _closed_or_forbidden_review(request, task_id)
    if isinstance(loaded, HttpResponseBase):
        return loaded
    profile, task = loaded
    payload = _request_payload(request)
    posted_terms = metadata_search.terms_from_payload(payload)
    if posted_terms is not None:
        _save_review_terms(task, posted_terms)
    terms = metadata_search.task_terms(task)
    return _queue_review_search(
        request,
        profile=profile,
        task=task,
        terms=terms,
        decision_payload={"search_again": True, "terms": terms},
        notes=str(payload.get("notes", "")),
        busy_prefix="Search terms saved, but",
    )


@require_POST
@login_required
def review_isbn_lookup(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    """Queue a provider lookup by the ISBN the reviewer typed (#59).

    The typed ISBN is only a query: a record counts as verified when a provider
    returns it carrying that ISBN. The task's saved title/author terms are kept.
    """
    loaded = _closed_or_forbidden_review(request, task_id)
    if isinstance(loaded, HttpResponseBase):
        return loaded
    profile, task = loaded
    payload = _request_payload(request)
    isbn = metadata_search.parse_isbn(payload.get("isbn"))
    if isbn is None:
        message = "enter one valid ISBN-10 or ISBN-13 (the check digit did not match)"
        if _wants_html(request):
            messages.error(request, f"ISBN lookup not started: {message}.")
            return redirect("review-detail", task_id=task.id)
        return HttpResponseBadRequest(message)
    return _queue_review_search(
        request,
        profile=profile,
        task=task,
        terms=metadata_search.isbn_terms(isbn),
        decision_payload={"isbn_lookup": isbn},
        notes=str(payload.get("notes", "")),
        busy_prefix="ISBN lookup not started:",
    )


@require_GET
@login_required
def review_identification_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.REVIEW_IDENTIFICATION:
        return HttpResponseNotFound()
    payload = review_identification.job_payload(job)
    task_id = payload.get("review_task_id")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except (ReviewTask.DoesNotExist, ValueError, TypeError):
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    if _wants_html(request):
        if job.state == ProcessingJob.JobState.SUCCESS:
            return redirect("review-detail", task_id=task.id)
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Metadata search"},
        )
    return JsonResponse(payload)


@require_POST
@login_required
def review_edit_search_terms(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    payload = _request_payload(request)
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    posted_terms = metadata_search.terms_from_payload(payload)
    if posted_terms is not None:
        _save_review_terms(task, posted_terms)

    contributions.record_review_reversal(
        task,
        source=ReviewDecisionCorrection.Source.REOPENED,
        found_by=profile.user,
        note="search terms edited on a resolved review",
    )
    task.state = ReviewTask.State.OPEN
    task.decision = ReviewTask.Decision.EDIT_SEARCH_TERMS
    task.reviewer = profile.user
    task.decision_notes = str(payload.get("notes", ""))
    task.decided_at = timezone.now()
    task.save(
        update_fields=[
            "state",
            "decision",
            "decision_notes",
            "inferred_title",
            "inferred_authors",
            "reviewer_id",
            "decided_at",
            "updated_at",
        ],
    )
    return _review_action_response(
        request,
        {"task_id": str(task.id), "state": task.state},
        task_id=task.id,
    )


@require_POST
@login_required
def review_manual_entry(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    blocker = multipart.confirmation_blocker(task)
    if blocker is not None:
        return HttpResponseBadRequest(blocker)

    payload = _request_payload(request)
    if not isinstance(payload.get("title"), str):
        return HttpResponseBadRequest("title required")
    try:
        reviewer_genres = _reviewer_genre_choice(request, payload)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    book = _build_manual_book_from_payload(payload)
    # A manually entered book has no provider record, so only the file's own tags apply.
    _apply_review_genres(
        book=book,
        task=task,
        reviewer_genres=reviewer_genres,
        provider_subjects=None,
        reviewer=request.user,
    )

    _note_review_redecided(task, profile)
    variant = _resolve_review_source(
        task=task,
        book=book,
        reviewer=profile.user,
        decision=cast(str, ReviewTask.Decision.MANUAL_ENTRY),
        decision_payload=payload,
    )
    contributions.record_review_resolution(task)
    return _review_action_response(
        request,
        {
            "task_id": str(task.id),
            "source_id": str(task.import_job_id or task.scan_job_id),
            "book_id": str(book.id),
            "variant_id": str(variant.id) if variant is not None else None,
        },
    )


@require_POST
@login_required
def review_scanner_opf(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Scanner-OPF review actions (#130).

    ``action=resolve`` with ``choice`` decides a metadata conflict (provider record vs
    scanner OPF: ``scanner``/``provider``; hand-edited field vs OPF:
    ``current``/``scanner``). ``action=create_book`` creates a Book from the scanner's
    OPF for a scan that finish handed to identification (ambiguous ISBN, likely
    duplicate).
    """
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()
    try:
        task = ReviewTask.objects.select_related("scan_job").get(pk=task_id)
    except (ReviewTask.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    payload = _request_payload(request)
    action = str(payload.get("action") or "")
    try:
        if action == "resolve":
            revision = str(payload.get("revision") or "")
            if not revision:
                return HttpResponseBadRequest(
                    "revision is required: send the scanner_opf.revision of the review you "
                    "decided on"
                )
            book = scanner_opf.resolve_conflict(
                task,
                choice=str(payload.get("choice") or ""),
                user=profile.user,
                revision=revision,
            )
        elif action == "create_book":
            book = scanner_opf.create_book_from_review(task, user=profile.user)
            contributions.record_review_resolution(task)
        else:
            return HttpResponseBadRequest("action must be resolve or create_book")
    except scanner_opf.StaleReview as exc:
        # The decision was made on evidence that no longer applies: reject it and show
        # the refreshed review (form error on the page, 409 for API clients).
        if _wants_html(request):
            messages.error(request, str(exc))
            return redirect("review-detail", task_id=task.id)
        task.refresh_from_db()
        context = scanner_opf.review_context(task)
        return JsonResponse(
            {
                "task_id": str(task.id),
                "error": str(exc),
                "state": task.state,
                "revision": context["revision"] if context is not None else None,
            },
            status=409,
        )
    except scanner_opf.ScannerOpfError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {"task_id": str(task.id), "book_id": str(book.pk), "state": ReviewTask.State.RESOLVED},
        task_id=task.id,
    )


@require_POST
@login_required
def review_merge_existing(
    request: HttpRequest,
    task_id: str,
) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "scan_job").get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()

    payload = _request_payload(request)
    raw_book_id = payload.get("book_id")
    if isinstance(raw_book_id, int):
        book_id = raw_book_id
    elif isinstance(raw_book_id, str) and raw_book_id.isdigit():
        book_id = int(raw_book_id)
    else:
        return HttpResponseBadRequest("book_id required")
    try:
        book = Book.objects.get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseBadRequest("book not found")
    blocker = multipart.confirmation_blocker(task)
    if blocker is not None:
        return HttpResponseBadRequest(blocker)

    _note_review_redecided(task, profile)
    _resolve_review_source(
        task=task,
        book=book,
        reviewer=profile.user,
        decision=cast(str, ReviewTask.Decision.MERGED_BOOK),
        decision_payload={"book_id": book_id},
    )
    contributions.record_review_resolution(task)
    return _review_action_response(
        request,
        {"task_id": str(task.id), "book_id": str(book.id)},
    )


def _multipart_review_task(
    request: HttpRequest,
    task_id: str,
) -> tuple[ReviewTask | None, AppUserProfile | None, HttpResponseBase | None]:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return None, None, HttpResponseBadRequest("forbidden")
    try:
        task = ReviewTask.objects.select_related("import_job", "import_job__import_group").get(
            pk=task_id
        )
    except (ReviewTask.DoesNotExist, ValueError):
        return None, None, HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return None, None, HttpResponseForbidden()
    if task.task_type != ReviewTask.TaskType.MULTIPART_WORK:
        return None, None, HttpResponseBadRequest("not a multi-part review")
    if task.state in {ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED}:
        return None, None, HttpResponseBadRequest("review task is already closed")
    return task, profile, None


@require_POST
@login_required
def review_multipart_order(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Save the reviewer's part order/labels before confirming a multi-part work.

    Form posts send ``part_index_<import_job_id>`` / ``part_label_<import_job_id>``;
    JSON posts send ``{"parts": [{"import_job_id", "part_index", "part_label"}]}``.
    """
    task, _profile, error = _multipart_review_task(request, task_id)
    if error is not None or task is None:
        return error or HttpResponseBadRequest("invalid request")
    payload = _request_payload(request)
    order: dict[str, tuple[int, str]] = {}
    raw_parts = payload.get("parts")
    if isinstance(raw_parts, list):
        for entry in raw_parts:
            if not isinstance(entry, dict):
                return HttpResponseBadRequest("invalid parts")
            index = _coerce_int(entry.get("part_index"), minimum=1)
            if index is None:
                return HttpResponseBadRequest("part_index must be a positive number")
            order[str(entry.get("import_job_id"))] = (index, str(entry.get("part_label") or ""))
    else:
        for key, value in payload.items():
            if not key.startswith("part_index_"):
                continue
            job_id = key.removeprefix("part_index_")
            index = _coerce_int(value, minimum=1)
            if index is None:
                return HttpResponseBadRequest("part_index must be a positive number")
            order[job_id] = (index, str(payload.get(f"part_label_{job_id}", "") or ""))
    if not order:
        return HttpResponseBadRequest("parts required")
    try:
        multipart.update_part_order(task, order)
    except multipart.MultipartError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {"task_id": str(task.id), "multipart": multipart.group_for_task(task)},
        task_id=task.id,
    )


@require_POST
@login_required
def review_multipart_acknowledge_incomplete(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Acknowledge the parts that block confirming a multi-part work.

    Missing (failed/absent) parts are left out; parts identical to another Book's file
    are included as parts of this work.
    """
    task, profile, error = _multipart_review_task(request, task_id)
    if error is not None or task is None or profile is None:
        return error or HttpResponseBadRequest("invalid request")
    try:
        absent, duplicates = multipart.acknowledge_incomplete(
            review_task=task, reviewer=profile.user
        )
    except multipart.MultipartError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {
            "task_id": str(task.id),
            "acknowledged_missing_parts": absent,
            "acknowledged_duplicate_parts": duplicates,
        },
        task_id=task.id,
    )


@require_POST
@login_required
def review_multipart_reject(request: HttpRequest, task_id: str) -> HttpResponseBase:
    """Not one work: close the group and identify every file as its own import."""
    task, profile, error = _multipart_review_task(request, task_id)
    if error is not None or task is None or profile is None:
        return error or HttpResponseBadRequest("invalid request")
    payload = _request_payload(request)
    try:
        split = multipart.reject_grouping(
            review_task=task,
            reviewer=profile.user,
            notes=str(payload.get("notes", "")),
        )
    except multipart.MultipartError as exc:
        return HttpResponseBadRequest(str(exc))
    return _review_action_response(
        request,
        {
            "task_id": str(task.id),
            "state": ReviewTask.State.RESOLVED,
            "split_import_job_ids": [str(job.id) for job in split],
        },
    )
