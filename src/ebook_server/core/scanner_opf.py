"""The scanner's ``metadata.opf`` (#130, libreflip contract D3, §4.5, §6.8).

The scanner (``sans``) sends the book's bibliographic metadata as a Calibre-style OPF.
Per contract decision D3 it is the primary metadata source:

* Every upload with a new checksum is stored as a new immutable :class:`ScanJobOpf`
  version; the newest is current. While the scan is raw it creates **no** Book: the job
  page and the "Raw scans" list only *show* its title, authors, ISBN and language.
* At finish (:func:`link_book_from_opf`, a :mod:`.scan_lifecycle` book linker):

  - the OPF's ISBN matches exactly one Book -> the job is linked to that Book (it gains a
    new variant); the OPF fills that Book's empty fields, differing fields open a review;
  - otherwise a new Book is created from the OPF, provenance ``scanner_opf``;
  - no OPF, or an OPF without a title -> the existing scan identification flow.

  Two cases that would silently guess are routed to scan identification and its review
  instead (AGENTS.md "never hide guesses"): an ISBN that several Books carry, and an
  ISBN-less OPF whose normalised title and author set match an existing Book (a likely
  duplicate). The review page then offers "create a Book from the scanner metadata" next
  to the usual merge.

* An OPF received after linking updates the Book's scanner-provenance fields, with a
  :class:`BookMetadataProvenance` history row per change. A field a reviewer edited by
  hand, or that changed outside the scanner since the scanner last set it, is not
  overwritten: that conflict opens a review task.

**External verification (owner decision on D3 vs AGENTS.md).** AGENTS.md makes external
verification mandatory for intake; D3 makes the scanner authoritative. Both hold: after a
Book is created or linked from an OPF, a background job (:func:`process_provider_check_job`)
looks the OPF's title, authors and ISBN up through the provider abstraction (rate-limited
like every provider call). If providers confirm or find nothing, the scanner metadata
stays. If a confident provider record contradicts the scanner's ISBN, title or authors,
nothing is overwritten: a review task shows both, with the scanner OPF preselected. The
result is recorded in the Book's provenance and on the OPF version.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Final, cast

import structlog
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from ebook_server.jobs import get_queue

from . import identification, intake, metadata_search, review_accept, scan_lifecycle, scan_manifest
from .languages import normalize_language
from .models import (
    Author,
    Book,
    BookMetadataProvenance,
    Identifier,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    ScanJobOpf,
)

logger = structlog.get_logger(__name__)

MANIFEST_NAME: Final = "metadata_opf"
ITEM_KEY: Final = "metadata_opf"
PROVENANCE_SOURCE: Final = BookMetadataProvenance.Source.SCANNER_OPF
# Book fields the OPF provides, in display order.
FIELDS: Final = ("title", "authors", "isbn", "language", "publisher", "description")
FIELD_LABELS: Final = {
    "title": "Title",
    "authors": "Authors",
    "isbn": "ISBN",
    "language": "Language",
    "publisher": "Publisher",
    "description": "Description",
}
# Fields the provider check compares (contract §4.5 display fields that providers know).
CHECKED_FIELDS: Final = ("title", "authors", "isbn")

# ReviewTask.decision_payload key and the kinds of scanner-OPF reviews.
REVIEW_KEY: Final = "scanner_opf"
KIND_PROVIDER_CONFLICT: Final = "provider_conflict"
KIND_FIELD_CONFLICT: Final = "field_conflict"
CONFLICT_KINDS: Final = frozenset({KIND_PROVIDER_CONFLICT, KIND_FIELD_CONFLICT})
# ScanJob.notes key: why finish handed an OPF job to scan identification.
NOTES_KEY: Final = "scanner_opf"

CHECK_CONFIRMED: Final = "confirmed"
CHECK_NO_RESULTS: Final = "no_results"
CHECK_INCONCLUSIVE: Final = "inconclusive"
CHECK_PROVIDER_ERROR: Final = "provider_error"
CHECK_CONTRADICTED: Final = "contradicted"

_ISBN_KINDS: Final = (Identifier.IdentifierType.ISBN10, Identifier.IdentifierType.ISBN13)
_OPEN_REVIEW_STATES: Final = (ReviewTask.State.OPEN, ReviewTask.State.CLAIMED)
_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")


class InvalidOpf(ValueError):
    """The document is not a well-formed OPF with ``package/metadata``, or is hostile."""


class ScannerOpfError(RuntimeError):
    """A review action on a scanner-OPF task cannot be carried out."""


class StaleReview(ScannerOpfError):
    """The review no longer matches the scan's metadata (HTTP 409).

    Its OPF version is no longer the effective one, the scan is linked to another Book,
    the Book's metadata changed since the review was opened, or the evidence changed
    since the reviewer loaded it. Nothing was written to the Book.
    """


# --- parsing -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedOpf:
    fields: dict[str, Any]
    app_generated: bool


def _local_name(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    return cleaned or None


def parse_opf(payload: bytes) -> ParsedOpf:
    """Parse and validate an uploaded ``metadata.opf``.

    defusedxml rejects DTD entities (billion laughs) and external entities/references
    (XXE) before anything is expanded; the root must be ``package`` with a ``metadata``
    child. The Dublin-Core fields come from the intake OPF-sidecar parser
    (:func:`intake.parse_opf_metadata`). An OPF with the application's own
    ``ebook-server:generated`` marker is valid but carries no fields.
    """
    try:
        root = ET.fromstring(payload)
        if _local_name(root.tag) != "package" or not any(
            _local_name(child.tag) == "metadata" for child in root
        ):
            raise InvalidOpf("The OPF must have a package/metadata element.")
        generated = intake.opf_is_app_generated(payload)
        raw = {} if generated else intake.parse_opf_metadata(payload)
    except InvalidOpf:
        raise
    except DefusedXmlException as exc:
        raise InvalidOpf("The OPF uses forbidden XML constructs (entities or DTDs).") from exc
    except (ET.ParseError, ValueError, LookupError) as exc:
        raise InvalidOpf("The OPF is not well-formed XML.") from exc
    return ParsedOpf(fields=_clean_fields(raw), app_generated=generated)


def _clean_fields(raw: dict[str, Any]) -> dict[str, Any]:
    authors: list[str] = []
    for name in raw.get("authors") or []:
        cleaned = _text(name)
        if cleaned and cleaned not in authors:
            authors.append(intake._fit(Author, "full_name", cleaned))
    identifier = _text(raw.get("identifier"))
    isbn = None
    if identifier and "uuid" not in identifier.lower():
        isbn = next(
            (
                canonical
                for value in identification.normalize_isbns(identifier)
                if (canonical := metadata_search.canonical_isbn13(value)) is not None
            ),
            None,
        )
    language_raw = _text(raw.get("language"))
    title = _text(raw.get("title"))
    return {
        "title": intake._fit(Book, "title", title) if title else None,
        "authors": authors,
        "isbn": isbn,
        "identifier": identifier,
        "language": normalize_language(language_raw) or language_raw,
        "publisher": _text(raw.get("publisher")),
        "description": raw.get("description") if isinstance(raw.get("description"), str) else None,
        "subjects": [s for s in raw.get("subjects") or [] if isinstance(s, str)],
    }


# --- versions --------------------------------------------------------------------------------


@dataclass(frozen=True)
class StoreResult:
    opf: ScanJobOpf
    created: bool


def current_opf(job: ScanJob) -> ScanJobOpf | None:
    """The newest upload (what ``GET jobs/{id}`` and capture-complete compare)."""
    return job.opf_versions.order_by("-version").first()


def effective_opf(job: ScanJob) -> ScanJobOpf | None:
    """The newest upload that is metadata (not the application's own generated OPF)."""
    return job.opf_versions.filter(app_generated=False).order_by("-version").first()


def store_opf(
    job: ScanJob,
    payload: bytes,
    *,
    sha256: str,
    parsed: ParsedOpf,
    source: str = "scanner",
) -> StoreResult:
    """Store an upload on a job row the caller has locked (§6.8).

    The checksum of the current version answers ``created=False`` and changes nothing.
    A new version updates the job's provisional title/authors (shown on the job page)
    and, for a job already linked to a Book, is applied to that Book.
    """
    current = current_opf(job)
    if current is not None and current.sha256 == sha256:
        return StoreResult(current, False)
    opf = ScanJobOpf.objects.create(
        scan_job=job,
        version=(current.version + 1) if current is not None else 1,
        sha256=sha256,
        size_bytes=len(payload),
        content=payload,
        fields=parsed.fields,
        app_generated=parsed.app_generated,
        source=source,
    )
    logger.info(
        "scanner_opf_stored",
        scan_job_id=str(job.pk),
        version=opf.version,
        app_generated=opf.app_generated,
        has_title=bool(parsed.fields.get("title")),
        has_isbn=bool(parsed.fields.get("isbn")),
    )
    if parsed.app_generated:
        return StoreResult(opf, True)
    # Unclaimed reviews of earlier versions are obsolete now; claimed ones keep their
    # evidence and are rejected (and closed) when someone tries to resolve them.
    supersede_open_reviews(job, opf)
    update_fields = []
    if parsed.fields.get("title"):
        job.provisional_title = parsed.fields["title"]
        update_fields.append("provisional_title")
    if parsed.fields.get("authors"):
        job.provisional_authors = list(parsed.fields["authors"])
        update_fields.append("provisional_authors")
    if update_fields:
        job.save(update_fields=[*update_fields, "updated_at"])
    if job.book_id is not None:
        book = Book.objects.select_for_update().get(pk=job.book_id)
        apply_opf_to_book(book, opf, job)
        request_provider_check(job=job, book=book, opf=opf)
    return StoreResult(opf, True)


def display_metadata(job: ScanJob) -> dict[str, Any] | None:
    """Title, authors, ISBN and language of the current OPF, for the job page."""
    opf = effective_opf(job)
    newest = current_opf(job)
    if opf is None:
        if newest is None:
            return None
        return {"version": newest.version, "sha256": newest.sha256, "ignored": True}
    fields = opf.fields if isinstance(opf.fields, dict) else {}
    return {
        "version": opf.version,
        "sha256": opf.sha256,
        "received_at": opf.received_at.isoformat(),
        "title": fields.get("title"),
        "authors": list(fields.get("authors") or []),
        "isbn": fields.get("isbn"),
        "language": fields.get("language"),
        "ignored": False,
        "newer_ignored": newest is not None and newest.pk != opf.pk,
        "provider_check": opf.provider_check or None,
    }


# --- manifest source and "Raw scans" annotator (S3 hooks) ------------------------------


class OpfManifestSource:
    """``metadata_opf`` in ``GET jobs/{id}`` (§6.4) and capture-complete (§6.9)."""

    name = MANIFEST_NAME

    def received(self, job: ScanJob) -> dict[str, scan_manifest.ReceivedItem]:
        opf = current_opf(job)
        if opf is None:
            return {}
        return {ITEM_KEY: scan_manifest.ReceivedItem(opf.sha256, opf.version)}

    def status(self, job: ScanJob) -> dict[str, Any] | None:
        opf = current_opf(job)
        return {"sha256": opf.sha256, "version": opf.version} if opf is not None else None

    def expected(self, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, dict):
            raise scan_manifest.ManifestError(MANIFEST_NAME, "metadata_opf must be an object.")
        sha256 = value.get("sha256")
        if not isinstance(sha256, str) or _SHA256_RE.fullmatch(sha256) is None:
            raise scan_manifest.ManifestError(
                MANIFEST_NAME, "metadata_opf.sha256 must be a lowercase hex SHA-256."
            )
        return {ITEM_KEY: sha256}


def annotate_raw_scan(summary: scan_lifecycle.RawScanSummary) -> None:
    metadata = display_metadata(summary.job)
    if metadata is None or metadata.get("ignored"):
        return
    if metadata.get("title"):
        summary.title = metadata["title"]
    summary.metadata.update(
        {
            "title": metadata.get("title"),
            "authors": metadata.get("authors") or [],
            "isbn": metadata.get("isbn"),
            "language": metadata.get("language"),
            "opf_version": metadata["version"],
        }
    )


# --- Book field values and provenance --------------------------------------------------


def _isbn_values(book: Book) -> list[str]:
    values = {
        canonical
        for value in book.identifiers.filter(kind__in=_ISBN_KINDS).values_list("value", flat=True)
        if (canonical := metadata_search.canonical_isbn13(value)) is not None
    }
    return sorted(values)


def book_value(book: Book, field_name: str) -> Any:
    """A Book field in the form provenance rows store (ISBN: list of ISBN-13 forms)."""
    if field_name == "authors":
        return [author.full_name for author in book.authors.order_by("id")]
    if field_name == "isbn":
        return _isbn_values(book)
    return getattr(book, field_name) or ""


def snapshot_book(book: Book) -> dict[str, Any]:
    return {field_name: book_value(book, field_name) for field_name in FIELDS}


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode()).hexdigest()


