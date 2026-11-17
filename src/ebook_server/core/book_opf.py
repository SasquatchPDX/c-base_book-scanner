"""A Calibre-compatible ``metadata.opf`` in each book's archive folder (#7).

The OPF is a **derived artifact**. The database stays authoritative, and the file is
never edited by hand: whenever canonical Book metadata changes (title, subtitle,
authors, identifiers, publisher, publication date, language, description, genres and
categories, covers) the book is marked dirty (``Book.opf_dirty_at``) and one
background job per book rewrites the file. Marking a book that is already dirty does
not queue a second job, so a burst of edits (a review that sets the title, the
authors and three identifiers) produces one write. A job that finds the marker moved
while it was writing runs again, so the last edit always reaches the file.

The file is written atomically through the storage service (temporary file in the
same folder, then rename), into the book folder on every writable location that holds
one of the book's files. It is **never** written:

- under the per-import ``job`` layout (there is no book folder),
- when ``BOOK_SCANNER_WRITE_BOOK_OPF`` is false,
- on read-only, indexed-in-place locations (those folders belong to their share), or
- over a ``metadata.opf`` this application did not generate.

Every generated file carries ``<meta name="ebook-server:generated" content="true"/>``.
Intake's OPF-sidecar reader must skip such files (``is_generated_book_opf``): they
restate our own database, so they are not external evidence on a re-import.

The content follows what Calibre writes (OPF 2.0, Dublin Core): ``dc:title``,
``dc:creator`` with ``opf:role="aut"`` and ``opf:file-as``, ``dc:identifier`` with
``opf:scheme`` (``uuid``, ``ISBN``, ...), ``dc:language``, ``dc:publisher``, ``dc:date``
(the year alone when only the year is known), ``dc:description``, ``dc:subject``,
``calibre:title_sort`` and a ``cover`` guide reference when the cover file is in the
same folder.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from functools import partial
from pathlib import PurePosixPath
from typing import Any
from urllib.parse import quote
from xml.sax.saxutils import escape, quoteattr

import structlog
from defusedxml import ElementTree as DefusedET
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from ebook_server import jobs
from ebook_server.storage import LocalFilesystemStorage, ReadOnlyFilesystemStorage

from . import archive_layout
from .models import (
    Author,
    Book,
    Category,
    CoverImage,
    FileVariant,
    Genre,
    Identifier,
    ScanLog,
    StorageLocation,
)
from .storage_registry import StorageConfigurationError, filesystem_backend_for_location

logger = structlog.get_logger(__name__)

OPF_FILENAME = "metadata.opf"
GENERATED_META_NAME = "ebook-server:generated"
# Stable per-book UUID for dc:identifier opf:scheme="uuid" (Calibre's unique id), derived
# from the Book id so regenerating never changes it.
_BOOK_UUID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "urn:ebook-server:book")
# An existing metadata.opf larger than this is not ours and is not parsed.
MAX_EXISTING_OPF_BYTES = 2 * 1024 * 1024
# A job re-reads and rewrites at most this many times when edits keep arriving while
# it writes; after that it leaves the book dirty and queues itself again.
_MAX_PASSES = 3
_DC = "http://purl.org/dc/elements/1.1/"
_OPF = "http://www.idpf.org/2007/opf"
# Characters XML 1.0 cannot represent, even escaped.
_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff\ud800-\udfff]")
_LEADING_ARTICLE = re.compile(r"^(the|an|a)\s+(.+)$", re.IGNORECASE)
_NAME_SUFFIXES = frozenset({"jr", "sr", "ii", "iii", "iv", "phd", "md"})
_IDENTIFIER_SCHEMES = (
    (Identifier.IdentifierType.ISBN13, "ISBN"),
    (Identifier.IdentifierType.ISBN10, "ISBN"),
    (Identifier.IdentifierType.DOI, "DOI"),
    (Identifier.IdentifierType.OCLC, "OCLC"),
)
# Book fields that appear in the OPF (or decide where it is written).
OPF_BOOK_FIELDS = frozenset(
    {
        "title",
        "subtitle",
        "publisher",
        "publication_year",
        "publication_date",
        "language",
        "description",
        "front_cover",
        "archive_folder",
    },
)


def opf_writing_enabled() -> bool:
    """True when book folders get a generated ``metadata.opf``."""
    if not bool(getattr(settings, "BOOK_SCANNER_WRITE_BOOK_OPF", True)):
        return False
    return not archive_layout.configured_layout().job_scoped


def book_uuid(book_id: int) -> str:
    return str(uuid.uuid5(_BOOK_UUID_NAMESPACE, str(book_id)))


def _clean(value: object) -> str:
    """Text safe for XML 1.0: invalid code points removed, whitespace kept."""
    return _XML_INVALID.sub("", str(value or ""))


def _one_line(value: object) -> str:
    return " ".join(_clean(value).split())


def title_sort(title: str, language: str = "") -> str:
    """Calibre-style sort title: a leading English article moves to the end."""
    title = _one_line(title)
    lang = (language or "").strip().lower()
    if lang and not lang.startswith("en"):
        return title
    match = _LEADING_ARTICLE.match(title)
    if not match:
        return title
    return f"{match.group(2)}, {match.group(1)}"


def author_sort(name: str) -> str:
    """Calibre-style ``file-as`` for an author: ``Last, First`` (``Asimov, Isaac``)."""
    name = _one_line(name)
    if "," in name or " " not in name:
        return name
    tokens = name.split(" ")
    suffix: list[str] = []
    while len(tokens) > 2 and tokens[-1].rstrip(".").lower() in _NAME_SUFFIXES:
        suffix.insert(0, tokens.pop())
    last = tokens.pop()
    result = f"{last}, {' '.join(tokens)}"
    return f"{result} {' '.join(suffix)}" if suffix else result


def ordered_author_names(book: Book) -> list[str]:
    """Author names in the order they were attached (provider order)."""
    through = Book.authors.through
    author_ids = list(
        through.objects.filter(book_id=book.pk).order_by("id").values_list("author_id", flat=True)
    )
    names = dict(Author.objects.filter(pk__in=author_ids).values_list("pk", "full_name"))
    return [names[pk] for pk in author_ids if names.get(pk)]


def _subjects(book: Book) -> list[str]:
    seen: set[str] = set()
    subjects: list[str] = []
    for name in [
        *book.genres.order_by("name").values_list("name", flat=True),
        *book.categories.order_by("name").values_list("name", flat=True),
    ]:
        text = _one_line(name)
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            subjects.append(text)
    return subjects


def _date(book: Book) -> str:
    if book.publication_date is not None:
        return book.publication_date.isoformat()
    if book.publication_year:
        return f"{book.publication_year:04d}"
    return ""


def render_book_opf(book: Book, *, cover_href: str | None = None) -> bytes:
    """The OPF 2.0 document for ``book``, as UTF-8 bytes.

    Every value is escaped (and stripped of code points XML cannot carry), so any
    title or description yields a well-formed document.
    """
    lines: list[str] = []

    def element(tag: str, text: str, **attrs: str) -> None:
        rendered = "".join(f" {name}={quoteattr(value)}" for name, value in attrs.items())
        lines.append(f"    <{tag}{rendered}>{escape(text)}</{tag}>")

    def meta(name: str, content: str) -> None:
        lines.append(f"    <meta name={quoteattr(name)} content={quoteattr(content)}/>")

    title = _one_line(book.title)
    subtitle = _one_line(book.subtitle)
    full_title = f"{title}: {subtitle}" if title and subtitle else (title or subtitle)
    element("dc:identifier", book_uuid(book.pk), id="uuid_id", **{"opf:scheme": "uuid"})
    element("dc:title", full_title or "Unknown")
    for name in ordered_author_names(book):
        element(
            "dc:creator",
            _one_line(name),
            **{"opf:file-as": author_sort(name), "opf:role": "aut"},
        )
    date = _date(book)
    if date:
        element("dc:date", date)
    description = _clean(book.description).strip()
    if description:
        element("dc:description", description)
    publisher = _one_line(book.publisher)
    if publisher:
        element("dc:publisher", publisher)
    identifiers = list(book.identifiers.order_by("pk"))
    written: set[tuple[str, str]] = set()
    for kind, scheme in _IDENTIFIER_SCHEMES:
        for identifier in identifiers:
            value = _one_line(identifier.normalized_value or identifier.value)
            if identifier.kind != kind or not value or (scheme, value) in written:
                continue
            written.add((scheme, value))
            element("dc:identifier", value, **{"opf:scheme": scheme})
    language = _one_line(book.language)
    if language:
        element("dc:language", language)
    for subject in _subjects(book):
        element("dc:subject", subject)
    meta("calibre:title_sort", title_sort(full_title or "Unknown", book.language))
    meta(GENERATED_META_NAME, "true")
    meta("ebook-server:book_id", str(book.pk))
    guide = ""
    if cover_href:
        guide = (
            "  <guide>\n"
            f'    <reference type="cover" title="Cover" href={quoteattr(cover_href)}/>\n'
            "  </guide>\n"
        )
    document = (
        "<?xml version='1.0' encoding='utf-8'?>\n"
        f'<package xmlns="{_OPF}" unique-identifier="uuid_id" version="2.0">\n'
        f'  <metadata xmlns:dc="{_DC}" xmlns:opf="{_OPF}">\n'
        + "\n".join(lines)
        + "\n  </metadata>\n"
        + guide
        + "</package>\n"
    )
    return document.encode("utf-8")


def is_generated_book_opf(payload: bytes) -> bool:
    """True for a ``metadata.opf`` this application generated (not external evidence).

    Intake's OPF-sidecar reader calls this before using a sidecar. Unparseable or
    hostile documents (entity declarations) are not ours.
    """
    if len(payload) > MAX_EXISTING_OPF_BYTES:
        return False
    try:
        root = DefusedET.fromstring(payload)
    except Exception:  # malformed or hostile XML: certainly not one we wrote
        return False
    for node in root.iter():
        tag = node.tag if isinstance(node.tag, str) else ""
        if tag.rsplit("}", 1)[-1] != "meta":
            continue
        if node.attrib.get("name") == GENERATED_META_NAME:
            return str(node.attrib.get("content", "")).strip().lower() == "true"
    return False


# --- dirty marking and dispatch ----------------------------------------------------


def mark_book_opf_dirty(book_ids: Iterable[int | None], *, force: bool = False) -> list[int]:
    """Record that these books' OPF is stale and queue one job per newly dirty book.

    The job is queued after the surrounding transaction commits. A book that is already
    dirty only gets its marker moved forward: its queued (or running) job picks the
    change up. ``force`` queues a job even then (backfill). Returns the queued ids.
    """
    if not opf_writing_enabled():
        return []
    ids = sorted({int(pk) for pk in book_ids if pk is not None})
    if not ids:
        return []
    queryset = Book.objects.filter(pk__in=ids)
    if force:
        to_queue = list(queryset.values_list("pk", flat=True))
    else:
        to_queue = list(queryset.filter(opf_dirty_at__isnull=True).values_list("pk", flat=True))
    queryset.update(opf_dirty_at=timezone.now())
    if to_queue:
        transaction.on_commit(partial(_dispatch, to_queue))
    return to_queue


def _dispatch(book_ids: list[int]) -> None:
    for book_id in book_ids:
        try:
            jobs.enqueue(write_book_opf, book_id)
        except Exception as exc:  # metadata edits must never fail on the queue
            # The marker stays set, so ``write_book_opfs`` picks the book up later.
            logger.warning(
                "book_opf_dispatch_failed", book_id=book_id, error_type=type(exc).__name__
            )


# --- the job -----------------------------------------------------------------------


@dataclass(frozen=True)
class OpfTarget:
    location: StorageLocation
    storage: LocalFilesystemStorage
    identifier: str
    cover_href: str | None


def _in_folder(storage_identifier: str, folder: str) -> bool:
    return storage_identifier.lower().startswith(folder.lower().rstrip("/") + "/")


def opf_targets(book: Book) -> list[OpfTarget]:
    """Where ``book``'s OPF goes: its folder on each writable location holding its files."""
    folder = (book.archive_folder or "").strip("/")
    if not folder:
        return []
    location_ids: set[int] = set()
    saw_read_only = False
    rows: list[tuple[int, str, bool]] = []
    for model in (FileVariant, CoverImage):
        rows.extend(
            model.objects.filter(book_id=book.pk).values_list(
                "storage_location_id", "storage_identifier", "storage_location__read_only"
            )
        )
    rows.extend(
        ScanLog.objects.filter(scan_job__book_id=book.pk).values_list(
            "storage_location_id", "storage_identifier", "storage_location__read_only"
        )
    )
    for location_id, identifier, read_only in rows:
        if read_only:
            # Indexed in place: that folder belongs to its share, never ours to write.
            saw_read_only = True
        elif _in_folder(identifier, folder):
            location_ids.add(location_id)
    if not location_ids:
        logger.debug(
            "book_opf_no_target",
            book_id=book.pk,
            reason="read_only" if saw_read_only else "no_files_in_folder",
        )
        return []
    front = (
        CoverImage.objects.filter(pk=book.front_cover_id).first() if book.front_cover_id else None
    )
    targets: list[OpfTarget] = []
    for location in StorageLocation.objects.filter(pk__in=location_ids).order_by("pk"):
        if location.read_only:
            continue
        try:
            storage = filesystem_backend_for_location(location)
        except StorageConfigurationError:
            logger.warning("book_opf_location_unsupported", location_id=location.pk)
            continue
        if isinstance(storage, ReadOnlyFilesystemStorage):
            continue
        cover_href = None
        if (
            front is not None
            and front.storage_location_id == location.pk
            and str(PurePosixPath(front.storage_identifier).parent).lower() == folder.lower()
        ):
            cover_href = quote(PurePosixPath(front.storage_identifier).name)
        targets.append(
            OpfTarget(
                location=location,
                storage=storage,
                identifier=f"{folder}/{OPF_FILENAME}",
                cover_href=cover_href,
            ),
        )
    return targets


