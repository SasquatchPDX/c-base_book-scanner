"""LLM identification assist jobs: hints in front of the providers (#26).

Flow for one open import review task:

1. Build a bounded evidence bundle from data the pipeline already has.
2. Ask the local LLM (``core.llm``) for search hints; validate and ground them.
3. Store an ``LLMSuggestion`` (structured output and the bundle's hash, no book text).
4. Search the existing providers with the hints, score with the existing scorer, and
   add the new candidates to the open review task, tagged ``_hint_source="llm"``.

LLM-derived candidates never auto-accept (``intake.BLOCK_LLM_DERIVED``): the task stays
open with better candidates for a reviewer. The job runs on its own RQ queue.
"""

from __future__ import annotations

import html
import re
from datetime import timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import structlog
from django.db import transaction
from django.utils import timezone

from ebook_server.jobs import get_queue, is_stale_running, llm_job_timeout_seconds, llm_queue_name

from . import identification, intake, llm, review_reader
from .models import ImportJob, LLMSuggestion, MetadataCandidate, ProcessingJob, ReviewTask
from .storage_registry import backend_for_location

logger = structlog.get_logger(__name__)

HINT_SOURCE_KEY = "_hint_source"
HINT_SOURCE_LLM = "llm"
EVIDENCE_KEY = "llm"
SOURCE_OPT_OUT_KEY = "llm_assist"
AUTO_TRIGGERS = (
    LLMSuggestion.Trigger.AUTO_ZERO_CANDIDATES,
    LLMSuggestion.Trigger.AUTO_LOW_SCORE,
)
_ACTIVE_STATES = (ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING)
_CLOSED_TASK_STATES = (ReviewTask.State.RESOLVED, ReviewTask.State.CANCELLED)


class LLMIdentificationError(RuntimeError):
    """An LLM identification request cannot be scheduled."""


def _details(job: ProcessingJob) -> dict[str, Any]:
    return dict(job.details) if isinstance(job.details, dict) else {}


def is_llm_derived(candidate: identification.IdentificationCandidate) -> bool:
    return isinstance(candidate.raw, dict) and candidate.raw.get(HINT_SOURCE_KEY) == (
        HINT_SOURCE_LLM
    )


# --- trigger policy ----------------------------------------------------------------------


def _source_key(job: ImportJob) -> str:
    return f"source:{job.source_location_id}" if job.source_location_id else "upload"


def source_allows_llm(job: ImportJob) -> bool:
    """Per-source opt-out: ``SourceLocation.default_metadata["llm_assist"] = false``."""
    source = job.source_location
    if source is None:
        return True
    metadata = source.default_metadata if isinstance(source.default_metadata, dict) else {}
    return metadata.get(SOURCE_OPT_OUT_KEY) is not False


def daily_cap() -> int:
    from django.conf import settings

    return int(getattr(settings, "BOOK_SCANNER_LLM_DAILY_CAP", 0) or 0)


def daily_cap_reached(job: ImportJob) -> bool:
    """Automatic requests for this job's source in the last 24 hours reached the cap."""
    cap = daily_cap()
    if cap <= 0:
        return True
    since = timezone.now() - timedelta(hours=24)
    used = ProcessingJob.objects.filter(
        job_type=ProcessingJob.JobType.LLM_IDENTIFICATION,
        scheduled_at__gte=since,
        details__source_key=_source_key(job),
        details__trigger__in=[str(trigger) for trigger in AUTO_TRIGGERS],
    ).count()
    return used >= cap


def auto_trigger_reason(
    job: ImportJob,
    candidates: list[identification.IdentificationCandidate],
) -> str | None:
    """The automatic trigger for an import routed to review, or None.

    Fallback only: zero candidates, or a top score below the review threshold
    (``BOOK_SCANNER_IDENTIFICATION_REVIEW``). Never for imports with good signals, a
    source that opted out, or once the source's daily cap is used up.
    """
    if not llm.llm_enabled():
        return None
    if not candidates:
        reason = str(LLMSuggestion.Trigger.AUTO_ZERO_CANDIDATES)
    elif candidates[0].score < intake.REVIEW_SCORE:
        reason = str(LLMSuggestion.Trigger.AUTO_LOW_SCORE)
    else:
        return None
    if not source_allows_llm(job):
        return None
    if daily_cap_reached(job):
        logger.info("llm_identification_daily_cap_reached", source_key=_source_key(job))
        return None
    return reason