def book_revision(book: Book, field_names: Iterable[str]) -> str:
    """A fingerprint of the Book's values for ``field_names`` (what a review would write)."""
    return _digest({field_name: book_value(book, field_name) for field_name in field_names})


def opf_value(fields: dict[str, Any], field_name: str) -> Any:
    """The OPF's value for a Book field, or ``None`` when it gives none."""
    if field_name == "authors":
        return list(fields.get("authors") or []) or None
    if field_name == "isbn":
        return [fields["isbn"]] if fields.get("isbn") else None
    value = fields.get(field_name)
    return value if isinstance(value, str) and value.strip() else None


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == []


def same_value(field_name: str, wanted: Any, current: Any) -> bool:
    """Whether ``current`` already holds ``wanted`` (ISBN: every wanted ISBN present)."""
    if _is_empty(wanted) or _is_empty(current):
        return _is_empty(wanted) and _is_empty(current)
    if field_name == "title":
        return identification.normalize_text(wanted) == identification.normalize_text(
            current
        ) or identification.titles_agree(wanted, current)
    if field_name == "authors":
        return identification.author_lists_agree(list(wanted), list(current))
    if field_name == "isbn":
        return set(wanted) <= set(current)
    if field_name == "language":
        return (normalize_language(wanted) or wanted.lower()) == (
            normalize_language(current) or current.lower()
        )
    return identification.normalize_text(wanted) == identification.normalize_text(current)


