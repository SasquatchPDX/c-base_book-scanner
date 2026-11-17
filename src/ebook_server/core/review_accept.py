"""Accepting a metadata candidate for a review task, shared by the review page and
workers (#59: a reviewer-entered ISBN verified by a provider record).

``resolve_review_source`` and the candidate-to-Book lookup moved here from
``views`` unchanged, so the page's "Accept" button and an automatic acceptance
take exactly the same path.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, cast

from django.db import transaction
from django.utils import timezone

from . import intake, metadata_search
from .models import (
    Author,
    Book,
    FileVariant,
    Identifier,
    MetadataCandidate,
    ReviewTask,
    ScanJob,
    clean_publication_year,
)


def resolve_review_source(
    *,
    task: ReviewTask,
    book: Book,
    reviewer: Any,
    decision: str,
    selected_candidate: MetadataCandidate | None = None,
    decision_notes: str = "",
    decision_payload: dict[str, Any] | None = None,
) -> FileVariant | None:
    import_job = task.import_job
    if import_job is not None:
        return intake.resolve_import_with_review_data(
            job=import_job,
            review_task=task,
            book=book,
            selected_candidate=selected_candidate,
            reviewer=reviewer,
            decision=decision,
            decision_notes=decision_notes,
            decision_payload=decision_payload,
        )
    if task.scan_job_id is None:
        raise ValueError("review task has no source")
    with transaction.atomic():
        scan_job = ScanJob.objects.select_for_update().get(pk=task.scan_job_id)
        scan_job.book = book
        scan_job.save(update_fields=["book_id", "updated_at"])
        task.state = ReviewTask.State.RESOLVED
        task.decision = decision
        task.decision_notes = decision_notes
        task.decided_at = timezone.now()
        if decision_payload is not None:
            task.decision_payload = decision_payload
        task.resolved_book = book
        task.selected_candidate = selected_candidate
        task.reviewer = reviewer
        task.save(
            update_fields=[
                "state",
                "decision",
                "decision_notes",
                "decision_payload",
                "resolved_book_id",
                "selected_candidate_id",
                "reviewer_id",
                "decided_at",
                "updated_at",
            ],
        )
    return None


def book_for_candidate(candidate: MetadataCandidate) -> Book:
    """The library Book a candidate describes: by ISBN, then title+author, else new."""
    book: Book | None = None
    for value in (candidate.isbn13, candidate.isbn10):
        if book is None and value:
            existing_identifier = (
                Identifier.objects.filter(value=value).select_related("book").first()
            )
            if existing_identifier is not None:
                book = existing_identifier.book
    if book is None:
        candidate_author_set = {name.lower() for name in candidate.authors if isinstance(name, str)}
        for existing_book in Book.objects.filter(title__iexact=candidate.title):
            known_authors = [author.full_name.lower() for author in existing_book.authors.all()]
            if candidate_author_set.intersection(known_authors):
                book = existing_book
                break
    if book is not None:
        return book

    candidate_edition = getattr(candidate, "edition", "")
    book = Book.objects.create(
        title=candidate.title,
        publisher=candidate.publisher or "",
        language=candidate.language or "",
        description=candidate.description or "",
        edition=candidate_edition or "",
    )
    book.publication_year = clean_publication_year(candidate.publication_year)
    for author_name in candidate.authors:
        if not isinstance(author_name, str):
            continue
        author, _ = Author.objects.get_or_create(full_name=author_name.strip())
        if author_name.strip():
            book.authors.add(author)
    if candidate.isbn10:
        Identifier.objects.get_or_create(
            book=book,
            kind=Identifier.IdentifierType.ISBN10,
            value=candidate.isbn10,
            defaults={"normalized_value": candidate.isbn10},
        )
    if candidate.isbn13:
        Identifier.objects.get_or_create(
            book=book,
            kind=Identifier.IdentifierType.ISBN13,
            value=candidate.isbn13,
            defaults={"normalized_value": candidate.isbn13},
        )
    book.save()
    return book


def accept_candidate(
    *,
    task: ReviewTask,
    candidate: MetadataCandidate,
    reviewer: Any,
    reviewer_authors: list[str] | None = None,
    decision_notes: str = "",
    extra_payload: dict[str, Any] | None = None,
) -> tuple[Book, FileVariant | None]:
    """Resolve ``task`` by accepting ``candidate`` (the page's "Accept" action).

    A candidate without authors can be accepted with authors the reviewer
    confirmed; they only fill a Book that has no authors yet (provider authors
    always win). ``extra_payload`` is merged into the task's decision payload.
    """
    book = book_for_candidate(candidate)
    candidate_has_authors = any(
        isinstance(name, str) and name.strip() for name in candidate.authors
    )
    authors = [] if candidate_has_authors else list(reviewer_authors or [])
    payload_additions: dict[str, Any] = dict(extra_payload or {})
    if authors and not book.authors.exists():
        for author_name in authors:
            author, _ = Author.objects.get_or_create(
                full_name=intake._fit(Author, "full_name", author_name),
            )
            book.authors.add(author)
        payload_additions["reviewer_authors"] = authors
    decision_payload: dict[str, Any] | None = None
    if payload_additions:
        decision_payload = {
            **(task.decision_payload if isinstance(task.decision_payload, dict) else {}),
            **payload_additions,
        }
    variant = resolve_review_source(
        task=task,
        book=book,
        selected_candidate=candidate,
        reviewer=reviewer,
        decision=cast(str, ReviewTask.Decision.ACCEPT_CANDIDATE),
        decision_notes=decision_notes or f"Accepted candidate {candidate.id}",
        decision_payload=decision_payload,
    )
    return book, variant


# ------------------------------------------------------------- ISBN verification


def auto_acceptable_isbn_candidate(
    candidates: Iterable[MetadataCandidate],
    isbn: str,
) -> MetadataCandidate | None:
    """The candidate to accept for a reviewer-entered ISBN, or ``None``.

    Only records whose own provider identifiers carry the ISBN count
    (:func:`metadata_search.record_carries_isbn`); they must all describe one
    distinct book, and the chosen record must name its authors (an authorless
    ISBN match is left for the reviewer to confirm, as at intake, #42).
    """
    verified = metadata_search.verified_isbn_candidates(candidates, isbn)
    if not verified or len(metadata_search.distinct_books(verified)) != 1:
        return None
    best = verified[0]
    if not any(isinstance(name, str) and name.strip() for name in best.authors):
        return None
    return best
