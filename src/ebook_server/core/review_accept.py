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

from . import genres, intake, metadata_search
from .models import (
    Author,
    Book,
    FileVariant,
    Genre,
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
    inline_cover_fallback: bool = True,
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
            inline_cover_fallback=inline_cover_fallback,
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


def apply_review_genres(
    *,
    book: Book,
    task: ReviewTask,
    reviewer_genres: list[Genre] | None,
    provider_subjects: list[Any] | None,
    reviewer: Any = None,
) -> dict[str, Any]:
    """Attach genres on a review decision; returns decision-payload additions.

    An explicit reviewer choice, even an empty one, marks the Book's genres curated.
    """
    embedded = genres.embedded_subjects(task.embedded_metadata)
    if reviewer_genres is not None:
        genres.assign_reviewer(
            book,
            reviewer_genres,
            suggestion=genres.suggest(provider_subjects, embedded),
            user=reviewer,
        )
        return {"reviewer_genres": [genre.name for genre in reviewer_genres]}
    added = genres.assign_automatic(
        book,
        provider_subjects=provider_subjects,
        embedded_subjects=embedded,
    )
    return {"automatic_genres": [genre.name for genre in added]} if added else {}


def _identifier_kind(value: str) -> str:
    if len(value) == 10:
        return cast(str, Identifier.IdentifierType.ISBN10)
    return cast(str, Identifier.IdentifierType.ISBN13)


def candidate_isbns(
    candidate: MetadataCandidate, isbn: str | None = None
) -> tuple[list[str], bool]:
    """The ISBNs that identify the accepted edition, and whether they are *bound*.

    An ISBN is bound when it is authoritative for this acceptance: ``isbn`` (the
    reviewer-entered ISBN a provider record verified), else the searched ISBN the
    record itself carries (:func:`metadata_search.bound_isbn`). It is returned in
    both forms (ISBN-13, then ISBN-10). Otherwise the candidate's own ISBN-13 and
    only an ISBN-10 that is the same ISBN: a record's first ISBN-10 and first
    ISBN-13 can belong to different editions and must not be paired.
    """
    bound = (metadata_search.canonical_isbn13(isbn) if isbn else None) or (
        metadata_search.bound_isbn(candidate)
    )
    if bound is not None:
        return metadata_search.isbn_forms(bound), True
    isbn13 = candidate.isbn13 or ""
    isbn10 = candidate.isbn10 or ""
    canonical13 = metadata_search.canonical_isbn13(isbn13)
    canonical10 = metadata_search.canonical_isbn13(isbn10)
    if canonical13 and canonical10 and canonical13 != canonical10:
        isbn10 = ""
    return [value for value in (isbn13, isbn10) if value], False


def book_for_candidate(candidate: MetadataCandidate, *, isbn: str | None = None) -> Book:
    """The library Book a candidate describes: by ISBN, then title+author, else new.

    Only the ISBNs from :func:`candidate_isbns` are looked up and written, so a
    multi-ISBN record never merges into a Book that holds one of its *other*
    ISBNs. A bound ISBN (a verified reviewer-entered or searched ISBN) is always
    written to the Book, including an existing one matched by title and author.
    """
    isbns, bound = candidate_isbns(candidate, isbn)
    book: Book | None = None
    for value in isbns:
        existing_identifier = Identifier.objects.filter(value=value).select_related("book").first()
        if existing_identifier is not None:
            book = existing_identifier.book
            break
    if book is None:
        candidate_author_set = {name.lower() for name in candidate.authors if isinstance(name, str)}
        for existing_book in Book.objects.filter(title__iexact=candidate.title):
            known_authors = [author.full_name.lower() for author in existing_book.authors.all()]
            if candidate_author_set.intersection(known_authors):
                book = existing_book
                break
    if book is not None:
        if bound:
            _add_isbn_identifiers(book, isbns)
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
    _add_isbn_identifiers(book, isbns)
    book.save()
    return book


def _add_isbn_identifiers(book: Book, isbns: list[str]) -> None:
    for value in isbns:
        Identifier.objects.get_or_create(
            book=book,
            kind=_identifier_kind(value),
            value=value,
            defaults={"normalized_value": value},
        )


def accept_candidate(
    *,
    task: ReviewTask,
    candidate: MetadataCandidate,
    reviewer: Any,
    reviewer_authors: list[str] | None = None,
    reviewer_genres: list[Genre] | None = None,
    isbn: str | None = None,
    decision_notes: str = "",
    extra_payload: dict[str, Any] | None = None,
    inline_cover_fallback: bool = True,
) -> tuple[Book, FileVariant | None]:
    """Resolve ``task`` by accepting ``candidate`` (the page's "Accept" action).

    A candidate without authors can be accepted with authors the reviewer
    confirmed; they only fill a Book that has no authors yet (provider authors
    always win). Genres follow the review page's rules (:func:`apply_review_genres`):
    an explicit ``reviewer_genres`` choice, else automatic genres from the
    candidate's subjects. ``isbn`` is a verified reviewer-entered ISBN: it decides
    the Book's identifiers (:func:`candidate_isbns`). ``extra_payload`` is merged
    into the task's decision payload.
    """
    book = book_for_candidate(candidate, isbn=isbn)
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
    payload_additions.update(
        apply_review_genres(
            book=book,
            task=task,
            reviewer_genres=reviewer_genres,
            provider_subjects=candidate.subjects,
            reviewer=reviewer,
        )
    )
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
        inline_cover_fallback=inline_cover_fallback,
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
    distinct book (:func:`metadata_search.same_book`: title, authors and ISBN
    lists), and the chosen record must name its authors (an authorless ISBN match
    is left for the reviewer to confirm, as at intake, #42).
    """
    verified = metadata_search.verified_isbn_candidates(candidates, isbn)
    # Distinct by title *and* authors (and disjoint ISBN lists): two "Selected
    # Poems" by different poets carrying one ISBN are left for the reviewer.
    if not verified or len(metadata_search.distinct_books(verified)) != 1:
        return None
    best = verified[0]
    if not any(isinstance(name, str) and name.strip() for name in best.authors):
        return None
    return best