def latest_set(book: Book, field_name: str) -> BookMetadataProvenance | None:
    return (
        BookMetadataProvenance.objects.filter(
            book=book, field=field_name, action=BookMetadataProvenance.Action.SET
        )
        .order_by("-created_at", "-id")
        .first()
    )


def _record(
    book: Book,
    *,
    field_name: str = "",
    source: str,
    action: str,
    value: Any = None,
    previous_value: Any = None,
    details: dict[str, Any] | None = None,
    job: ScanJob | None = None,
    opf: ScanJobOpf | None = None,
    user: Any = None,
) -> BookMetadataProvenance:
    return BookMetadataProvenance.objects.create(
        book=book,
        field=field_name,
        source=source,
        action=action,
        value=value,
        previous_value=previous_value,
        details=details or {},
        scan_job=job,
        opf=opf,
        user=user if getattr(user, "pk", None) is not None else None,
    )


def _write_field(book: Book, field_name: str, value: Any, previous: Any) -> None:
    if field_name == "authors":
        authors = [
            Author.objects.get_or_create(full_name=intake._fit(Author, "full_name", name))[0]
            for name in value
            if isinstance(name, str) and name.strip()
        ]
        book.authors.set(authors)
        book.save(update_fields=["updated_at"])
        return
    if field_name == "isbn":
        dropped = set(previous or []) - set(value)
        for isbn in dropped:
            forms = metadata_search.isbn_forms(isbn)
            book.identifiers.filter(kind__in=_ISBN_KINDS).filter(
                Q(value__in=forms) | Q(normalized_value__in=forms)
            ).delete()
        for isbn in value:
            review_accept._add_isbn_identifiers(book, metadata_search.isbn_forms(isbn))
        book.save(update_fields=["updated_at"])
        return
    if field_name == "language":
        value = normalize_language(value) or value
    if field_name in {"title", "language", "publisher"}:
        value = intake._fit(Book, field_name, value)
    setattr(book, field_name, value)
    book.save(update_fields=[field_name, "updated_at"])


def set_field(
    book: Book,
    field_name: str,
    value: Any,
    *,
    source: str,
    job: ScanJob | None = None,
    opf: ScanJobOpf | None = None,
    user: Any = None,
    details: dict[str, Any] | None = None,
    replaces: Any = None,
) -> None:
    """Write one field and its provenance row.

    ``replaces`` (ISBN only) lists ISBNs this write supersedes; they are removed from the
    Book. Other ISBNs it carries are kept.
    """
    previous = book_value(book, field_name)
    _write_field(book, field_name, value, replaces)
    _record(
        book,
        field_name=field_name,
        source=source,
        action=BookMetadataProvenance.Action.SET,
        value=value,
        previous_value=previous,
        details=details,
        job=job,
        opf=opf,
        user=user,
    )


def record_reviewer_edits(book: Book, before: dict[str, Any], *, user: Any) -> list[str]:
    """Record every tracked field a person changed by hand (the admin form).

    Such a field is never overwritten by a later scanner OPF.
    """
    changed = []
    for field_name in FIELDS:
        after = book_value(book, field_name)
        if after == before.get(field_name):
            continue
        _record(
            book,
            field_name=field_name,
            source=BookMetadataProvenance.Source.REVIEWER,
            action=BookMetadataProvenance.Action.SET,
            value=after,
            previous_value=before.get(field_name),
            details={"via": "admin"},
            user=user,
        )
        changed.append(field_name)
    return changed


def _isbn_owner_elsewhere(book: Book, isbn: str) -> bool:
    forms = metadata_search.isbn_forms(isbn)
    return (
        Identifier.objects.filter(kind__in=_ISBN_KINDS)
        .filter(Q(value__in=forms) | Q(normalized_value__in=forms))
        .exclude(book=book)
        .exists()
    )


def apply_opf_to_book(book: Book, opf: ScanJobOpf, job: ScanJob) -> dict[str, Any]:
    """Apply an OPF version to a linked Book, field by field.

    * empty Book field -> filled from the OPF;
    * field the scanner set last and nobody changed since -> updated;
    * the same value already -> nothing;
    * anything else (a reviewer edit, a value from another source, or an ISBN another
      Book carries) -> not touched; the conflict opens a review task.
    """
    fields = opf.fields if isinstance(opf.fields, dict) else {}
    updated: list[str] = []
    conflicts: list[dict[str, Any]] = []
    for field_name in FIELDS:
        wanted = opf_value(fields, field_name)
        if wanted is None:
            continue
        current = book_value(book, field_name)
        if same_value(field_name, wanted, current):
            continue
        owner = latest_set(book, field_name)
        if field_name == "isbn" and _isbn_owner_elsewhere(book, wanted[0]):
            owned = False
        elif _is_empty(current):
            owned = True
        else:
            owned = (
                owner is not None
                and owner.source == PROVENANCE_SOURCE
                and same_value(field_name, owner.value, current)
            )
        if owned:
            set_field(
                book,
                field_name,
                wanted,
                source=PROVENANCE_SOURCE,
                job=job,
                opf=opf,
                details={"opf_version": opf.version},
                replaces=owner.value if owner is not None and field_name == "isbn" else None,
            )
            updated.append(field_name)
            continue
        conflicts.append(
            {
                "field": field_name,
                "scanner": wanted,
                "current": current,
                "current_source": owner.source if owner is not None else "library",
            }
        )
        _record(
            book,
            field_name=field_name,
            source=PROVENANCE_SOURCE,
            action=BookMetadataProvenance.Action.CONFLICT,
            value=wanted,
            previous_value=current,
            details={"opf_version": opf.version, "kept": "current"},
            job=job,
            opf=opf,
        )
    task = _open_field_conflict_review(job, book, opf, conflicts) if conflicts else None
    application = {
        "book_id": book.pk,
        "applied_at": timezone.now().isoformat(),
        "updated": updated,
        "conflicts": [conflict["field"] for conflict in conflicts],
        "review_task_id": task.pk if task is not None else None,
    }
    opf.application = application
    opf.save(update_fields=["application"])
    logger.info(
        "scanner_opf_applied",
        scan_job_id=str(job.pk),
        book_id=book.pk,
        version=opf.version,
        updated=updated,
        conflicts=application["conflicts"],
    )
    return application


