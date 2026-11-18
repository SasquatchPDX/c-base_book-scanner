"""Cover thumbnails for library lists and shelves (#28, phase 4).

A list page used to load every full-size cover (up to 1200x1600 JPEG) through a
Python worker. Thumbnails are small WebP renditions, generated in the
background and addressed by their own SHA-256:

- `generate_cover_thumbnails(cover_id)` is the worker job. It runs after a
  `CoverImage` is saved (queued on commit) and from
  `manage.py generate_cover_thumbnails` (backfill). It is idempotent: a cover
  whose thumbnails already derive from its current bytes is skipped.
- Files live in the `cover-thumbnails` `StorageLocation`, a regenerable cache on
  the application VM, under `<sha[:2]>/<sha>.webp`. The original cover is never
  touched.
- Publication is conditional on the cover's current hash (under the cover row
  lock), so a slow worker cannot overwrite a newer cover's thumbnails.
- Files are written only by `_publish`, under the file's registry row lock and
  after the cover-hash check, through a temp file and an atomic rename: a reader
  never sees a partial file and a failed write never clobbers a complete one. A
  file counts as present only when its size and SHA-256 match the registry, so
  a damaged file is replaced, not reused.
- Identical covers share files, so files are never deleted inline. A file whose
  last reference goes is marked in `CoverThumbnailFile`, and
  `collect_unreferenced_files` (`manage.py collect_cover_thumbnails`) deletes it
  after a grace period, re-checking references under the same row lock that
  publication takes.
- `/library/covers/<sha256>/<size>.webp` serves them with
  `Cache-Control: private, max-age=31536000, immutable`; the URL changes when
  the bytes do. With `BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX` set, Django
  only checks permissions and nginx sends the file (`X-Accel-Redirect`).
"""

from __future__ import annotations

import hashlib
import warnings
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from importlib.metadata import version
from io import BytesIO
from tempfile import NamedTemporaryFile
from typing import Any, Final

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import CharField, Exists, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Cast, Concat
from django.db.models.signals import post_delete, post_save
from django.utils import timezone
from PIL import Image, ImageOps, UnidentifiedImageError

from ebook_server import jobs

from .models import Book, CoverImage, CoverThumbnail, CoverThumbnailFile, StorageLocation
from .storage_registry import StorageConfigurationError, backend_for_location

logger = structlog.get_logger(__name__)

THUMBNAIL_LOCATION_IDENTIFIER: Final = "cover-thumbnails"
# Width in pixels per size; heights follow the cover's aspect ratio. Medium is
# the design's 240 px ceiling (2x for a ~120 CSS px shelf card), small serves
# 60 to 70 CSS px list rows at 2x.
THUMBNAIL_WIDTHS: Final[dict[str, int]] = {
    CoverThumbnail.Size.SMALL.value: 120,
    CoverThumbnail.Size.MEDIUM.value: 240,
}
# Covers are normalised to at most 1200x1600 at extraction; anything far larger
# in the cache location is not a cover we made.
MAX_SOURCE_BYTES: Final = 25 * 1024 * 1024
MAX_SOURCE_PIXELS: Final = 40_000_000
MAX_THUMBNAIL_HEIGHT_RATIO: Final = 2  # height <= 2x width (very tall images are cropped)
WEBP_QUALITY: Final = 80
TOOL: Final = "pillow-webp"
IMMUTABLE_CACHE_CONTROL: Final = "private, max-age=31536000, immutable"


class ThumbnailError(RuntimeError):
    """The cover could not be turned into thumbnails (unreadable or not an image)."""


def thumbnail_storage_location() -> StorageLocation:
    """The cache location for thumbnails, created on first use.

    Its `base_path` defaults to `BOOK_SCANNER_COVER_THUMBNAIL_ROOT` (or
    `cover-thumbnails` under the storage root) and can be changed in the admin.
    """
    configured = str(getattr(settings, "BOOK_SCANNER_COVER_THUMBNAIL_ROOT", "") or "")
    location, _created = StorageLocation.objects.get_or_create(
        identifier=THUMBNAIL_LOCATION_IDENTIFIER,
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": configured or THUMBNAIL_LOCATION_IDENTIFIER,
        },
    )
    return location


