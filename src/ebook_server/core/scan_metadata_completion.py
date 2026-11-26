"""Automatic completion of missing scanner-OPF fields (#227).

The scanner's ``metadata.opf`` is trusted (contract decision D3, :mod:`.scanner_opf`).
When a new OPF version is stored for a scan job that is not linked to a Book yet and the
OPF lacks one of the important fields (title, authors, ISBN), a scan-identification job
looks the scan up in the background, on the metadata queue
(:func:`ebook_server.jobs.metadata_queue_name`), with the OPF's own evidence:

* an OPF ISBN -> an ISBN lookup (:func:`metadata_search.isbn_signals`); this also covers
  an OPF that carries nothing but the ISBN;
* otherwise title, authors and language.

Rule for filling fields (AGENTS.md: weak matches are never silently accepted):

* ISBN lookup: the provider records that themselves list the OPF's ISBN must all
  describe one book, and that record must name its authors (the same rule as a
  reviewer-entered ISBN, :func:`review_accept.auto_acceptable_isbn_candidate`);
* text lookup: the intake auto-accept rule (:func:`intake.decide_auto_accept`: score at
  or above ``BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT``, no tie between different books,
  author, edition and title checks);
* in both cases the record must agree with what the OPF already says (title and
  authors, :func:`scanner_opf._agrees`).

Only a strong match completes fields, and only fields the OPF leaves empty: the OPF's
own values are never replaced. The completion is stored on the job
(``ScanJob.notes["opf_completion"]``), tied to the OPF version it was computed for, and
used when finish links or creates the Book (:func:`merged_fields`); there each completed
field is recorded with provenance ``provider_check``. A newer OPF version makes an older
completion obsolete and starts a new lookup if fields are still missing.

A weak or no match changes nothing. If the OPF has a title the scan keeps its trusted
scanner metadata (the provider check after finish verifies it). If it has no title the
Book's identity is unknown, so the scan-identification review opens (waiting for a
human), as at finish.
"""

from __future__ import annotations

from typing import Any, Final

import structlog
from django.utils import timezone

from ebook_server.jobs import tunable_setting

from . import identification, intake, metadata_search
from .languages import normalize_language
from .models import Book, ProcessingJob, ScanJob, ScanJobOpf

logger = structlog.get_logger(__name__)

NOTES_KEY: Final = "opf_completion"
# Missing any of these starts a lookup.
TRIGGER_FIELDS: Final = ("title", "authors", "isbn")
# Fields a strong match may fill when the OPF leaves them empty.
COMPLETABLE_FIELDS: Final = ("title", "authors", "isbn", "language", "publisher")

STATUS_COMPLETED: Final = "completed"
STATUS_NO_STRONG_MATCH: Final = "no_strong_match"
STATUS_CONTRADICTED: Final = "contradicted"
STATUS_STALE: Final = "opf_superseded"

_CLOSED_STATES: Final = frozenset(
    {ScanJob.State.FINALIZING, ScanJob.State.COMPLETE, ScanJob.State.CANCELLED}
)


def enabled() -> bool:
    value = tunable_setting("BOOK_SCANNER_SCAN_METADATA_COMPLETION", True)
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def missing_fields(fields: dict[str, Any]) -> list[str]:
    """The important OPF fields that are empty."""
    return [name for name in TRIGGER_FIELDS if _is_empty(fields.get(name))]


def lookup_terms(fields: dict[str, Any]) -> dict[str, Any] | None:
    """Search terms from the OPF's own evidence, or ``None`` when it has none."""
    isbn = metadata_search.canonical_isbn13(fields.get("isbn"))
    if isbn is not None:
        return {"isbn": isbn}
    title = fields.get("title") if isinstance(fields.get("title"), str) else ""
    authors = [
        name.strip()
        for name in fields.get("authors") or []
        if isinstance(name, str) and name.strip()
    ]
    if not title and not authors:
        return None
    terms: dict[str, Any] = {"title": title or "", "authors": authors}
    language = fields.get("language")
    if isinstance(language, str) and language.strip():
        terms["language"] = language.strip()
    return terms