# --- finish: linking (§4.5) ------------------------------------------------------------


def books_with_isbn(isbn: str) -> list[Book]:
    forms = metadata_search.isbn_forms(isbn)
    if not forms:
        return []
    book_ids = (
        Identifier.objects.filter(kind__in=_ISBN_KINDS)
        .filter(Q(value__in=forms) | Q(normalized_value__in=forms))
        .values_list("book_id", flat=True)
        .distinct()
    )
    return list(Book.objects.filter(pk__in=list(book_ids)).order_by("pk"))


def _author_key(names: Iterable[str]) -> frozenset[str]:
    return frozenset(
        normalized for name in names if (normalized := identification.normalize_text(name))
    )


def duplicate_books(title: str, authors: list[str]) -> list[Book]:
    """Books with the same normalised title and the same author set (a likely duplicate)."""
    wanted_title = identification.normalize_text(title)
    wanted_authors = _author_key(authors)
    matches = []
    for book in Book.objects.filter(title__iexact=title.strip()).prefetch_related("authors"):
        if identification.normalize_text(book.title) != wanted_title:
            continue
        if _author_key(author.full_name for author in book.authors.all()) == wanted_authors:
            matches.append(book)
    return matches


def create_book_from_opf(job: ScanJob, opf: ScanJobOpf, *, user: Any = None) -> Book:
    """A new Book from the OPF's fields, each recorded with provenance ``scanner_opf``."""
    fields = opf.fields if isinstance(opf.fields, dict) else {}
    isbn13 = fields.get("isbn")
    forms = metadata_search.isbn_forms(isbn13) if isbn13 else []
    book = intake._provision_book_from_manual_metadata(
        title=fields["title"],
        authors=list(fields.get("authors") or []),
        publisher=fields.get("publisher"),
        language=fields.get("language"),
        description=fields.get("description"),
        isbn13=next((value for value in forms if len(value) == 13), None),
        isbn10=next((value for value in forms if len(value) == 10), None),
    )
    for field_name in FIELDS:
        value = opf_value(fields, field_name)
        if value is None:
            continue
        _record(
            book,
            field_name=field_name,
            source=PROVENANCE_SOURCE,
            action=BookMetadataProvenance.Action.SET,
            value=value,
            details={"opf_version": opf.version, "created": True},
            job=job,
            opf=opf,
            user=user,
        )
    return book


def _hand_to_identification(
    job: ScanJob, opf: ScanJobOpf, *, reason: str, book_ids: list[int] | None = None
) -> None:
    """Leave linking to scan identification and its review, with the OPF as evidence."""
    fields = opf.fields if isinstance(opf.fields, dict) else {}
    notes = dict(job.notes) if isinstance(job.notes, dict) else {}
    notes[NOTES_KEY] = {
        "reason": reason,
        "opf_version": opf.version,
        "title": fields.get("title"),
        "authors": list(fields.get("authors") or []),
        "isbn": fields.get("isbn"),
        "language": fields.get("language"),
        "possible_duplicate_book_ids": list(book_ids or []),
    }
    job.notes = notes
    update = ["notes"]
    if fields.get("title") and not job.provisional_title:
        job.provisional_title = fields["title"]
        update.append("provisional_title")
    if fields.get("authors") and not job.provisional_authors:
        job.provisional_authors = list(fields["authors"])
        update.append("provisional_authors")
    job.save(update_fields=[*update, "updated_at"])
    logger.info("scanner_opf_needs_review", scan_job_id=str(job.pk), reason=reason)


def link_book_from_opf(job: ScanJob) -> Book | None:
    """Finish-time book linker (registered with :mod:`.scan_lifecycle`)."""
    with transaction.atomic():
        locked = ScanJob.objects.select_for_update().get(pk=job.pk)
        if locked.book_id is not None:
            job.book_id = locked.book_id
            return locked.book
        opf = effective_opf(locked)
        if opf is None:
            return None
        fields = opf.fields if isinstance(opf.fields, dict) else {}
        title = fields.get("title")
        if not title:
            _hand_to_identification(locked, opf, reason="no_title")
            return None
        isbn = fields.get("isbn")
        path: str
        if isbn:
            matches = books_with_isbn(isbn)
            if len(matches) > 1:
                _hand_to_identification(
                    locked, opf, reason="ambiguous_isbn", book_ids=[b.pk for b in matches]
                )
                return None
            if matches:
                book = Book.objects.select_for_update().get(pk=matches[0].pk)
                path = "isbn_match"
            else:
                book = create_book_from_opf(locked, opf)
                path = "created"
        else:
            duplicates = duplicate_books(title, list(fields.get("authors") or []))
            if duplicates:
                _hand_to_identification(
                    locked, opf, reason="possible_duplicate", book_ids=[b.pk for b in duplicates]
                )
                return None
            book = create_book_from_opf(locked, opf)
            path = "created"
        locked.book = book
        notes = dict(locked.notes) if isinstance(locked.notes, dict) else {}
        notes.pop(NOTES_KEY, None)
        notes["book_link"] = {"source": "scanner_opf", "path": path, "opf_version": opf.version}
        locked.notes = notes
        locked.save(update_fields=["book", "notes", "updated_at"])
        if path == "isbn_match":
            apply_opf_to_book(book, opf, locked)
        request_provider_check(job=locked, book=book, opf=opf)
    job.book = book
    job.notes = locked.notes
    logger.info("scanner_opf_linked", scan_job_id=str(job.pk), book_id=book.pk, path=path)
    return book


# --- background provider check (owner decision, see module docstring) -------------------


def request_provider_check(*, job: ScanJob, book: Book, opf: ScanJobOpf) -> ProcessingJob:
    """Queue the external check of ``opf`` after the surrounding transaction commits."""
    processing_job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.SCANNER_OPF_CHECK,
        details={
            "scan_job_id": str(job.pk),
            "book_id": book.pk,
            "opf_id": opf.pk,
            "opf_version": opf.version,
            "attempt_count": 0,
            "error": "",
        },
    )
    job_id = str(processing_job.job_id)
    transaction.on_commit(lambda: _dispatch_provider_check(job_id))
    return processing_job


def _dispatch_provider_check(job_id: str) -> None:
    try:
        queue = get_queue()
        if queue is None:
            process_provider_check_job(job_id)
        else:
            queue.enqueue(process_provider_check_job, job_id, raise_on_error=True)
    except Exception as exc:  # the check must never break the request that queued it
        logger.warning(
            "scanner_opf_check_dispatch_failed", job_id=job_id, error_type=type(exc).__name__
        )
        ProcessingJob.objects.filter(
            job_id=job_id, state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING]
        ).update(state=ProcessingJob.JobState.FAILED, finished_at=timezone.now())