def thumbnail_storage_identifier(sha256: str) -> str:
    return f"{sha256[:2]}/{sha256}.webp"


def thumbnail_url(thumbnail: CoverThumbnail) -> str:
    return f"/library/covers/{thumbnail.sha256}/{thumbnail.size}.webp"


@dataclass(frozen=True)
class ThumbnailRef:
    """A current thumbnail of a book's front cover, read from a list query annotation."""

    size: str
    sha256: str
    width: int
    height: int

    @property
    def url(self) -> str:
        return f"/library/covers/{self.sha256}/{self.size}.webp"


def _annotation(size: str) -> str:
    return f"front_cover_thumbnail_{size}"


def with_thumbnail_refs(books: QuerySet[Book]) -> QuerySet[Book]:
    """Annotate each book with its front cover's current thumbnails, inside the book query.

    One correlated `"sha256:width:height"` subquery per size (unique `(cover, size)`
    index) instead of a prefetch, so list pages pay no extra round trip.
    Thumbnails made from older cover bytes are ignored.
    """
    annotations: dict[str, Any] = {}
    for size in THUMBNAIL_WIDTHS:
        current = (
            CoverThumbnail.objects.filter(
                cover_id=OuterRef("front_cover_id"),
                size=size,
                source_sha256=OuterRef("front_cover__sha256"),
            )
            .annotate(
                ref=Concat(
                    "sha256",
                    Value(":"),
                    Cast("width", CharField()),
                    Value(":"),
                    Cast("height", CharField()),
                    output_field=CharField(),
                )
            )
            .values("ref")[:1]
        )
        annotations[_annotation(size)] = Subquery(current, output_field=CharField())
    return books.annotate(**annotations)


def annotated_thumbnails(book: Book) -> dict[str, ThumbnailRef] | None:
    """The refs `with_thumbnail_refs` attached, or None when the book was not annotated."""
    refs: dict[str, ThumbnailRef] = {}
    annotated = False
    for size in THUMBNAIL_WIDTHS:
        if not hasattr(book, _annotation(size)):
            continue
        annotated = True
        raw = getattr(book, _annotation(size))
        if not raw:
            continue
        sha256, width, height = str(raw).split(":")
        refs[size] = ThumbnailRef(size=size, sha256=sha256, width=int(width), height=int(height))
    return refs if annotated else None


def _tool_version() -> str:
    return f"Pillow {version('Pillow')}"[:32]


def _load_cover(payload: bytes) -> Image.Image:
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload), formats=["JPEG", "PNG", "WEBP", "GIF"]) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_SOURCE_PIXELS:
                    raise ThumbnailError("cover dimensions exceed the safe limit")
                source.seek(0)
                source.load()
                upright = ImageOps.exif_transpose(source)
                return upright.convert("RGB")
    except ThumbnailError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise ThumbnailError("cover is not a readable image") from exc


@dataclass(frozen=True)
class RenderedThumbnail:
    size: str
    payload: bytes
    width: int
    height: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.payload).hexdigest()


def render_thumbnails(payload: bytes) -> list[RenderedThumbnail]:
    """WebP renditions of one cover image, one per configured size."""
    image = _load_cover(payload)
    rendered: list[RenderedThumbnail] = []
    with image:
        for size, width in THUMBNAIL_WIDTHS.items():
            target_width = min(width, image.width)
            target_height = max(1, round(image.height * target_width / image.width))
            target_height = min(target_height, target_width * MAX_THUMBNAIL_HEIGHT_RATIO)
            resized = ImageOps.fit(image, (target_width, target_height), Image.Resampling.LANCZOS)
            output = BytesIO()
            resized.save(output, format="WEBP", quality=WEBP_QUALITY, method=6)
            rendered.append(
                RenderedThumbnail(
                    size=size,
                    payload=output.getvalue(),
                    width=target_width,
                    height=target_height,
                )
            )
    return rendered


def _read_cover(cover: CoverImage) -> bytes:
    storage = backend_for_location(cover.storage_location)
    with storage.open(cover.storage_identifier) as handle:
        payload = handle.read(MAX_SOURCE_BYTES + 1)
    if len(payload) > MAX_SOURCE_BYTES:
        raise ThumbnailError("cover exceeds the safe size limit")
    return payload


