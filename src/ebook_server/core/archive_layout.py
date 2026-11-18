"""Configurable on-disk layout for files the application writes into the archive (#7).

Database identifiers stay authoritative: a ``FileVariant`` or ``CoverImage`` records its
``StorageLocation`` and storage identifier, and nothing reads a book's metadata back from
its path. The layout only decides where *new* files go, so a member browsing the share
finds them in a familiar, Calibre-like tree.

Layouts (``BOOK_SCANNER_ARCHIVE_LAYOUT``):

``author_title`` (default)
    ``<Author>/<Title>/``. Two different books with the same author and title get
    ``<Author>/<Title> (<book id>)/`` for the second one.
``calibre``
    ``<Author>/<Title> (<book id>)/``, Calibre's own naming; always unique.
``job``
    ``books/<import job id>/``, the original layout: one folder per import.
custom template
    ``/``-separated components using ``{author}``, ``{title}``, ``{book_id}``,
    ``{year}`` and ``{language}``, or ``{job_id}`` for a per-import layout.

Under a book-scoped layout every file of a book (each format, conversions, covers, OCR
output, cleaned scans) goes into one folder. The folder is assigned once and stored on
``Book.archive_folder``; correcting the title or author later does **not** move files
(paths are implementation details, and renaming folders on a shared NAS under members'
feet is surprising). New files keep joining the existing folder. Moving a book into the
folder its current metadata implies is an explicit admin reorganize.

A file archived while its import awaits review has no Book yet; it is held under
``_unsorted/<import job id>/`` and moved into the book folder when the review resolves.
"""

from __future__ import annotations

import string
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

import structlog
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction

from ebook_server.storage import (
    MAX_COMPONENT_BYTES,
    LocalFilesystemStorage,
    PublishMode,
    StorageBackend,
    safe_path_component,
)

from .models import Author, Book

if TYPE_CHECKING:
    from .models import ImportJob

logger = structlog.get_logger(__name__)

LAYOUT_PRESETS: dict[str, str] = {
    "author_title": "{author}/{title}",
    "calibre": "{author}/{title} ({book_id})",
    "job": "books/{job_id}",
}
DEFAULT_LAYOUT = "author_title"
BOOK_PLACEHOLDERS = frozenset({"author", "title", "book_id", "year", "language"})
JOB_PLACEHOLDER = "job_id"
# Files archived before their import is identified (review pending) wait here.
PENDING_NAMESPACE = "_unsorted"
# Per-value caps keep "<Author>/<Title> (<id>)" well inside common path limits.
_VALUE_MAX_BYTES = {"author": 80, "title": 100}
_FALLBACKS = {
    "author": "Unknown author",
    "title": "Unknown title",
    "year": "Unknown year",
    "language": "und",
}
_MAX_CLAIM_ATTEMPTS = 6


@dataclass(frozen=True)
class ArchiveLayout:
    """A validated archive layout template."""

    name: str
    template: str

    @property
    def job_scoped(self) -> bool:
        """True for per-import layouts (``books/<job_id>``) that have no book folder."""
        return f"{{{JOB_PLACEHOLDER}}}" in self.template


def _template_fields(template: str) -> list[str]:
    fields: list[str] = []
    for _literal, field, spec, conversion in string.Formatter().parse(template):
        if field is None:
            continue
        if spec or conversion or not field.isidentifier():
            raise ImproperlyConfigured(
                f"archive layout {template!r}: use plain placeholders like {{title}}",
            )
        fields.append(field)
    return fields


def validate_template(template: str) -> None:
    """Raise ``ImproperlyConfigured`` unless ``template`` is a safe relative layout."""
    if not template or "\\" in template or template.startswith("/"):
        raise ImproperlyConfigured(f"archive layout {template!r} must be a relative '/' path")
    parts = template.split("/")
    if any(part.strip() in {"", ".", ".."} for part in parts):
        raise ImproperlyConfigured(f"archive layout {template!r} has an empty or '..' component")
    if parts[0] == PENDING_NAMESPACE:
        raise ImproperlyConfigured(f"archive layout must not start with {PENDING_NAMESPACE!r}")
    fields = set(_template_fields(template))
    allowed = BOOK_PLACEHOLDERS | {JOB_PLACEHOLDER}
    unknown = fields - allowed
    if unknown:
        raise ImproperlyConfigured(
            f"archive layout {template!r} uses unknown placeholders: {sorted(unknown)}",
        )
    if not fields:
        raise ImproperlyConfigured(f"archive layout {template!r} has no placeholder")
    if JOB_PLACEHOLDER in fields and fields & BOOK_PLACEHOLDERS:
        raise ImproperlyConfigured(
            f"archive layout {template!r}: {{job_id}} cannot be combined with book fields",
        )


def configured_layout() -> ArchiveLayout:
    """The layout selected by ``BOOK_SCANNER_ARCHIVE_LAYOUT`` (validated)."""
    raw = str(getattr(settings, "BOOK_SCANNER_ARCHIVE_LAYOUT", "") or DEFAULT_LAYOUT).strip()
    template = LAYOUT_PRESETS.get(raw, raw)
    validate_template(template)
    name = raw if raw in LAYOUT_PRESETS else "custom"
    return ArchiveLayout(name=name, template=template)


