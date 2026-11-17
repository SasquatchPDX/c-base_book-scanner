"""Which books and variants the library shows (#127, libreflip contract §5, decision D6).

A scan is a *raw scan* until a human finishes it: every state before ``complete``
(``open``, ``capturing``, ``paused``, ``captured``, ``finalizing``) and ``cancelled``.
Raw scans never appear in library browse, search, home or download. That is enforced
here, at the query level, so every library view and the search index use one rule.

**Book rule.** A Book is a *raw-scan book*, and hidden, when all of these hold:

* a *pending* scan job is linked to it: not ``complete``, and not a completed job that
  is only waiting to be re-finished after a replacement page (``refinish_needed``;
  §5 keeps such a book and its variants in the library until the next finish);
* no ``complete`` scan job is linked to it;
* it has no ``complete`` variant from a non-scan source (upload, incoming folder,
  network source).

So a raw scan never hides a book that is already in the library for another reason
(say, a reviewer linked the scan to an existing book with an EPUB), and a book whose
only reason to exist is an unfinished or cancelled scan stays out of the library.

**Variant rule.** A cleaned variant built from a pending scan job (its
``converter_options.scan_job_id``) is hidden even on a visible book. Such a variant
only exists while a finalization is running or after one failed half-way.
"""

from __future__ import annotations

from typing import Any, Final

from django.db.models import Exists, OuterRef, QuerySet
from django.db.models.fields.json import KT

from .models import Book, FileVariant, ScanJob

# Variant sources that put a book in the library on their own, independent of scanning.
LIBRARY_SOURCE_TYPES: Final = (
    FileVariant.SourceType.UPLOAD,
    FileVariant.SourceType.INCOMING_FOLDER,
    FileVariant.SourceType.NETWORK_SOURCE,
)


def pending_scan_jobs() -> QuerySet[ScanJob]:
    """Scan jobs whose output must not be in the library yet."""
    return ScanJob.objects.exclude(state=ScanJob.State.COMPLETE).filter(refinish_needed=False)


def raw_scan_book_ids() -> QuerySet[ScanJob, Any]:
    """Subquery (``values("book_id")``) of the books hidden as raw scans.

    Correlated ``EXISTS`` keeps it cheap: it only looks at the few books linked to a
    pending scan job, never at the whole variant table.
    """
    completed_scan = ScanJob.objects.filter(
        book_id=OuterRef("book_id"), state=ScanJob.State.COMPLETE
    )
    library_variant = FileVariant.objects.filter(
        book_id=OuterRef("book_id"),
        processing_state=FileVariant.ProcessingState.COMPLETE,
        source_type__in=LIBRARY_SOURCE_TYPES,
    )
    return (
        pending_scan_jobs()
        .filter(book_id__isnull=False)
        .filter(~Exists(completed_scan), ~Exists(library_variant))
        .order_by()
        .values("book_id")
    )


def visible_books(queryset: QuerySet[Book] | None = None) -> QuerySet[Book]:
    """``queryset`` (default: every Book) without raw-scan books."""
    books = Book.objects.all() if queryset is None else queryset
    return books.exclude(pk__in=raw_scan_book_ids())


def without_pending_scan_variants(queryset: QuerySet[FileVariant]) -> QuerySet[FileVariant]:
    """``queryset`` without cleaned variants built by a pending scan job.

    A correlated ``EXISTS`` on the variant's ``converter_options.scan_job_id``, so it
    adds no query of its own (it is used in list prefetches).
    """
    return queryset.alias(scan_job_ref=KT("converter_options__scan_job_id")).exclude(
        Exists(pending_scan_jobs().filter(pk=OuterRef("scan_job_ref"))),
        source_type=FileVariant.SourceType.CLEANED,
    )


def library_variants(queryset: QuerySet[FileVariant]) -> QuerySet[FileVariant]:
    """``queryset`` without variants of raw-scan books or from pending scan jobs."""
    return without_pending_scan_variants(queryset.exclude(book_id__in=raw_scan_book_ids()))
