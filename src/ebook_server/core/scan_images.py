"""Working-resolution page images, generated once per page version (#195 step 2).

Upstream Scan Tailor runs its detectors at about 150 dpi and keeps decoded
thumbnails in a cache. Here every :class:`ScanPageVersion` gets, once, after upload:

- a lossless *working image* at roughly 150 dpi (never smaller than
  ``WORK_MIN_EDGE`` and never larger than ``WORK_MAX_EDGE`` on its long edge, never
  upscaled). All interactive stages detect and render on it;
- a small JPEG thumbnail for page lists.

Versions are immutable, so the files live under the version's id and never change.
Generation runs as a background job when a version is committed; a stage that needs
the working image before the job ran generates it on the spot.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any, Final

import structlog
from django.db import IntegrityError, transaction
from django.db.models.signals import post_save
from PIL import Image

from ebook_server.jobs import enqueue, get_queue

from .models import ScanPageImage, ScanPageVersion
from .storage_registry import filesystem_backend_for_location

logger = structlog.get_logger(__name__)

GENERATOR_VERSION: Final[str] = "work-v1"
WORK_DPI: Final[int] = 150
WORK_MIN_EDGE: Final[int] = 1600
WORK_MAX_EDGE: Final[int] = 2400
THUMBNAIL_EDGE: Final[int] = 400
# Fast lossless encoding: the working image is an intermediate, never downloaded.
WORK_PNG_COMPRESS_LEVEL: Final[int] = 1
# A queued generation is not queued again for this long (editor retries, page reloads).
REQUEST_DEDUPE_SECONDS: Final[int] = 120


def work_scale(size: tuple[int, int], dpi: tuple[int, int]) -> float:
    """Scale from the full-resolution page to its working image (at most 1)."""
    long_edge = max(1, max(size))
    scale = max(WORK_DPI / max(1, min(dpi)), WORK_MIN_EDGE / long_edge)
    return min(1.0, scale, WORK_MAX_EDGE / long_edge)


def work_size(size: tuple[int, int], dpi: tuple[int, int]) -> tuple[int, int]:
    scale = work_scale(size, dpi)
    return max(1, round(size[0] * scale)), max(1, round(size[1] * scale))


def downscale(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """Area-averaging reduction (fast integer ``reduce`` first, then exact resize)."""
    if image.size == size:
        return image
    return image.resize(size, Image.Resampling.LANCZOS, reducing_gap=3.0)


def save_work_png(image: Image.Image, path: Path, dpi: tuple[int, int] | None = None) -> None:
    options: dict[str, Any] = {"format": "PNG", "compress_level": WORK_PNG_COMPRESS_LEVEL}
    if dpi is not None:
        options["dpi"] = dpi
    image.save(path, **options)


def ensure_rgb_or_l(image: Image.Image) -> Image.Image:
    return image if image.mode in {"L", "RGB"} else image.convert("RGB")


def save_preview_jpeg(image: Image.Image, path: Path) -> None:
    rgb = image if image.mode in {"L", "RGB"} else image.convert("RGB")
    rgb.save(path, format="JPEG", quality=85)


def _namespace(version: ScanPageVersion) -> str:
    return f"scan-images/{version.pk}"


def ensure_page_image(version: ScanPageVersion) -> ScanPageImage:
    """The version's page image, generating it now if it does not exist yet.

    Raises :class:`ebook_server.core.scantailor.ImageLoadError` for undecodable data.
    """
    from . import scantailor  # scantailor imports this module

    existing = ScanPageImage.objects.filter(version=version).first()
    storage = scantailor._scan_storage()
    if existing is not None and storage.exists(existing.work_storage_identifier):
        return existing

    source = filesystem_backend_for_location(version.storage_location).resolve_location(
        version.storage_identifier
    )
    image = scantailor._load_image(source)
    dpi = scantailor._dpi_of(image)
    target = work_size(image.size, dpi)
    work = downscale(image, target)
    namespace = _namespace(version)
    work_identifier = f"{namespace}/work-{GENERATOR_VERSION}.png"
    thumbnail_identifier = f"{namespace}/thumbnail-{GENERATOR_VERSION}.jpg"
    with tempfile.TemporaryDirectory(prefix="page-image-") as tempdir:
        work_path = Path(tempdir) / "work.png"
        thumbnail_path = Path(tempdir) / "thumbnail.jpg"
        save_work_png(work, work_path)
        save_preview_jpeg(scantailor._thumbnail(work, THUMBNAIL_EDGE), thumbnail_path)
        storage.store_atomic(str(work_path), work_identifier)
        storage.store_atomic(str(thumbnail_path), thumbnail_identifier)

    values = {
        "full_width": image.width,
        "full_height": image.height,
        "dpi_x": dpi[0],
        "dpi_y": dpi[1],
        "work_width": work.width,
        "work_height": work.height,
        "storage_location": scantailor._scan_storage_location(),
        "work_storage_identifier": work_identifier,
        "thumbnail_storage_identifier": thumbnail_identifier,
        "generator_version": GENERATOR_VERSION,
    }
    if existing is not None:
        for field, value in values.items():
            setattr(existing, field, value)
        existing.save()
        return existing
    try:
        with transaction.atomic():
            return ScanPageImage.objects.create(version=version, **values)
    except IntegrityError:
        # A concurrent job made the same (deterministic) files first.
        return ScanPageImage.objects.get(version=version)


def generate_page_image_job(version_id: int) -> None:
    """Background job: build the working image and thumbnail of a new page version."""
    version = (
        ScanPageVersion.objects.select_related("storage_location").filter(pk=version_id).first()
    )
    if version is None:
        return
    try:
        page_image = ensure_page_image(version)
    except Exception as exc:  # an unreadable upload must not break anything else
        logger.warning("scan_page_image_failed", version_id=version_id, error=str(exc))
        return
    logger.info(
        "scan_page_image_ready",
        version_id=version_id,
        work_width=page_image.work_width,
        work_height=page_image.work_height,
    )


def _request_key(version_id: int) -> str:
    return f"ebook-server:scan-page-image-requested:{version_id}"


def request_page_image(version_id: int) -> bool:
    """Queue generation of a version's page image unless it is already queued.

    Returns whether a job was queued. With an RQ queue a short-lived marker in the
    queue's Redis (shared by every web and worker process) makes a reader that keeps
    asking for a missing image (the editor retrying its preview, several members
    opening the same page) queue one job, not one per request. Without RQ the job
    runs inline, so the image exists when this returns and nothing needs dedupe. The
    job is idempotent, so a lost marker only costs a redundant job.
    """
    queue = get_queue()
    connection = getattr(getattr(queue, "queue", None), "connection", None)
    if connection is not None:
        try:
            fresh = connection.set(_request_key(version_id), 1, nx=True, ex=REQUEST_DEDUPE_SECONDS)
        except Exception:  # Redis hiccup: queueing twice is harmless
            fresh = True
        if not fresh:
            return False
    enqueue(generate_page_image_job, version_id)
    return True


def missing_page_image_versions(*, include_inactive: bool = False) -> Any:
    """Versions that have no page image yet, e.g. uploaded before migration 0050."""
    versions = ScanPageVersion.objects.filter(page_image__isnull=True)
    if not include_inactive:
        versions = versions.filter(is_active=True)
    return versions.order_by("pk")


def _on_version_saved(sender: Any, instance: ScanPageVersion, created: bool, **_: Any) -> None:
    if not created:
        return
    version_id = instance.pk
    transaction.on_commit(lambda: request_page_image(version_id), robust=True)


def connect_signals() -> None:
    post_save.connect(
        _on_version_saved, sender=ScanPageVersion, dispatch_uid="scan_page_image_version"
    )