def first_author_name(book: Book) -> str:
    """The book's first author in the order they were attached (provider order)."""
    through = Book.authors.through
    author_id = (
        through.objects.filter(book_id=book.pk).order_by("id").values_list("author_id", flat=True)
    ).first()
    if author_id is None:
        return ""
    return str(
        Author.objects.filter(pk=author_id).values_list("full_name", flat=True).first() or ""
    )


def _book_values(book: Book) -> dict[str, str]:
    year = book.published_year
    raw = {
        "author": first_author_name(book),
        "title": book.title,
        "book_id": str(book.pk),
        "year": str(year) if year else "",
        "language": book.language,
    }
    return {
        key: safe_path_component(
            value,
            fallback=_FALLBACKS.get(key, "Unknown"),
            max_bytes=_VALUE_MAX_BYTES.get(key, MAX_COMPONENT_BYTES),
        )
        for key, value in raw.items()
    }


def _render(template: str, values: dict[str, str]) -> str:
    components = [
        safe_path_component(part.format(**values), fallback="Unknown")
        for part in template.split("/")
    ]
    return "/".join(components)


def render_book_folder(book: Book, layout: ArchiveLayout | None = None) -> str:
    """The folder ``book``'s current metadata implies (ignores the stored folder)."""
    layout = layout or configured_layout()
    if layout.job_scoped:
        raise ValueError("a per-import archive layout has no book folder")
    return _render(layout.template, _book_values(book))


def book_file_stem(book: Book) -> str:
    """Calibre-style file name stem: ``<Title> - <Author>``."""
    values = _book_values(book)
    return safe_path_component(f"{values['title']} - {values['author']}", fallback="book")


def _folder_candidates(preferred: str, book_id: int) -> Iterator[str]:
    yield preferred
    head, _, last = preferred.rpartition("/")
    prefix = f"{head}/" if head else ""
    for attempt in range(_MAX_CLAIM_ATTEMPTS):
        token = f"{book_id}" if attempt == 0 else f"{book_id}-{uuid.uuid4().hex[:6]}"
        suffix = f" ({token})"
        if attempt == 0 and last.endswith(suffix):
            continue
        base = safe_path_component(last, max_bytes=MAX_COMPONENT_BYTES - len(suffix))
        yield f"{prefix}{base}{suffix}"


def _folder_in_use(candidate: str, book: Book, storage: StorageBackend) -> bool:
    if Book.objects.filter(archive_folder__iexact=candidate).exclude(pk=book.pk).exists():
        return True
    # A folder no Book claims (a legacy pile, a stray copy) is not ours to mix into.
    return storage.exists(candidate)


def book_folder(book: Book, storage: StorageBackend) -> str:
    """Return the archive folder for ``book``, assigning it on first use.

    The assignment is collision-proof: a folder another Book already owns (compared
    case-insensitively, as SMB shares do) or that already exists on storage is never
    reused; the Book id is appended instead. A unique constraint settles races between
    concurrent imports of two same-titled books.
    """
    layout = configured_layout()
    with transaction.atomic():
        locked = Book.objects.select_for_update().get(pk=book.pk)
        if locked.archive_folder:
            book.archive_folder = locked.archive_folder
            return locked.archive_folder
        for candidate in _folder_candidates(render_book_folder(locked, layout), locked.pk):
            if _folder_in_use(candidate, locked, storage):
                continue
            try:
                with transaction.atomic():
                    Book.objects.filter(pk=locked.pk).update(archive_folder=candidate)
            except IntegrityError:
                continue
            book.archive_folder = candidate
            logger.info("archive_folder_assigned", book_id=locked.pk, layout=layout.name)
            return candidate
    raise RuntimeError(f"could not assign a unique archive folder to book {book.pk}")


def proposed_book_folder(book: Book, storage: StorageBackend) -> str:
    """The folder a reorganize would give ``book`` now, without claiming it.

    Its current folder is kept when that is already the folder (or the disambiguated
    form of the folder) its metadata implies.
    """
    current = (book.archive_folder or "").lower()
    for candidate in _folder_candidates(render_book_folder(book), book.pk):
        if candidate.lower() == current:
            return book.archive_folder
        if not _folder_in_use(candidate, book, storage):
            return candidate
    raise RuntimeError(f"no free archive folder for book {book.pk}")


def claim_planned_book_folder(book: Book, folder: str, storage: StorageBackend) -> bool:
    """Make ``folder`` (taken from a reviewed reorganize plan) ``book``'s folder.

    Claims exactly the planned folder, never a substitute: returns True when the book
    already owns it or it was free and is now assigned, False when another Book owns
    it or an unowned folder appeared there since the plan (the plan is then stale).
    Files are not moved here; the caller moves them.
    """
    with transaction.atomic():
        locked = Book.objects.select_for_update().get(pk=book.pk)
        current = locked.archive_folder or ""
        if current.lower() == folder.lower():
            book.archive_folder = current
            return True
        if _folder_in_use(folder, locked, storage):
            return False
        try:
            with transaction.atomic():
                Book.objects.filter(pk=locked.pk).update(archive_folder=folder)
        except IntegrityError:
            return False
    book.archive_folder = folder
    logger.info("archive_folder_reassigned", book_id=book.pk)
    return True