def _read_existing(storage: LocalFilesystemStorage, identifier: str) -> bytes | None:
    if not storage.exists(identifier):
        return None
    with storage.open(identifier) as handle:
        return handle.read(MAX_EXISTING_OPF_BYTES + 1)


def _write_once(book_id: int) -> str:
    book = Book.objects.filter(pk=book_id).first()
    if book is None:
        return "missing"
    targets = opf_targets(book)
    if not targets:
        return "no_target"
    outcome = "unchanged"
    for target in targets:
        payload = render_book_opf(book, cover_href=target.cover_href)
        existing = _read_existing(target.storage, target.identifier)
        if existing == payload:
            continue
        if existing is not None and not is_generated_book_opf(existing):
            # Someone else's metadata.opf (a hand-placed file): never overwritten.
            logger.warning(
                "book_opf_foreign_file_kept", book_id=book_id, location_id=target.location.pk
            )
            outcome = "foreign_kept" if outcome == "unchanged" else outcome
            continue
        target.storage.write_atomic(target.identifier, payload)
        outcome = "written"
    return outcome


def write_book_opf(book_id: int) -> str:
    """Worker entry point: (re)write ``book_id``'s ``metadata.opf`` and clear its marker.

    Idempotent. The marker is cleared only if no edit arrived while writing; otherwise
    the job writes again (bounded), then leaves the book dirty and re-queues itself.
    """
    if not opf_writing_enabled():
        return "disabled"
    outcome = "missing"
    for _ in range(_MAX_PASSES):
        row = Book.objects.filter(pk=book_id).values_list("opf_dirty_at").first()
        if row is None:
            return "missing"
        marker = row[0]
        outcome = _write_once(book_id)
        if Book.objects.filter(pk=book_id, opf_dirty_at=marker).update(opf_dirty_at=None):
            logger.info("book_opf_job_done", book_id=book_id, outcome=outcome)
            return outcome
    logger.info("book_opf_requeued", book_id=book_id)
    _dispatch([book_id])
    return outcome