def _candidate_isbns(candidate: identification.IdentificationCandidate) -> set[str]:
    """ISBN-13 forms the provider record itself carries (not ones echoed from the query)."""
    found = set(metadata_search.record_isbns(candidate.raw))
    for value in candidate.edition_isbns:
        if (canonical := metadata_search.canonical_isbn13(value)) is not None:
            found.add(canonical)
    return found


def _candidate_summary(candidate: identification.IdentificationCandidate) -> dict[str, Any]:
    return {
        "provider_id": candidate.provider_id,
        "provider_ref": candidate.provider_ref,
        "title": candidate.title,
        "authors": list(candidate.authors),
        "isbn13": candidate.isbn13,
        "isbn10": candidate.isbn10,
        "score": round(float(candidate.score), 4),
    }


def _agrees(fields: dict[str, Any], candidate: identification.IdentificationCandidate) -> bool:
    title = fields.get("title")
    if title and candidate.title and not identification.titles_agree(title, candidate.title):
        return False
    return not identification.authors_conflict(
        list(candidate.authors), list(fields.get("authors") or [])
    )


def assess_provider_result(
    fields: dict[str, Any],
    candidates: list[identification.IdentificationCandidate],
    outcomes: list[identification.ProviderOutcome],
) -> dict[str, Any]:
    """Classify a provider lookup of the OPF's title, authors and ISBN.

    Records whose own identifiers carry the OPF's ISBN are authoritative for that ISBN;
    without such a record, only candidates at or above the auto-accept score count.
    ``confirmed``: one of them agrees with the OPF's title and authors. ``contradicted``:
    there are such records and none agrees (e.g. the ISBN's record names another book).
    ``no_results`` / ``provider_error`` / ``inconclusive`` (only weak matches) keep the
    scanner metadata as it is.
    """
    result: dict[str, Any] = {
        "checked_at": timezone.now().isoformat(),
        "providers": [outcome.as_dict() for outcome in outcomes],
        "candidate_count": len(candidates),
        "threshold": identification.AUTO_ACCEPT_SCORE,
    }
    if not candidates:
        failed = bool(outcomes) and all(outcome.status == "error" for outcome in outcomes)
        result["status"] = CHECK_PROVIDER_ERROR if failed else CHECK_NO_RESULTS
        return result
    isbn = fields.get("isbn")
    isbn_records = [c for c in candidates if isbn and isbn in _candidate_isbns(c)]
    pool = isbn_records or [c for c in candidates if c.score >= identification.AUTO_ACCEPT_SCORE]
    result["basis"] = "isbn_record" if isbn_records else "score"
    if not pool:
        result["status"] = CHECK_INCONCLUSIVE
        result["best"] = _candidate_summary(candidates[0])
        return result
    agreeing = [c for c in pool if _agrees(fields, c)]
    if agreeing:
        result["status"] = CHECK_CONFIRMED
        result["record"] = _candidate_summary(agreeing[0])
    else:
        result["status"] = CHECK_CONTRADICTED
        result["record"] = _candidate_summary(pool[0])
    return result


def _signals(fields: dict[str, Any]) -> identification.IdentificationInput:
    title = fields.get("title")
    return identification.IdentificationInput(
        title_hints=[title] if title else [],
        author_hints=list(fields.get("authors") or []),
        isbns=identification.normalize_isbns(fields.get("isbn")) if fields.get("isbn") else [],
        folder_hints=[],
        language=fields.get("language"),
        publisher=fields.get("publisher"),
        description=None,
    )


def _fence(job: ScanJob, opf_id: Any, book_id: Any) -> dict[str, Any] | None:
    """Why a result for OPF ``opf_id`` on Book ``book_id`` no longer applies to ``job``.

    ``None`` while that OPF is still the job's effective metadata and the job is still
    linked to that Book. The caller holds the job's row lock.
    """
    effective = effective_opf(job)
    if effective is None or effective.pk != opf_id:
        return {
            "reason": "opf_superseded",
            "effective_opf_id": effective.pk if effective is not None else None,
            "effective_opf_version": effective.version if effective is not None else None,
        }
    if job.book_id != book_id:
        return {"reason": "book_changed", "linked_book_id": job.book_id}
    return None


def _finish_stale(
    processing_job: ProcessingJob,
    details: dict[str, Any],
    stale: dict[str, Any],
    result: dict[str, Any] | None,
) -> None:
    """Keep a check that no longer applies as history only: nothing is published."""
    processing_job.state = ProcessingJob.JobState.SUCCESS
    processing_job.finished_at = timezone.now()
    processing_job.details = {**details, "result": result, "stale": stale, "published": False}
    processing_job.save(update_fields=["state", "finished_at", "details"])
    logger.info(
        "scanner_opf_check_stale",
        job_id=str(processing_job.job_id),
        opf_id=details.get("opf_id"),
        reason=stale.get("reason"),
        status=result.get("status") if result is not None else None,
    )


def process_provider_check_job(job_id: str, *, raise_on_error: bool = False) -> None:
    """Run one provider check (worker entry point).

    The check is fenced to the OPF version and Book it was requested for: it publishes
    (provenance, ``ScanJobOpf.provider_check``, a review task) only if, under the job's
    row lock, that version is still the effective OPF and the job is still linked to that
    Book. Otherwise the result is kept on the processing job as stale history and nothing
    else changes - a delayed worker never replaces newer evidence.
    """
    with transaction.atomic():
        processing_job = (
            ProcessingJob.objects.select_for_update()
            .filter(job_id=job_id, job_type=ProcessingJob.JobType.SCANNER_OPF_CHECK)
            .first()
        )
        if processing_job is None or processing_job.state != ProcessingJob.JobState.QUEUED:
            return
        details = dict(processing_job.details) if isinstance(processing_job.details, dict) else {}
        details["attempt_count"] = int(details.get("attempt_count", 0)) + 1
        processing_job.state = ProcessingJob.JobState.RUNNING
        processing_job.started_at = timezone.now()
        processing_job.details = details
        processing_job.save(update_fields=["state", "started_at", "details"])
    try:
        opf = ScanJobOpf.objects.get(pk=details["opf_id"])
        with transaction.atomic():
            job = ScanJob.objects.select_for_update().get(pk=opf.scan_job_id)
            stale = _fence(job, opf.pk, details.get("book_id"))
            if stale is not None:  # superseded before the lookup: spare the providers
                _finish_stale(processing_job, details, stale, None)
                return
        fields = opf.fields if isinstance(opf.fields, dict) else {}
        candidates, outcomes = identification.resolve_candidates_with_report(_signals(fields))
        result = assess_provider_result(fields, candidates, outcomes)
        result["job_id"] = job_id
        with transaction.atomic():
            # Lock order everywhere: scan job, Book, review tasks.
            job = ScanJob.objects.select_for_update().get(pk=opf.scan_job_id)
            stale = _fence(job, opf.pk, details.get("book_id"))
            book = Book.objects.select_for_update().filter(pk=details.get("book_id")).first()
            if stale is None and book is None:
                stale = {"reason": "book_deleted"}
            if stale is not None or book is None:
                _finish_stale(processing_job, details, stale or {}, result)
                return
            task = None
            if result["status"] == CHECK_CONTRADICTED:
                task = _open_provider_conflict_review(job, book, opf, result)
                result["review_task_id"] = task.pk
            opf.provider_check = result
            opf.save(update_fields=["provider_check"])
            _record(
                book,
                source=BookMetadataProvenance.Source.PROVIDER_CHECK,
                action=BookMetadataProvenance.Action.PROVIDER_CHECK,
                value=result["status"],
                details=result,
                job=job,
                opf=opf,
            )
            processing_job.state = ProcessingJob.JobState.SUCCESS
            processing_job.finished_at = timezone.now()
            processing_job.details = {**details, "result": result}
            processing_job.save(update_fields=["state", "finished_at", "details"])
        logger.info(
            "scanner_opf_checked",
            scan_job_id=str(job.pk),
            book_id=book.pk,
            status=result["status"],
            candidate_count=result["candidate_count"],
        )
    except Exception as exc:
        logger.warning("scanner_opf_check_failed", job_id=job_id, error_type=type(exc).__name__)
        ProcessingJob.objects.filter(pk=processing_job.pk).update(
            state=ProcessingJob.JobState.FAILED,
            finished_at=timezone.now(),
            details={**details, "error": f"{type(exc).__name__}: {exc}"[:500]},
        )
        if raise_on_error:
            raise


