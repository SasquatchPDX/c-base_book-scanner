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
- `/library/covers/<sha256>/<size>.webp` serves them with
  `Cache-Control: private, max-age=31536000, immutable`; the URL changes when
  the bytes do. With `BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX` set, Django
  only checks permissions and nginx sends the file (`X-Accel-Redirect`).
"""

from __future__ import annotations

import hashlib
import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from importlib.metadata import version
from io import BytesIO
from tempfile import NamedTemporaryFile
from typing import Any, Final

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import CharField, OuterRef, QuerySet, Subquery, Value
from django.db.models.functions import Cast, Concat
from django.db.models.signals import post_delete, post_save
from PIL import Image, ImageOps, UnidentifiedImageError

from ebook_server import jobs

from .models import Book, CoverImage, CoverThumbnail, StorageLocation
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


def _store(location: StorageLocation, rendered: RenderedThumbnail) -> str:
    storage = backend_for_location(location)
    identifier = thumbnail_storage_identifier(rendered.sha256)
    if storage.exists(identifier):  # content-addressed: identical bytes already stored
        return identifier
    storage.ensure_free_space(len(rendered.payload))
    with NamedTemporaryFile(suffix=".webp", delete_on_close=False) as output_file:
        output_file.write(rendered.payload)
        output_file.close()
        storage.store(output_file.name, identifier)
    return identifier


def is_current(cover: CoverImage, thumbnails: Iterable[CoverThumbnail]) -> bool:
    """Whether every size exists and derives from the cover's current bytes."""
    by_size = {thumbnail.size: thumbnail for thumbnail in thumbnails}
    return all(
        size in by_size and by_size[size].source_sha256 == cover.sha256 for size in THUMBNAIL_WIDTHS
    )


def generate_cover_thumbnails(cover_id: int, *, force: bool = False) -> str:
    """Create or refresh the thumbnails of one cover (worker job). Returns an outcome.

    Outcomes: `created`, `current` (nothing to do), `missing` (cover gone) or
    `failed` (unreadable cover; logged, not retried; the list falls back to the
    full cover). Storage outages raise, so the queue retries them.
    """
    try:
        cover = CoverImage.objects.select_related("storage_location").get(pk=cover_id)
    except CoverImage.DoesNotExist:
        return "missing"
    existing = list(cover.thumbnails.all())
    if not force and is_current(cover, existing):
        return "current"
    try:
        rendered = render_thumbnails(_read_cover(cover))
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
    stored = [(item, _store(location, item)) for item in rendered]
    tool_version = _tool_version()
    with transaction.atomic():
        if not CoverImage.objects.filter(pk=cover.pk).exists():
            return "missing"  # deleted meanwhile; the files are content-addressed cache
        for item, identifier in stored:
            CoverThumbnail.objects.update_or_create(
                cover=cover,
                size=item.size,
                defaults={
                    "storage_location": location,
                    "storage_identifier": identifier,
                    "sha256": item.sha256,
                    "source_sha256": cover.sha256,
                    "width": item.width,
                    "height": item.height,
                    "mime_type": "image/webp",
                    "size_bytes": len(item.payload),
                    "tool": TOOL,
                    "tool_version": tool_version,
                },
            )
    replaced = {thumbnail.sha256 for thumbnail in existing} - {item.sha256 for item, _ in stored}
    for thumbnail in existing:
        if thumbnail.sha256 in replaced:
            _delete_unreferenced_file(thumbnail.storage_location_id, thumbnail.storage_identifier)
    logger.info("cover_thumbnails_created", cover_id=cover.id, book_id=cover.book_id)
    return "created"


def _delete_unreferenced_file(location_id: int, identifier: str) -> None:
    """Remove a thumbnail file once no row points at it (identical covers share files)."""
    if CoverThumbnail.objects.filter(
        storage_location_id=location_id, storage_identifier=identifier
    ).exists():
        return
    try:
        location = StorageLocation.objects.get(pk=location_id)
        backend_for_location(location).delete(identifier)
    except Exception:  # a stale cache file is harmless; never fail the caller
        logger.warning("cover_thumbnail_cleanup_failed", storage_identifier=identifier)


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
    location_id, identifier = instance.storage_location_id, instance.storage_identifier
    transaction.on_commit(lambda: _delete_unreferenced_file(location_id, identifier))


def connect_signals() -> None:
    """Generate thumbnails for new or changed covers (called from `CoreConfig.ready`)."""
    post_save.connect(_on_cover_saved, sender=CoverImage, dispatch_uid="cover-thumbnails-save")
    post_delete.connect(
        _on_thumbnail_deleted, sender=CoverThumbnail, dispatch_uid="cover-thumbnails-delete"
    )