# --- change detection --------------------------------------------------------------


def _book_ids_of(instance: Any) -> list[int]:
    return list(instance.books.values_list("pk", flat=True))


def _on_book_pre_save(sender: Any, instance: Book, update_fields: Any = None, **_: Any) -> None:
    # ``opf_dirty_at`` is owned by queryset updates here; an instance loaded earlier
    # must not write a stale marker back (that could hide a pending change).
    if instance.pk is None or instance._state.adding:
        return
    if update_fields is not None and "opf_dirty_at" not in update_fields:
        return
    instance.opf_dirty_at = (
        Book.objects.filter(pk=instance.pk).values_list("opf_dirty_at", flat=True).first()
    )


def _on_book_saved(
    sender: Any, instance: Book, created: bool, update_fields: Any = None, **_: Any
) -> None:
    if update_fields is not None and not (set(update_fields) & OPF_BOOK_FIELDS):
        return
    mark_book_opf_dirty([instance.pk])


def _on_book_relation_changed(
    sender: Any,
    instance: Any,
    action: str,
    reverse: bool,
    pk_set: Any = None,
    **_: Any,
) -> None:
    if not reverse:
        if action in {"post_add", "post_remove", "post_clear"}:
            mark_book_opf_dirty([instance.pk])
        return
    # Reverse side (``author.books.add(book)``): ``pk_set`` holds Book ids.
    if action in {"post_add", "post_remove"}:
        mark_book_opf_dirty(pk_set or [])
    elif action == "pre_clear":
        mark_book_opf_dirty(_book_ids_of(instance))