def maybe_request_after_intake(
    job: ImportJob,
    candidates: list[identification.IdentificationCandidate],
) -> ProcessingJob | None:
    """Queue the LLM assist for an import that just went to review, when the policy says so.

    Never raises: the import is already safely archived and in review.
    """
    try:
        reason = auto_trigger_reason(job, candidates)
        if reason is None:
            return None
        task = (
            ReviewTask.objects.filter(
                import_job=job,
                state__in=(ReviewTask.State.OPEN, ReviewTask.State.CLAIMED),
            )
            .order_by("-created_at")
            .first()
        )
        if task is None:
            return None
        return request_llm_identification(
            review_task=task,
            trigger=reason,
            requested_by_user_id=None,
        )
    except Exception:
        logger.exception("llm_identification_auto_request_failed", import_job_id=str(job.id))
        return None


# --- request and dispatch ----------------------------------------------------------------


def job_payload(job: ProcessingJob) -> dict[str, Any]:
    details = _details(job)
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "review_task_id": details.get("review_task_id"),
        "import_job_id": details.get("import_job_id"),
        "trigger": details.get("trigger"),
        "outcome": details.get("outcome"),
        "suggestion_id": details.get("suggestion_id"),
        "new_candidate_count": details.get("new_candidate_count", 0),
        "error": details.get("error", ""),
        "scheduled_at": job.scheduled_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def request_llm_identification(
    *,
    review_task: ReviewTask,
    trigger: str,
    requested_by_user_id: int | None,
) -> ProcessingJob:
    """Create (or reuse) the LLM job for an open import review task and dispatch it."""
    if not llm.llm_enabled():
        raise LLMIdentificationError("the LLM identification assist is disabled")
    if review_task.import_job_id is None:
        raise LLMIdentificationError("review task is not for an import")
    if review_task.state in _CLOSED_TASK_STATES:
        raise LLMIdentificationError("review task is already closed")
    if trigger not in LLMSuggestion.Trigger.values:
        raise LLMIdentificationError(f"unknown trigger: {trigger}")

    with transaction.atomic():
        # Lock only the review task row: ``import_job`` is nullable, so the join is an
        # outer join and PostgreSQL refuses FOR UPDATE on its nullable side.
        task = (
            ReviewTask.objects.select_for_update(of=("self",))
            .select_related("import_job")
            .get(pk=review_task.pk)
        )
        for active in ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.LLM_IDENTIFICATION,
            state__in=_ACTIVE_STATES,
            details__review_task_id=task.id,
        ).order_by("-scheduled_at"):
            if not is_stale_running(active):
                return active
        import_job = task.import_job
        assert import_job is not None
        processing_job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.LLM_IDENTIFICATION,
            details={
                "review_task_id": task.id,
                "import_job_id": str(import_job.id),
                "trigger": trigger,
                "source_key": _source_key(import_job),
                "requested_by_user_id": requested_by_user_id,
                "new_candidate_count": 0,
                "error": "",
            },
        )

    job_id = str(processing_job.job_id)
    try:
        queue = get_queue(llm_queue_name())
        if queue is None:
            process_llm_identification_job(job_id)
        else:
            # One attempt only: a failed request is visible on the review page and the
            # reviewer can ask again; automatic retries would only load the AI server.
            queue.enqueue(
                process_llm_identification_job,
                job_id,
                job_timeout=llm_job_timeout_seconds(),
                retry=None,
            )
    except Exception as exc:
        processing_job.refresh_from_db()
        if processing_job.state in _ACTIVE_STATES:
            _fail(processing_job, f"queue dispatch failed: {type(exc).__name__}")
        raise LLMIdentificationError("could not queue the LLM request") from exc
    processing_job.refresh_from_db()
    return processing_job


def _fail(processing_job: ProcessingJob, error: str, **extra: Any) -> None:
    processing_job.state = ProcessingJob.JobState.FAILED
    processing_job.finished_at = timezone.now()
    processing_job.details = {**_details(processing_job), **extra, "error": error}
    processing_job.save(update_fields=["state", "finished_at", "details"])


# --- evidence ----------------------------------------------------------------------------


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"p", "br", "div", "h1", "h2", "h3", "h4", "li", "tr", "section"}:
            self.parts.append("\n")


def _html_to_text(markup: str) -> str:
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return html.unescape("".join(parser.parts))