def schedule(scan_job_id: str, opf_id: int) -> None:
    """Request the lookup once the transaction that stored the OPF has committed."""
    from django.db import transaction

    transaction.on_commit(lambda: request(scan_job_id, opf_id))


def request(scan_job_id: str, opf_id: int) -> ProcessingJob | None:
    """Queue the automatic lookup for OPF ``opf_id`` if it is still needed.

    Never raises: an OPF upload must not fail because a lookup could not be queued.
    """
    if not enabled():
        return None
    from . import scan_identification, scanner_opf

    try:
        job = ScanJob.objects.filter(pk=scan_job_id).first()
        if job is None or job.book_id is not None or job.state in _CLOSED_STATES:
            return None
        opf = scanner_opf.effective_opf(job)
        if opf is None or opf.pk != opf_id:
            return None
        fields = opf.fields if isinstance(opf.fields, dict) else {}
        if not missing_fields(fields):
            return None
        notes = job.notes if isinstance(job.notes, dict) else {}
        previous = notes.get(NOTES_KEY)
        if isinstance(previous, dict) and previous.get("opf_id") == opf.pk:
            return None
        terms = lookup_terms(fields)
        if terms is None:
            return None
        return scan_identification.request_scan_identification(
            scan_job=job,
            requested_by_user_id=None,
            terms=terms,
            automatic={"opf_id": opf.pk, "opf_version": opf.version},
        )
    except (
        scan_identification.ScanIdentificationError,
        metadata_search.SearchInProgressError,
    ) as exc:
        logger.info(
            "scan_metadata_completion_not_requested",
            scan_job_id=scan_job_id,
            opf_id=opf_id,
            reason=str(exc),
        )
    except Exception:
        logger.exception(
            "scan_metadata_completion_request_failed", scan_job_id=scan_job_id, opf_id=opf_id
        )
    return None


def _named_authors(candidate: identification.IdentificationCandidate) -> list[str]:
    return [
        name
        for name in candidate.authors
        if isinstance(name, str) and name.strip() and not identification.is_placeholder_author(name)
    ]


def strong_match(
    fields: dict[str, Any],
    *,
    mode: str,
    signals: identification.IdentificationInput,
    candidates: list[identification.IdentificationCandidate],
) -> tuple[str, identification.IdentificationCandidate | None]:
    """``(status, candidate)``: whether a lookup result may complete the OPF."""
    from . import scanner_opf

    best: identification.IdentificationCandidate | None
    if mode == "isbn":
        isbn = metadata_search.canonical_isbn13(fields.get("isbn"))
        verified = [
            candidate
            for candidate in candidates
            if isbn is not None and isbn in scanner_opf._candidate_isbns(candidate)
        ]
        if not verified:
            return STATUS_NO_STRONG_MATCH, None
        best = verified[0]
        if any(
            not identification.candidates_describe_same_book(best, other) for other in verified[1:]
        ) or not _named_authors(best):
            return STATUS_NO_STRONG_MATCH, None
    else:
        decision = intake.decide_auto_accept(
            candidates, list(fields.get("authors") or []), file_evidence=signals
        )
        best = decision.candidate
        if best is None:
            return STATUS_NO_STRONG_MATCH, None
    if not scanner_opf._agrees(fields, best):
        return STATUS_CONTRADICTED, best
    return STATUS_COMPLETED, best


def completion_values(
    fields: dict[str, Any], candidate: identification.IdentificationCandidate
) -> dict[str, Any]:
    """Values for the OPF's empty completable fields, from ``candidate``."""
    from . import scanner_opf

    values: dict[str, Any] = {}
    for name in COMPLETABLE_FIELDS:
        if not _is_empty(fields.get(name)):
            continue
        value: Any = None
        if name == "title":
            value = intake._fit(Book, "title", candidate.title) if candidate.title else None
        elif name == "authors":
            value = _named_authors(candidate) or None
        elif name == "isbn":
            own = scanner_opf._candidate_isbns(candidate)
            for raw in (candidate.isbn13, candidate.isbn10):
                canonical = metadata_search.canonical_isbn13(raw)
                if canonical is not None and canonical in own:
                    value = canonical
                    break
        elif name == "language":
            value = normalize_language(candidate.language) if candidate.language else None
        elif name == "publisher":
            value = candidate.publisher.strip() if candidate.publisher else None
        if not _is_empty(value):
            values[name] = value
    return values