def _on_book_child_changed(sender: Any, instance: Any, **_: Any) -> None:
    mark_book_opf_dirty([instance.book_id])


def _on_file_variant_saved(
    sender: Any, instance: FileVariant, created: bool, update_fields: Any = None, **_: Any
) -> None:
    # A file joining or leaving the book folder decides where the OPF is written.
    if (
        created
        or update_fields is None
        or {"storage_identifier", "storage_location"} & set(update_fields)
    ):
        mark_book_opf_dirty([instance.book_id])


def _on_named_entity_saved(sender: Any, instance: Any, created: bool, **_: Any) -> None:
    # A renamed author, genre or category changes every one of its books' OPF.
    if not created:
        mark_book_opf_dirty(_book_ids_of(instance))


def connect_signals() -> None:
    """Hook metadata changes to OPF regeneration (called from ``CoreConfig.ready``)."""
    from django.db.models.signals import m2m_changed, post_delete, post_save, pre_save

    pre_save.connect(_on_book_pre_save, sender=Book, dispatch_uid="book_opf_book_pre")
    post_save.connect(_on_book_saved, sender=Book, dispatch_uid="book_opf_book")
    for relation in (Book.authors.through, Book.genres.through, Book.categories.through):
        m2m_changed.connect(
            _on_book_relation_changed,
            sender=relation,
            dispatch_uid=f"book_opf_m2m_{relation._meta.label_lower}",
        )
    for child in (Identifier, CoverImage):
        uid = child._meta.label_lower
        post_save.connect(_on_book_child_changed, sender=child, dispatch_uid=f"book_opf_s_{uid}")
        post_delete.connect(_on_book_child_changed, sender=child, dispatch_uid=f"book_opf_d_{uid}")
    post_save.connect(_on_file_variant_saved, sender=FileVariant, dispatch_uid="book_opf_fv")
    post_delete.connect(_on_book_child_changed, sender=FileVariant, dispatch_uid="book_opf_fv_d")
    for named in (Author, Genre, Category):
        post_save.connect(
            _on_named_entity_saved,
            sender=named,
            dispatch_uid=f"book_opf_named_{named._meta.label_lower}",
        )


# --- cleanup when files leave a folder ---------------------------------------------


def remove_orphaned_opf(storage: LocalFilesystemStorage, folder: str) -> bool:
    """Delete our generated OPF from ``folder`` once it is the folder's only file.

    Used after a reorganize moves a book's files out of a folder and after a book is
    deleted, so the derived file does not keep an otherwise empty folder alive.
    Foreign OPFs and folders that still hold other files are left alone.
    """
    folder = folder.strip("/")
    if not folder or isinstance(storage, ReadOnlyFilesystemStorage):
        return False
    identifier = f"{folder}/{OPF_FILENAME}"
    try:
        directory = storage.resolve_location(folder)
        if not directory.is_dir():
            return False
        names = {entry.name for entry in directory.iterdir()}
        if names != {OPF_FILENAME}:
            return False
        existing = _read_existing(storage, identifier)
        if existing is None or not is_generated_book_opf(existing):
            return False
        storage.delete(identifier)
        storage.prune_empty_parents(identifier)
    except (OSError, ValueError) as exc:
        logger.warning("book_opf_cleanup_failed", error_type=type(exc).__name__)
        return False
    return True