def load_text_sample(task: ReviewTask, limit_chars: int) -> str:
    """The first ``limit_chars`` of the import's own text (TXT, HTML, EPUB, FB2).

    Reuses the review reader's bounded, sanitizing readers. Other formats (PDF, MOBI,
    scans) contribute no text in the MVP; OCR-then-LLM is a later phase.
    """
    if limit_chars <= 0:
        return ""
    sources = review_reader.reader_sources(task)
    if not sources:
        return ""
    source = sources[0]
    fmt = source.file_format.lower()
    if fmt not in {"txt", "html", "htm", "epub", "fb2"}:
        return ""
    try:
        backend = backend_for_location(source.storage_location)
        with backend.open(source.storage_identifier) as stream:
            if fmt == "txt":
                return review_reader.read_text_page(stream, 1).text[:limit_chars]
            if fmt in {"html", "htm"}:
                text = _html_to_text(review_reader.html_document(stream))
            elif fmt == "fb2":
                text = _html_to_text(review_reader.fb2_document(stream))
            else:
                chunks: list[str] = []
                parts = review_reader.epub_parts(stream)
                for part in parts[:4]:
                    stream.seek(0)
                    chunks.append(_html_to_text(review_reader.epub_part_html(stream, part.index)))
                    if sum(len(chunk) for chunk in chunks) >= limit_chars:
                        break
                text = "\n".join(chunks)
    except Exception as exc:  # unreadable sample: path and metadata still help
        logger.info("llm_text_sample_unavailable", error_type=type(exc).__name__)
        return ""
    return re.sub(r"\n\s*\n+", "\n", text).strip()[:limit_chars]


def _sibling_filenames(job: ImportJob) -> list[str]:
    """Other files of the same source folder, from import records (no filesystem walk)."""
    if job.source_location_id is None:
        return []
    folder = str(Path(job.source_path).parent)
    names: list[str] = []
    for other_path in (
        ImportJob.objects.filter(
            source_location_id=job.source_location_id,
            source_path__startswith=folder,
        )
        .exclude(pk=job.pk)
        .values_list("source_path", flat=True)[:200]
    ):
        candidate = Path(other_path)
        if str(candidate.parent) == folder and candidate.name not in names:
            names.append(candidate.name)
        if len(names) >= llm.MAX_SIBLINGS:
            break
    return sorted(names)


def build_bundle_for_task(task: ReviewTask, import_job: ImportJob) -> llm.LLMEvidenceBundle:
    evidence = import_job.evidence if isinstance(import_job.evidence, dict) else {}
    if import_job.source_location is not None:
        relative = llm.bounded_relative_path(
            import_job.source_path, import_job.source_location.root_path
        )
    else:
        # Upload staging paths say nothing about the book; use the name only.
        relative = import_job.original_filename
    failed = [
        {"title": candidate.title, "authors": candidate.authors}
        for candidate in import_job.metadata_candidates.order_by("-score")[
            : llm.MAX_FAILED_CANDIDATES
        ]
    ]
    from django.conf import settings

    sample_chars = int(
        getattr(settings, "BOOK_SCANNER_LLM_TEXT_SAMPLE_CHARS", 0) or llm.DEFAULT_TEXT_SAMPLE_CHARS
    )
    layout = evidence.get(identification.PATH_LAYOUT_EVIDENCE_KEY)
    return llm.build_evidence_bundle(
        relative_path=relative,
        filename=import_job.original_filename,
        siblings=_sibling_filenames(import_job),
        embedded_metadata=evidence,
        layout_hints=layout if isinstance(layout, dict) else {},
        text_sample=load_text_sample(task, sample_chars),
        failed_candidates=failed,
        text_sample_chars=sample_chars,
    )


def hint_identification_input(
    hints: llm.GroundedHints,
    base: identification.IdentificationInput,
) -> identification.IdentificationInput:
    """LLM hints first (they drive the provider queries), the file's own signals after.

    The scorer compares candidates with this merged input, so the file's own evidence
    still counts; LLM-derived candidates are blocked from auto-accept regardless.
    """

    def merged(first: list[str], second: list[str]) -> list[str]:
        return list(dict.fromkeys(value for value in [*first, *second] if value))

    return identification.IdentificationInput(
        title_hints=merged(hints.title_hints, base.title_hints),
        author_hints=merged(hints.author_hints, base.author_hints),
        isbns=merged(hints.isbns, base.isbns),
        folder_hints=base.folder_hints,
        language=hints.language or base.language,
        publisher=base.publisher,
        description=base.description,
    )


# --- job ---------------------------------------------------------------------------------