def _write_file(storage: Any, identifier: str, payload: bytes) -> None:
    """Write (or replace) a thumbnail file atomically: temp file, then rename.

    Only called by `_publish` while it holds the file's registry row lock.
    """
    storage.ensure_free_space(len(payload))
    with NamedTemporaryFile(suffix=".webp", delete_on_close=False) as output_file:
        output_file.write(payload)
        output_file.close()
        storage.store_atomic(output_file.name, identifier)


def _file_matches(storage: Any, identifier: str, sha256: str, size_bytes: int) -> bool:
    """Whether the stored file has exactly the expected content (size, then SHA-256)."""
    try:
        actual_size, _mtime = storage.stat(identifier)
        if size_bytes and actual_size != size_bytes:
            return False
        return bool(storage.hash(identifier) == sha256)
    except FileNotFoundError:
        return False


def is_current(cover: CoverImage, thumbnails: Iterable[CoverThumbnail]) -> bool:
    """Whether every size has a row derived from the cover's current bytes."""
    by_size = {thumbnail.size: thumbnail for thumbnail in thumbnails}
    return all(
        size in by_size and by_size[size].source_sha256 == cover.sha256 for size in THUMBNAIL_WIDTHS
    )


def _files_verified(thumbnails: Iterable[CoverThumbnail]) -> bool:
    """Whether every row's file is registered and holds exactly the registered bytes.

    Hashes the files (a few KB each). The serving path trusts the rows; the job
    and the backfill verify, so a damaged or lost file is regenerated.
    """
    for thumbnail in thumbnails:
        record = CoverThumbnailFile.objects.filter(
            storage_location_id=thumbnail.storage_location_id,
            storage_identifier=thumbnail.storage_identifier,
        ).first()
        if record is None or record.sha256 != thumbnail.sha256:
            return False
        storage = backend_for_location(thumbnail.storage_location)
        if not _file_matches(storage, record.storage_identifier, record.sha256, record.size_bytes):
            return False
    return True


def generate_cover_thumbnails(cover_id: int, *, force: bool = False) -> str:
    """Create or refresh the thumbnails of one cover (worker job). Returns an outcome.

    Outcomes: `created`; `current` (rows derive from the cover's current bytes and
    their files hold exactly the registered bytes: nothing to do); `stale` (the
    cover changed while this job rendered it: nothing published, the newer
    cover's own job publishes);
    `missing` (cover gone) or `failed` (unreadable cover; logged, not retried; the
    list falls back to the full cover). Storage outages raise, so the queue
    retries them.
    """
    try:
        cover = CoverImage.objects.select_related("storage_location").get(pk=cover_id)
    except CoverImage.DoesNotExist:
        return "missing"
    existing = list(cover.thumbnails.select_related("storage_location"))
    if not force and is_current(cover, existing) and _files_verified(existing):
        return "current"
    try:
        payload = _read_cover(cover)
        rendered = render_thumbnails(payload)
    except (ThumbnailError, FileNotFoundError, StorageConfigurationError) as exc:
        logger.warning(
            "cover_thumbnail_failed",
            cover_id=cover.id,
            book_id=cover.book_id,
            reason=str(exc),
            error_type=type(exc).__name__,
        )
        return "failed"

    location = thumbnail_storage_location()
    # Nothing is written before publication: `_publish` writes under the locks.
    # Fence on the bytes actually rendered, not on the row read before reading them.
    outcome = _publish(cover.pk, hashlib.sha256(payload).hexdigest(), location, rendered)
    if outcome == "created":
        logger.info("cover_thumbnails_created", cover_id=cover.id, book_id=cover.book_id)
    elif outcome == "stale":
        logger.info("cover_thumbnail_stale", cover_id=cover.id, book_id=cover.book_id)
    return outcome


def _lock_file_record(
    location: StorageLocation, identifier: str, sha256: str, size_bytes: int
) -> CoverThumbnailFile:
    """Create, or lock, the registry row of one thumbnail file.

    Collection deletes a file and its registry row in one transaction while
    holding this lock. A publisher that waited for the lock and then finds the
    row gone knows the file went with it, and creates the row again.
    """
    for _attempt in range(5):
        record, _created = CoverThumbnailFile.objects.get_or_create(
            storage_location=location,
            storage_identifier=identifier,
            defaults={"sha256": sha256, "size_bytes": size_bytes},
        )
        locked = CoverThumbnailFile.objects.select_for_update().filter(pk=record.pk).first()
        if locked is not None:
            return locked
    raise RuntimeError("thumbnail file registry row kept disappearing")