# --- review tasks ---------------------------------------------------------------------


def _review_payload(task: ReviewTask) -> dict[str, Any] | None:
    payload = task.decision_payload if isinstance(task.decision_payload, dict) else {}
    value = payload.get(REVIEW_KEY)
    return value if isinstance(value, dict) else None


def review_fields(review: dict[str, Any]) -> tuple[str, ...]:
    """The Book fields a scanner-OPF review shows and may write."""
    if review.get("kind") == KIND_PROVIDER_CONFLICT:
        return CHECKED_FIELDS
    return tuple(
        str(conflict.get("field"))
        for conflict in review.get("conflicts") or []
        if conflict.get("field") in FIELD_LABELS
    )


def review_revision(review: dict[str, Any]) -> str:
    """A fingerprint of the evidence a reviewer sees (OPF version, Book revision, values).

    The review form sends it back; resolving is rejected when it no longer matches.
    """
    return _digest({k: v for k, v in review.items() if k not in {"choice", "obsolete"}})[:32]


def _mark_obsolete(task: ReviewTask, stale: dict[str, Any], *, note: str) -> None:
    """Close a review whose OPF version or Book no longer applies; its evidence stays."""
    payload = dict(task.decision_payload) if isinstance(task.decision_payload, dict) else {}
    review = dict(payload.get(REVIEW_KEY) or {})
    review["obsolete"] = {**stale, "at": timezone.now().isoformat()}
    payload[REVIEW_KEY] = review
    task.decision_payload = payload
    task.state = ReviewTask.State.CANCELLED
    task.decision_notes = note
    task.decided_at = timezone.now()
    task.save()


def supersede_open_reviews(job: ScanJob, opf: ScanJobOpf) -> list[int]:
    """Cancel unclaimed scanner-OPF conflict reviews of other OPF versions of ``job``.

    A *claimed* review is left untouched (someone is looking at its evidence); resolving
    it is rejected later because its OPF version is no longer the effective one.
    """
    cancelled = []
    for task in ReviewTask.objects.select_for_update().filter(
        scan_job=job, state=ReviewTask.State.OPEN
    ):
        review = _review_payload(task)
        if review is None or review.get("kind") not in CONFLICT_KINDS:
            continue
        if review.get("opf_id") == opf.pk:
            continue
        _mark_obsolete(
            task,
            {
                "reason": "opf_superseded",
                "effective_opf_id": opf.pk,
                "effective_opf_version": opf.version,
            },
            note=f"Superseded by scanner metadata.opf version {opf.version}",
        )
        cancelled.append(task.pk)
    if cancelled:
        logger.info(
            "scanner_opf_reviews_superseded",
            scan_job_id=str(job.pk),
            version=opf.version,
            review_task_ids=cancelled,
        )
    return cancelled


def _owned_review(job: ScanJob, kind: str, opf: ScanJobOpf) -> ReviewTask | None:
    """The open or claimed review of ``kind`` for exactly this OPF version (locked)."""
    for task in ReviewTask.objects.select_for_update().filter(
        scan_job=job, state__in=_OPEN_REVIEW_STATES
    ):
        review = _review_payload(task)
        if review is not None and review.get("kind") == kind and review.get("opf_id") == opf.pk:
            return task
    return None


def _save_review(
    job: ScanJob,
    book: Book,
    opf: ScanJobOpf,
    review: dict[str, Any],
    *,
    provider_snapshot: list[dict[str, Any]] | None = None,
) -> ReviewTask:
    """Create, or refresh while still unclaimed, this OPF version's review of a kind.

    A claimed review is never modified: its evidence is what the reviewer is deciding on.
    The review records the Book revision it was opened against.
    """
    review = {**review, "book_revision": book_revision(book, review_fields(review))}
    fields = opf.fields if isinstance(opf.fields, dict) else {}
    values: dict[str, Any] = {
        "task_type": ReviewTask.TaskType.QUALITY_REVIEW,
        "created_by": job.operator,
        "uploader": job.operator,
        "source_filename": f"physical scan {job.pk}",
        "source_path_hint": f"scan-job:{job.pk}",
        "inferred_title": fields.get("title") or book.title,
        "inferred_authors": list(fields.get("authors") or []),
        "embedded_metadata": {"scanner_opf": fields, "opf_version": opf.version},
        "provider_snapshot": provider_snapshot or [],
        "decision_payload": {REVIEW_KEY: review},
        "resolved_book": book,
    }
    task = _owned_review(job, str(review["kind"]), opf)
    if task is None:
        return ReviewTask.objects.create(scan_job=job, **values)
    if task.state != ReviewTask.State.OPEN:
        logger.info(
            "scanner_opf_review_kept_claimed",
            scan_job_id=str(job.pk),
            review_task_id=task.pk,
            version=opf.version,
        )
        return task
    for name, value in values.items():
        setattr(task, name, value)
    task.save()
    return task


def _open_field_conflict_review(
    job: ScanJob, book: Book, opf: ScanJobOpf, conflicts: list[dict[str, Any]]
) -> ReviewTask:
    task = _save_review(
        job,
        book,
        opf,
        {
            "kind": KIND_FIELD_CONFLICT,
            "book_id": book.pk,
            "opf_id": opf.pk,
            "opf_version": opf.version,
            # A reviewer's (or the library's) value is kept unless someone decides otherwise.
            "preselected": "current",
            "conflicts": conflicts,
        },
    )
    logger.info(
        "scanner_opf_conflict_review",
        scan_job_id=str(job.pk),
        book_id=book.pk,
        review_task_id=task.pk,
        fields=[conflict["field"] for conflict in conflicts],
    )
    return task