def _save_llm_candidates(
    import_job: ImportJob,
    candidates: list[identification.IdentificationCandidate],
) -> list[MetadataCandidate]:
    """Add candidates not already on the import (by provider and ref), always in REVIEW."""
    existing = set(import_job.metadata_candidates.values_list("provider_id", "provider_ref"))
    created: list[MetadataCandidate] = []
    for candidate in candidates:
        provider_ref = intake._fit(MetadataCandidate, "provider_ref", candidate.provider_ref)
        key = (candidate.provider_id, provider_ref)
        if key in existing:
            continue
        existing.add(key)
        created.append(
            MetadataCandidate.objects.create(
                import_job=import_job,
                provider_id=candidate.provider_id,
                provider_ref=provider_ref,
                title=intake._fit(MetadataCandidate, "title", candidate.title),
                authors=candidate.authors,
                isbn10=intake._fit(MetadataCandidate, "isbn10", candidate.isbn10),
                isbn13=intake._fit(MetadataCandidate, "isbn13", candidate.isbn13),
                score=Decimal(str(candidate.score)),
                raw_payload=candidate.raw,
                subjects=candidate.subjects,
                language=intake._fit(MetadataCandidate, "language", candidate.language),
                publisher=intake._fit(MetadataCandidate, "publisher", candidate.publisher),
                description=candidate.description or "",
                publication_year=candidate.publication_year,
                state=MetadataCandidate.CandidateState.REVIEW,
            ),
        )
    return created


def _record_on_task(
    task: ReviewTask,
    import_job: ImportJob,
    suggestion: LLMSuggestion,
    new_candidates: list[identification.IdentificationCandidate],
    decision: intake.AutoAcceptDecision,
) -> None:
    pointer = {
        "suggestion_id": suggestion.pk,
        "outcome": suggestion.outcome,
        "new_candidate_count": len(new_candidates),
    }
    evidence = dict(import_job.evidence) if isinstance(import_job.evidence, dict) else {}
    evidence[EVIDENCE_KEY] = pointer
    embedded = dict(task.embedded_metadata) if isinstance(task.embedded_metadata, dict) else {}
    embedded[EVIDENCE_KEY] = pointer
    if decision.blocked_reason is not None and not evidence.get(intake.AUTO_ACCEPT_BLOCKED_KEY):
        for target in (evidence, embedded):
            target[intake.AUTO_ACCEPT_BLOCKED_KEY] = decision.blocked_reason
            target[intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY] = decision.details
    import_job.evidence = evidence
    import_job.save(update_fields=["evidence", "updated_at"])
    snapshot = task.provider_snapshot if isinstance(task.provider_snapshot, list) else []
    snapshot = [
        *snapshot,
        *(
            {
                "provider_id": candidate.provider_id,
                "provider_ref": candidate.provider_ref,
                "score": float(candidate.score),
                HINT_SOURCE_KEY: HINT_SOURCE_LLM,
            }
            for candidate in new_candidates
        ),
    ]
    payload = task.decision_payload if isinstance(task.decision_payload, dict) else {}
    task.embedded_metadata = embedded
    task.provider_snapshot = snapshot
    task.decision_payload = {
        **payload,
        "candidate_count": import_job.metadata_candidates.count(),
        "llm_suggestion_id": suggestion.pk,
    }
    task.save(
        update_fields=["embedded_metadata", "provider_snapshot", "decision_payload", "updated_at"],
    )


def _claim(job_id: str) -> tuple[ProcessingJob, dict[str, Any]] | None:
    with transaction.atomic():
        try:
            processing_job = ProcessingJob.objects.select_for_update().get(job_id=job_id)
        except ProcessingJob.DoesNotExist:
            return None
        if processing_job.job_type != ProcessingJob.JobType.LLM_IDENTIFICATION:
            return None
        if processing_job.state in {
            ProcessingJob.JobState.SUCCESS,
            ProcessingJob.JobState.FAILED,
        }:
            return None
        if processing_job.state == ProcessingJob.JobState.RUNNING and not is_stale_running(
            processing_job,
        ):
            return None
        processing_job.state = ProcessingJob.JobState.RUNNING
        processing_job.started_at = timezone.now()
        processing_job.finished_at = None
        processing_job.save(update_fields=["state", "started_at", "finished_at"])
    return processing_job, _details(processing_job)