def _publish(
    cover_id: int,
    source_sha256: str,
    location: StorageLocation,
    rendered: Sequence[RenderedThumbnail],
) -> str:
    """Point the cover's thumbnail rows at `rendered`, if it is still the cover's content.

    Conditional publication: the cover row is locked, and rows change only when
    the cover's current hash equals the hash of the bytes that were rendered. An
    old worker therefore cannot overwrite what a newer cover's job published.

    Each file's registry row is locked before the file is checked and before a
    reference to it is written, and collection takes the same lock before it
    re-checks references and deletes. This is the only place thumbnail files are
    written. An existing file is reused only when its size and SHA-256 match; a
    missing or damaged one (collected meanwhile, cut short by an older version,
    corrupted on disk) is written through a temp file and an atomic rename. A
    published row therefore never points at missing or wrong bytes, and a failed
    write leaves the previous file untouched. Replaced files are only marked
    unreferenced, never deleted.
    """
    storage = backend_for_location(location)
    tool_version = _tool_version()
    with transaction.atomic():
        current = (
            CoverImage.objects.select_for_update()
            .filter(pk=cover_id)
            .values_list("sha256", flat=True)
            .first()
        )
        if current is None:
            return "missing"  # deleted meanwhile; its rows went with it
        if current != source_sha256:
            return "stale"
        # One lock order (by file hash) for every publisher.
        for item in sorted(rendered, key=lambda thumbnail: thumbnail.sha256):
            identifier = thumbnail_storage_identifier(item.sha256)
            size_bytes = len(item.payload)
            record = _lock_file_record(location, identifier, item.sha256, size_bytes)
            changed: list[str] = []
            if record.unreferenced_since is not None:
                record.unreferenced_since = None
                changed.append("unreferenced_since")
            if (record.sha256, record.size_bytes) != (item.sha256, size_bytes):
                record.sha256, record.size_bytes = item.sha256, size_bytes
                changed.extend(["sha256", "size_bytes"])
            if changed:
                record.save(update_fields=changed)
            if not _file_matches(storage, identifier, item.sha256, size_bytes):
                if storage.exists(identifier):
                    logger.warning(
                        "cover_thumbnail_file_repaired",
                        cover_id=cover_id,
                        storage_identifier=identifier,
                    )
                _write_file(storage, identifier, item.payload)
        previous = set(
            CoverThumbnail.objects.filter(cover_id=cover_id).values_list(
                "storage_location_id", "storage_identifier"
            )
        )
        for item in rendered:
            CoverThumbnail.objects.update_or_create(
                cover_id=cover_id,
                size=item.size,
                defaults={
                    "storage_location": location,
                    "storage_identifier": thumbnail_storage_identifier(item.sha256),
                    "sha256": item.sha256,
                    "source_sha256": source_sha256,
                    "width": item.width,
                    "height": item.height,
                    "mime_type": "image/webp",
                    "size_bytes": len(item.payload),
                    "tool": TOOL,
                    "tool_version": tool_version,
                },
            )
        mark_unreferenced(previous)
    return "created"


# ------------------------------------------------------------ file collection


def _references(location_id: Any, identifier: Any) -> QuerySet[CoverThumbnail]:
    return CoverThumbnail.objects.filter(
        storage_location_id=location_id, storage_identifier=identifier
    )


def mark_unreferenced(files: Iterable[tuple[int, str]]) -> int:
    """Start the grace period of files that no thumbnail row points at any more.

    Only a hint for `collect_unreferenced_files`, which re-checks references
    under the file's lock before deleting anything. Returns the files marked.
    """
    marked = 0
    now = timezone.now()
    for location_id, identifier in files:
        marked += (
            CoverThumbnailFile.objects.filter(
                storage_location_id=location_id,
                storage_identifier=identifier,
                unreferenced_since__isnull=True,
            )
            .filter(~Exists(_references(location_id, identifier)))
            .update(unreferenced_since=now)
        )
    return marked


@dataclass(frozen=True)
class CollectionReport:
    marked: int
    deleted: int
    kept: int