def _summary(candidate: identification.IdentificationCandidate) -> dict[str, Any]:
    return {
        "provider_id": candidate.provider_id,
        "provider_ref": candidate.provider_ref,
        "title": candidate.title,
        "authors": list(candidate.authors),
        "isbn13": candidate.isbn13,
        "score": round(float(candidate.score), 4),
    }


def publish(
    scan_job: ScanJob,
    automatic: dict[str, Any],
    *,
    mode: str,
    signals: identification.IdentificationInput,
    candidates: list[identification.IdentificationCandidate],
    processing_job_id: str,
    search_id: str,
) -> dict[str, Any]:
    """Record the outcome of an automatic lookup on ``scan_job`` (row locked by caller).

    Returns the outcome; ``needs_review`` tells the caller to open the identification
    review (no strong match and the OPF has no title).
    """
    from . import scanner_opf

    opf = scanner_opf.effective_opf(scan_job)
    if opf is None or opf.pk != automatic.get("opf_id"):
        return {"status": STATUS_STALE, "needs_review": False}
    fields = opf.fields if isinstance(opf.fields, dict) else {}
    status, candidate = strong_match(fields, mode=mode, signals=signals, candidates=candidates)
    outcome: dict[str, Any] = {
        "status": status,
        "opf_id": opf.pk,
        "opf_version": opf.version,
        "mode": mode,
        "processing_job_id": processing_job_id,
        "search_id": search_id,
        "checked_at": timezone.now().isoformat(),
        "candidate_count": len(candidates),
        "fields": completion_values(fields, candidate)
        if status == STATUS_COMPLETED and candidate is not None
        else {},
        "candidate": _summary(candidate) if candidate is not None else None,
    }
    notes = dict(scan_job.notes) if isinstance(scan_job.notes, dict) else {}
    notes[NOTES_KEY] = outcome
    scan_job.notes = notes
    update = ["notes"]
    completed = outcome["fields"]
    if completed.get("title") and not scan_job.provisional_title:
        scan_job.provisional_title = completed["title"]
        update.append("provisional_title")
    if completed.get("authors") and not scan_job.provisional_authors:
        scan_job.provisional_authors = list(completed["authors"])
        update.append("provisional_authors")
    scan_job.save(update_fields=[*update, "updated_at"])
    logger.info(
        "scan_metadata_completion_published",
        scan_job_id=str(scan_job.pk),
        opf_version=opf.version,
        status=status,
        completed=sorted(completed),
    )
    return {**outcome, "needs_review": status != STATUS_COMPLETED and not fields.get("title")}


def completion_for(notes: Any, opf_id: Any) -> dict[str, Any]:
    """The completed fields stored in ``notes`` for OPF ``opf_id`` (empty if none)."""
    stored = notes.get(NOTES_KEY) if isinstance(notes, dict) else None
    if (
        not isinstance(stored, dict)
        or stored.get("status") != STATUS_COMPLETED
        or stored.get("opf_id") != opf_id
        or not isinstance(stored.get("fields"), dict)
    ):
        return {}
    return dict(stored["fields"])


def merged_fields(job: ScanJob, opf: ScanJobOpf) -> tuple[dict[str, Any], dict[str, Any]]:
    """``(fields, completed)``: the OPF's fields with completed values in empty ones."""
    fields = dict(opf.fields) if isinstance(opf.fields, dict) else {}
    completed = {
        name: value
        for name, value in completion_for(job.notes, opf.pk).items()
        if _is_empty(fields.get(name))
    }
    fields.update(completed)
    return fields, completed