def _suggestion(
    *,
    processing_job: ProcessingJob,
    task: ReviewTask,
    import_job: ImportJob,
    details: dict[str, Any],
    outcome: str,
    bundle: llm.LLMEvidenceBundle | None,
    completion: llm.LLMCompletion | None = None,
    grounding: dict[str, Any] | None = None,
    new_candidate_count: int = 0,
    model: str = "",
) -> LLMSuggestion:
    requested_by = details.get("requested_by_user_id")
    return LLMSuggestion.objects.create(
        import_job=import_job,
        review_task=task,
        processing_job=processing_job,
        trigger=str(details.get("trigger") or LLMSuggestion.Trigger.REVIEWER),
        requested_by_id=requested_by if isinstance(requested_by, int) else None,
        model=(completion.model if completion else model)[:255],
        model_version=(completion.model_version if completion else "")[:255],
        prompt_template_version=llm.PROMPT_TEMPLATE_VERSION,
        input_sha256=bundle.sha256() if bundle is not None else "",
        output=completion.output.model_dump(mode="json") if completion else {},
        grounding=grounding or {},
        outcome=outcome,
        new_candidate_count=new_candidate_count,
        duration_seconds=completion.duration_seconds if completion else None,
    )


def process_llm_identification_job(job_id: str) -> None:
    claimed = _claim(job_id)
    if claimed is None:
        return
    processing_job, details = claimed
    task: ReviewTask | None = None
    import_job: ImportJob | None = None
    bundle: llm.LLMEvidenceBundle | None = None
    model_name = ""
    log_fields: dict[str, Any] = {
        "processing_job_id": str(processing_job.job_id),
        "trigger": details.get("trigger"),
        "prompt_template_version": llm.PROMPT_TEMPLATE_VERSION,
    }
    try:
        task = ReviewTask.objects.select_related("import_job", "import_job__source_location").get(
            pk=details["review_task_id"],
        )
        import_job = task.import_job
        if import_job is None:
            raise LLMIdentificationError("review task is not for an import")
        if task.state in _CLOSED_TASK_STATES:
            raise LLMIdentificationError("review task was closed before the LLM ran")
        bundle = build_bundle_for_task(task, import_job)
        log_fields.update(input_chars=bundle.input_chars(), input_sha256=bundle.sha256())
        client = llm.get_llm_client()
        model_name = str(getattr(client, "model", ""))
        completion = client.complete(bundle)
    except llm.LLMOutputInvalid as exc:
        _finish_failed(
            processing_job,
            task,
            import_job,
            details,
            bundle,
            outcome=LLMSuggestion.Outcome.INVALID_OUTPUT,
            error="llm_output_invalid",
            error_detail=str(exc),
            model=model_name,
            log_fields={**log_fields, "schema_valid": False},
        )
        return
    except Exception as exc:
        transient = bool(getattr(exc, "transient", False))
        _finish_failed(
            processing_job,
            task,
            import_job,
            details,
            bundle,
            outcome=LLMSuggestion.Outcome.FAILED,
            error=_safe_error(exc),
            model=model_name,
            log_fields={**log_fields, "transient": transient},
        )
        return

    assert task is not None and import_job is not None and bundle is not None
    hints = llm.ground_output(completion.output, bundle)
    log_fields.update(
        model=completion.model,
        model_version=completion.model_version,
        duration_seconds=completion.duration_seconds,
        schema_valid=True,
        grounded_hint_count=hints.grounded_count,
        ungrounded_hint_count=hints.ungrounded_count,
        dropped_isbn_count=len(hints.report.get("isbn_hints_dropped", [])),
    )
    new_candidates: list[identification.IdentificationCandidate] = []
    try:
        if hints.has_hints():
            base = identification.build_identification_input(
                source_path=import_job.source_path,
                original_filename=import_job.original_filename,
                extracted_metadata=(
                    import_job.evidence if isinstance(import_job.evidence, dict) else {}
                ),
            )
            found = identification.resolve_candidates(hint_identification_input(hints, base))
            new_candidates = [_tag(candidate) for candidate in found]
        with transaction.atomic():
            task = ReviewTask.objects.select_for_update().get(pk=task.pk)
            if task.state in _CLOSED_TASK_STATES:
                raise LLMIdentificationError("review task was closed before the LLM finished")
            saved = _save_llm_candidates(import_job, new_candidates)
            saved_keys = {(row.provider_id, row.provider_ref) for row in saved}
            added = [
                candidate
                for candidate in new_candidates
                if (
                    candidate.provider_id,
                    intake._fit(MetadataCandidate, "provider_ref", candidate.provider_ref),
                )
                in saved_keys
            ]
            decision = intake.decide_auto_accept(
                added,
                identification.evidence_author_names(
                    import_job.evidence if isinstance(import_job.evidence, dict) else {}
                ),
            )
            outcome = (
                LLMSuggestion.Outcome.HINTS_USED
                if added
                else LLMSuggestion.Outcome.NO_NEW_CANDIDATES
            )
            suggestion = _suggestion(
                processing_job=processing_job,
                task=task,
                import_job=import_job,
                details=details,
                outcome=outcome,
                bundle=bundle,
                completion=completion,
                grounding=hints.report,
                new_candidate_count=len(added),
            )
            _record_on_task(task, import_job, suggestion, added, decision)
            processing_job.state = ProcessingJob.JobState.SUCCESS
            processing_job.finished_at = timezone.now()
            processing_job.details = {
                **details,
                "outcome": str(outcome),
                "suggestion_id": suggestion.pk,
                "new_candidate_count": len(added),
                "error": "",
            }
            processing_job.save(update_fields=["state", "finished_at", "details"])
    except Exception as exc:
        _finish_failed(
            processing_job,
            task,
            import_job,
            details,
            bundle,
            outcome=LLMSuggestion.Outcome.FAILED,
            error=_safe_error(exc),
            model=completion.model,
            log_fields=log_fields,
        )
        return
    logger.info(
        "llm_identification_finished",
        **log_fields,
        outcome=str(outcome),
        new_candidate_count=len(added),
        auto_accept_blocked=decision.blocked_reason,
    )