def collect_unreferenced_files(
    *, grace_seconds: int | None = None, limit: int = 10_000
) -> CollectionReport:
    """Delete thumbnail files nothing has referenced for longer than the grace period.

    The periodic garbage collection of the content-addressed cache
    (`manage.py collect_cover_thumbnails`). First every registry row without a
    reference is marked (rows removed by cascades or crashes included). Then each
    file marked longer ago than the grace period is handled in its own
    transaction: its registry row is locked (`SKIP LOCKED`: a publisher holding it
    wins), references are checked again, and only then are the file and the row
    deleted together. A file referenced meanwhile is unmarked and kept.
    """
    grace = (
        int(getattr(settings, "BOOK_SCANNER_COVER_THUMBNAIL_GC_GRACE_SECONDS", 86_400))
        if grace_seconds is None
        else grace_seconds
    )
    unreferenced = ~Exists(
        CoverThumbnail.objects.filter(
            storage_location_id=OuterRef("storage_location_id"),
            storage_identifier=OuterRef("storage_identifier"),
        )
    )
    now = timezone.now()
    marked = CoverThumbnailFile.objects.filter(
        unreferenced, unreferenced_since__isnull=True
    ).update(unreferenced_since=now)
    cutoff = now - timedelta(seconds=max(grace, 0))
    candidates = list(
        CoverThumbnailFile.objects.filter(unreferenced_since__lte=cutoff)
        .order_by("pk")
        .values_list("pk", flat=True)[:limit]
    )
    deleted = kept = 0
    for pk in candidates:
        with transaction.atomic():
            record = (
                CoverThumbnailFile.objects.select_for_update(skip_locked=True)
                .select_related("storage_location")
                .filter(pk=pk, unreferenced_since__lte=cutoff)
                .first()
            )
            if record is None:
                continue  # a publisher holds it, or it was unmarked meanwhile
            if _references(record.storage_location_id, record.storage_identifier).exists():
                record.unreferenced_since = None
                record.save(update_fields=["unreferenced_since"])
                kept += 1
                continue
            backend_for_location(record.storage_location).delete(record.storage_identifier)
            record.delete()
            deleted += 1
    if marked or deleted or kept:
        logger.info("cover_thumbnail_files_collected", marked=marked, deleted=deleted, kept=kept)
    return CollectionReport(marked=marked, deleted=deleted, kept=kept)


def covers_needing_thumbnails() -> Any:
    """Front covers whose thumbnails are missing or derive from older cover bytes."""
    from django.db.models import Count, F, Q

    return (
        CoverImage.objects.filter(position=CoverImage.CoverPosition.FRONT)
        .annotate(
            current=Count(
                "thumbnails",
                filter=Q(thumbnails__source_sha256=F("sha256")),
            )
        )
        .filter(current__lt=len(THUMBNAIL_WIDTHS))
    )


# ------------------------------------------------------------------- signals


def _enqueue_after_commit(cover_id: int) -> None:
    def dispatch() -> None:
        try:
            jobs.enqueue(generate_cover_thumbnails, cover_id)
        except Exception:  # thumbnails are optional; the backfill command catches up
            logger.exception("cover_thumbnail_enqueue_failed", cover_id=cover_id)

    transaction.on_commit(dispatch)


def _on_cover_saved(sender: type[CoverImage], instance: CoverImage, **kwargs: Any) -> None:
    # Only front covers appear in lists and shelves.
    if instance.position == CoverImage.CoverPosition.FRONT:
        _enqueue_after_commit(instance.pk)


def _on_thumbnail_deleted(
    sender: type[CoverThumbnail], instance: CoverThumbnail, **kwargs: Any
) -> None:
    # Never delete the shared file here: only start its grace period.
    key = (instance.storage_location_id, instance.storage_identifier)
    transaction.on_commit(lambda: mark_unreferenced([key]))


def connect_signals() -> None:
    """Generate thumbnails for new or changed covers (called from `CoreConfig.ready`)."""
    post_save.connect(_on_cover_saved, sender=CoverImage, dispatch_uid="cover-thumbnails-save")
    post_delete.connect(
        _on_thumbnail_deleted, sender=CoverThumbnail, dispatch_uid="cover-thumbnails-delete"
    )