def _open_provider_conflict_review(
    job: ScanJob, book: Book, opf: ScanJobOpf, result: dict[str, Any]
) -> ReviewTask:
    fields = opf.fields if isinstance(opf.fields, dict) else {}
    record = result["record"]
    task = _save_review(
        job,
        book,
        opf,
        {
            "kind": KIND_PROVIDER_CONFLICT,
            "book_id": book.pk,
            "opf_id": opf.pk,
            "opf_version": opf.version,
            # D3: the scanner is authoritative; the reviewer only overrides it on purpose.
            "preselected": "scanner",
            "scanner": {
                "title": fields.get("title"),
                "authors": list(fields.get("authors") or []),
                "isbn": fields.get("isbn"),
            },
            "provider": record,
            "check_job_id": result.get("job_id"),
        },
        provider_snapshot=[record],
    )
    logger.info(
        "scanner_opf_provider_conflict",
        scan_job_id=str(job.pk),
        book_id=book.pk,
        review_task_id=task.pk,
        provider_id=record.get("provider_id"),
    )
    return task


def _display(field_name: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return str(value)


def review_context(task: ReviewTask) -> dict[str, Any] | None:
    """What the review page shows for a scanner-OPF task, or ``None`` for other tasks."""
    review = _review_payload(task)
    job = task.scan_job
    if review is None:
        if job is None or job.book_id is not None:
            return None
        notes = job.notes if isinstance(job.notes, dict) else {}
        handed = notes.get(NOTES_KEY)
        if not isinstance(handed, dict):
            return None
        duplicate_ids = [
            pk for pk in handed.get("possible_duplicate_book_ids") or [] if isinstance(pk, int)
        ]
        return {
            "kind": "identification",
            "is_conflict": False,
            "reason": handed.get("reason"),
            "opf": handed,
            "duplicates": list(Book.objects.filter(pk__in=duplicate_ids).order_by("title")),
            "can_create": bool(handed.get("title")) and task.state in _OPEN_REVIEW_STATES,
        }
    kind = review.get("kind")
    rows: list[dict[str, Any]] = []
    if kind == KIND_PROVIDER_CONFLICT:
        scanner = review.get("scanner") or {}
        provider = review.get("provider") or {}
        provider_isbn = provider.get("isbn13") or provider.get("isbn10")
        for field_name, scanner_value, other in (
            ("title", scanner.get("title"), provider.get("title")),
            ("authors", scanner.get("authors"), provider.get("authors")),
            ("isbn", scanner.get("isbn"), provider_isbn),
        ):
            rows.append(
                {
                    "field": field_name,
                    "label": FIELD_LABELS[field_name],
                    "scanner": _display(field_name, scanner_value),
                    "other": _display(field_name, other),
                }
            )
    elif kind == KIND_FIELD_CONFLICT:
        for conflict in review.get("conflicts") or []:
            field_name = conflict.get("field")
            if field_name not in FIELD_LABELS:
                continue
            rows.append(
                {
                    "field": field_name,
                    "label": FIELD_LABELS[field_name],
                    "scanner": _display(field_name, conflict.get("scanner")),
                    "other": _display(field_name, conflict.get("current")),
                    "other_source": conflict.get("current_source"),
                }
            )
    else:
        return None
    is_open = task.state in _OPEN_REVIEW_STATES
    obsolete = review.get("obsolete")
    if is_open and job is not None:
        # Shown before anyone tries: a newer OPF version or another Book link makes this
        # review's evidence obsolete (resolving it is rejected and closes it).
        obsolete = _fence(job, review.get("opf_id"), review.get("book_id"))
    return {
        "kind": kind,
        "is_conflict": True,
        "review": review,
        "rows": rows,
        "preselected": review.get("preselected"),
        "choice": review.get("choice"),
        "book": Book.objects.filter(pk=review.get("book_id")).first(),
        "open": is_open and obsolete is None,
        "obsolete": obsolete,
        "revision": review_revision(review),
    }


def _close_task(task: ReviewTask, *, book: Book, user: Any, choice: str, notes: str) -> None:
    payload = dict(task.decision_payload) if isinstance(task.decision_payload, dict) else {}
    review = dict(payload.get(REVIEW_KEY) or {})
    review["choice"] = choice
    payload[REVIEW_KEY] = review
    task.state = ReviewTask.State.RESOLVED
    task.decision = ReviewTask.Decision.METADATA_CONFLICT
    task.decision_payload = payload
    task.decision_notes = notes
    task.decided_at = timezone.now()
    task.resolved_book = book
    task.reviewer = user
    task.save()


CONFLICT_CHOICES: Final = {
    KIND_PROVIDER_CONFLICT: ("scanner", "provider"),
    KIND_FIELD_CONFLICT: ("current", "scanner"),
}


def _refresh_stale_review(
    task: ReviewTask, review: dict[str, Any], job: ScanJob, book: Book
) -> str | None:
    """``None`` if the review still applies; otherwise refresh it and say why it does not.

    The caller holds the job, Book and task row locks.
    """
    stale = _fence(job, review.get("opf_id"), book.pk)
    if stale is not None:
        if stale["reason"] == "opf_superseded":
            version = stale.get("effective_opf_version")
            why = (
                f"the scanner has since sent metadata.opf version {version}"
                if version is not None
                else "its scanner metadata.opf is no longer used"
            )
        else:
            why = "the scan is now linked to another book"
        _mark_obsolete(task, stale, note=f"Obsolete: {why}")
        logger.info(
            "scanner_opf_review_obsolete",
            review_task_id=task.pk,
            scan_job_id=str(job.pk),
            reason=stale["reason"],
        )
        return (
            f"This review is obsolete: it was opened for metadata.opf version "
            f"{review.get('opf_version')}, but {why}. Nothing was changed and the review "
            "was closed; the current metadata gets its own review if it needs one."
        )
    current_revision = book_revision(book, review_fields(review))
    if current_revision == review.get("book_revision"):
        return None
    payload = dict(task.decision_payload) if isinstance(task.decision_payload, dict) else {}
    refreshed = dict(review)
    refreshed["book_revision"] = current_revision
    if refreshed.get("kind") == KIND_FIELD_CONFLICT:
        refreshed["conflicts"] = [
            {**conflict, "current": book_value(book, str(conflict.get("field")))}
            if conflict.get("field") in FIELD_LABELS
            else conflict
            for conflict in refreshed.get("conflicts") or []
        ]
    payload[REVIEW_KEY] = refreshed
    task.decision_payload = payload
    task.save(update_fields=["decision_payload", "updated_at"])
    logger.info("scanner_opf_review_refreshed", review_task_id=task.pk, scan_job_id=str(job.pk))
    return (
        "The book's metadata changed after this review was opened. Nothing was changed; "
        "the review now shows the current values. Decide again."
    )


def resolve_conflict(
    task: ReviewTask, *, choice: str, user: Any, revision: str | None = None
) -> Book:
    """Apply a reviewer's decision on a scanner-OPF conflict task.

    Provider conflict: ``scanner`` keeps the scanner metadata (recorded as confirmed, so
    later OPFs still update it); ``provider`` writes the provider's title, authors and
    ISBN as reviewer edits. Field conflict: ``current`` keeps the Book's values;
    ``scanner`` writes the OPF values with scanner provenance.

    The decision is only applied to the evidence it was made on. :class:`StaleReview`
    (nothing written to the Book) when ``revision`` (what the reviewer loaded, see
    :func:`review_revision`) no longer matches the task; when the task's OPF version is
    no longer the effective one or the scan is linked to another Book (the task is then
    closed as obsolete); or when the Book's values for the reviewed fields changed since
    the task was opened (the task is refreshed with the current values to decide again).
    """
    stale_message: str | None = None
    with transaction.atomic():
        # Lock order everywhere: scan job, Book, review tasks.
        unlocked = ReviewTask.objects.filter(pk=task.pk).first()
        if unlocked is None:
            raise ScannerOpfError("the review task no longer exists")
        first_look = _review_payload(unlocked) or {}
        job = (
            ScanJob.objects.select_for_update().filter(pk=unlocked.scan_job_id).first()
            if unlocked.scan_job_id is not None
            else None
        )
        book = Book.objects.select_for_update().filter(pk=first_look.get("book_id")).first()
        locked = ReviewTask.objects.select_for_update().get(pk=task.pk)
        review = _review_payload(locked)
        if review is None or review.get("kind") not in CONFLICT_KINDS:
            raise ScannerOpfError("not a scanner metadata conflict")
        if locked.state not in _OPEN_REVIEW_STATES:
            raise ScannerOpfError("review task is already closed")
        kind = str(review["kind"])
        if choice not in CONFLICT_CHOICES[kind]:
            raise ScannerOpfError(f"choice must be one of: {', '.join(CONFLICT_CHOICES[kind])}")
        if job is None:
            raise ScannerOpfError("not a scan review")
        if book is None or review.get("book_id") != book.pk:
            raise ScannerOpfError("the book no longer exists")
        if revision is not None and revision != review_revision(review):
            raise StaleReview(
                "This review changed after you loaded it. Reload it and decide again."
            )
        stale_message = _refresh_stale_review(locked, review, job, book)
        if stale_message is None:
            opf = ScanJobOpf.objects.filter(pk=review.get("opf_id")).first()
            reviewer_source = BookMetadataProvenance.Source.REVIEWER
            details = {"review_task_id": locked.pk, "choice": choice}
            if kind == KIND_PROVIDER_CONFLICT and choice == "provider":
                provider = review.get("provider") or {}
                scanner = review.get("scanner") or {}
                new_isbn = metadata_search.canonical_isbn13(
                    provider.get("isbn13") or provider.get("isbn10")
                )
                values: dict[str, Any] = {
                    "title": provider.get("title"),
                    "authors": list(provider.get("authors") or []),
                    "isbn": [new_isbn] if new_isbn else None,
                }
                for field_name, value in values.items():
                    if _is_empty(value):
                        continue
                    set_field(
                        book,
                        field_name,
                        value,
                        source=reviewer_source,
                        job=job,
                        opf=opf,
                        user=user,
                        details=details,
                        replaces=[scanner["isbn"]]
                        if field_name == "isbn" and scanner.get("isbn")
                        else None,
                    )
            elif kind == KIND_FIELD_CONFLICT and choice == "scanner":
                for conflict in review.get("conflicts") or []:
                    field_name = conflict.get("field")
                    if field_name not in FIELD_LABELS:
                        continue
                    owner = latest_set(book, field_name)
                    set_field(
                        book,
                        field_name,
                        conflict.get("scanner"),
                        source=PROVENANCE_SOURCE,
                        job=job,
                        opf=opf,
                        user=user,
                        details={**details, "confirmed_by_reviewer": True},
                        replaces=(
                            owner.value if field_name == "isbn" and owner is not None else None
                        ),
                    )
            else:
                fields = (
                    CHECKED_FIELDS
                    if kind == KIND_PROVIDER_CONFLICT
                    else tuple(str(c.get("field")) for c in review.get("conflicts") or [])
                )
                for field_name in fields:
                    _record(
                        book,
                        field_name=field_name,
                        source=reviewer_source,
                        action=BookMetadataProvenance.Action.CONFIRMED,
                        value=book_value(book, field_name),
                        details=details,
                        job=job,
                        opf=opf,
                        user=user,
                    )
            _close_task(
                locked,
                book=book,
                user=user,
                choice=choice,
                notes=f"Scanner metadata conflict: kept {choice}",
            )
    if stale_message is not None:
        raise StaleReview(stale_message)
    return book


def create_book_from_review(task: ReviewTask, *, user: Any) -> Book:
    """ "Create a Book from the scanner metadata" on a review the finish handed over
    (ambiguous ISBN or likely duplicate): the reviewer has looked and decided."""
    job = task.scan_job
    if job is None:
        raise ScannerOpfError("not a scan review")
    with transaction.atomic():
        locked = ScanJob.objects.select_for_update().get(pk=job.pk)
        if locked.book_id is not None:
            raise ScannerOpfError("the scan job is already linked to a book")
        if task.state not in _OPEN_REVIEW_STATES:
            raise ScannerOpfError("review task is already closed")
        opf = effective_opf(locked)
        if opf is None or not (opf.fields or {}).get("title"):
            raise ScannerOpfError("the scan job has no scanner metadata with a title")
        book = create_book_from_opf(locked, opf, user=user)
        review_accept.resolve_review_source(
            task=task,
            book=book,
            reviewer=user,
            decision=cast(str, ReviewTask.Decision.MANUAL_ENTRY),
            decision_notes="Created from the scanner's metadata.opf",
            decision_payload={
                **(task.decision_payload if isinstance(task.decision_payload, dict) else {}),
                "resolution_source": "scanner_opf",
                "opf_version": opf.version,
            },
        )
        locked.refresh_from_db()
        notes = dict(locked.notes) if isinstance(locked.notes, dict) else {}
        notes.pop(NOTES_KEY, None)
        notes["book_link"] = {"source": "scanner_opf", "path": "review", "opf_version": opf.version}
        locked.notes = notes
        locked.save(update_fields=["notes", "updated_at"])
        request_provider_check(job=locked, book=book, opf=opf)
    return book


# --- registration ----------------------------------------------------------------------


def register() -> None:
    """Install the S3 hooks (called from ``CoreConfig.ready``)."""
    scan_manifest.register_manifest_source(OpfManifestSource())
    scan_lifecycle.register_raw_scan_annotator(annotate_raw_scan)
    scan_lifecycle.register_book_linker(link_book_from_opf)