def pending_destination(storage: StorageBackend, job: ImportJob) -> str:
    """Storage id for an original archived before its Book is known.

    The namespace belongs to this import alone (``books/<job id>/`` under the ``job``
    layout, ``_unsorted/<job id>/`` otherwise), so the id needs no reservation: only
    this job ever writes there, and a retry of the job replaces its own file.
    ``place_pending_original`` moves it into the book folder once the Book is settled.
    """
    layout = configured_layout()
    namespace = (
        layout.template.format(job_id=job.id)
        if layout.job_scoped
        else f"{PENDING_NAMESPACE}/{job.id}"
    )
    return storage.generate_destination(namespace, job.normalized_filename)


def _original_filename(job: ImportJob, book: Book) -> str:
    suffix = Path(job.normalized_filename).suffix or (
        f".{job.detected_format}" if job.detected_format else ""
    )
    return f"{book_file_stem(book)}{suffix}"


def publish_original(
    storage: StorageBackend,
    job: ImportJob,
    book: Book,
    source: str,
    *,
    mode: PublishMode = "copy",
) -> str:
    """Archive an imported original of ``book`` and return its storage id.

    Under a book-scoped layout the file joins the shared book folder; the name is
    allocated and written in one exclusive step (``publish_unique``), so two imports
    or conversions racing for ``<Title> - <Author>.epub`` get distinct files and
    neither overwrites the other. Under the ``job`` layout it goes to the import's own
    ``books/<job id>/`` folder.
    """
    if configured_layout().job_scoped:
        destination = pending_destination(storage, job)
        if mode == "link":
            raise ValueError("a per-import layout has no book folder to link into")
        if mode == "move":
            storage.move(source, destination)
        else:
            storage.store(source, destination)
        return destination
    return storage.publish_unique(
        source, book_folder(book, storage), _original_filename(job, book), mode=mode
    )


def publish_derived(
    storage: StorageBackend,
    book: Book,
    source: str,
    filename: str,
    *,
    legacy_namespace: str,
    legacy_filename: str | None = None,
) -> str:
    """Store a file derived from a book (conversion, cover, OCR, cleaned scan).

    Under a book-scoped layout it lands in the book's folder; allocation and the write
    are one exclusive step, so concurrent jobs producing the same ``output.pdf`` each
    get their own file and an existing file is never overwritten. Under the ``job``
    layout it keeps the original ``books/<book id>/<kind>/`` namespace and naming
    (``legacy_namespace``, ``legacy_filename``), which callers make unique per job.
    Returns the storage id written.
    """
    if configured_layout().job_scoped:
        destination = storage.generate_destination(legacy_namespace, legacy_filename or filename)
        storage.store(source, destination)
        return destination
    return storage.publish_unique(source, book_folder(book, storage), filename)


def is_pending_identifier(storage_identifier: str) -> bool:
    """True for a file archived while its import was awaiting review."""
    parts = PurePosixPath(storage_identifier).parts
    return len(parts) > 1 and parts[0] == PENDING_NAMESPACE


def place_pending_original(
    job: ImportJob,
    book: Book,
    storage: LocalFilesystemStorage,
) -> None:
    """Move an original archived under ``_unsorted/`` into ``book``'s folder.

    Runs when identification or a review settles the Book, possibly inside a database
    transaction that may still roll back. So the file is first *hard-linked* into the
    book folder (instant, same bytes, same share) and the ``_unsorted`` name is removed
    only after the transaction commits. A rollback leaves the recorded ``_unsorted``
    path valid (plus a harmless extra link). Where hard links are not supported, or
    anything else fails, the file simply stays under ``_unsorted``. ``storage`` is the
    backend of ``job.storage_location``.
    """
    location = job.storage_location
    source = job.storage_identifier
    if (
        location is None
        or location.read_only
        or not source
        or not is_pending_identifier(source)
        or configured_layout().job_scoped
    ):
        return
    try:
        destination = publish_original(storage, job, book, source, mode="link")
    except (OSError, ValueError, RuntimeError) as exc:
        logger.warning(
            "archive_pending_move_failed",
            import_job_id=str(job.id),
            book_id=book.pk,
            error_type=type(exc).__name__,
        )
        return
    job.storage_identifier = destination

    def _drop_pending_name() -> None:
        try:
            storage.delete(source)
            storage.prune_empty_parents(source)
        except OSError:
            logger.warning("archive_pending_cleanup_failed", import_job_id=str(job.id))

    transaction.on_commit(_drop_pending_name)
    logger.info("archive_pending_moved", import_job_id=str(job.id), book_id=book.pk)