def _safe_error(exc: BaseException) -> str:
    """Our own exception messages carry no book content; anything else is named only."""
    if isinstance(exc, (llm.LLMClientError, llm.LLMConfigurationError, LLMIdentificationError)):
        return str(exc)
    return type(exc).__name__


def _tag(
    candidate: identification.IdentificationCandidate,
) -> identification.IdentificationCandidate:
    raw = dict(candidate.raw) if isinstance(candidate.raw, dict) else {}
    raw[HINT_SOURCE_KEY] = HINT_SOURCE_LLM
    return identification.IdentificationCandidate(
        provider_id=candidate.provider_id,
        provider_ref=candidate.provider_ref,
        title=candidate.title,
        authors=candidate.authors,
        isbn10=candidate.isbn10,
        isbn13=candidate.isbn13,
        language=candidate.language,
        description=candidate.description,
        publisher=candidate.publisher,
        edition=candidate.edition,
        publication_year=candidate.publication_year,
        score=candidate.score,
        raw=raw,
        subjects=candidate.subjects,
    )


def _finish_failed(
    processing_job: ProcessingJob,
    task: ReviewTask | None,
    import_job: ImportJob | None,
    details: dict[str, Any],
    bundle: llm.LLMEvidenceBundle | None,
    *,
    outcome: str,
    error: str,
    model: str,
    log_fields: dict[str, Any],
    error_detail: str = "",
) -> None:
    suggestion_id: int | None = None
    if task is not None and import_job is not None:
        suggestion = _suggestion(
            processing_job=processing_job,
            task=task,
            import_job=import_job,
            details=details,
            outcome=outcome,
            bundle=bundle,
            model=model,
        )
        suggestion_id = suggestion.pk
        evidence = dict(import_job.evidence) if isinstance(import_job.evidence, dict) else {}
        evidence[EVIDENCE_KEY] = {"suggestion_id": suggestion_id, "outcome": outcome}
        import_job.evidence = evidence
        import_job.save(update_fields=["evidence", "updated_at"])
    _fail(
        processing_job,
        error,
        outcome=str(outcome),
        suggestion_id=suggestion_id,
        error_detail=error_detail,
    )
    logger.warning(
        "llm_identification_failed",
        **log_fields,
        model=model or log_fields.get("model"),
        outcome=str(outcome),
        error=error,
    )


def latest_suggestion(task: ReviewTask) -> LLMSuggestion | None:
    if task.import_job_id is None:
        return None
    return (
        LLMSuggestion.objects.filter(import_job_id=task.import_job_id)
        .order_by("-created_at", "-pk")
        .first()
    )


def active_job(task: ReviewTask) -> ProcessingJob | None:
    for job in ProcessingJob.objects.filter(
        job_type=ProcessingJob.JobType.LLM_IDENTIFICATION,
        state__in=_ACTIVE_STATES,
        details__review_task_id=task.id,
    ).order_by("-scheduled_at"):
        if not is_stale_running(job):
            return job
    return None
