"""Bounded front-cover extraction for imported book variants."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import warnings
from importlib.metadata import version
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any
from urllib.parse import unquote
from uuid import uuid4
from zipfile import ZipFile

import pypdfium2 as pdfium
import structlog
from defusedxml import ElementTree as ET
from django.conf import settings as django_settings
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from ebook_server.storage import StorageBackend

from . import archive_layout, intake
from .models import Book, CoverImage, FileVariant, ImportJob
from .storage_registry import backend_for_location, writable_location_for

logger = structlog.get_logger(__name__)

MAX_COVER_INPUT_BYTES = 25 * 1024 * 1024
MAX_COVER_COMPRESSION_RATIO = 200
COVER_SIZE = (1200, 1600)
ARCHIVE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
# Sidecar covers (Calibre-style ``cover.jpg`` next to the book file), in preference order.
SIDECAR_COVER_NAMES = intake.SIDECAR_COVER_NAMES
MAX_SIDECAR_COVER_BYTES = MAX_COVER_INPUT_BYTES
MAX_SIDECAR_COVER_PIXELS = 40_000_000
SIDECAR_EXTRACTOR_TOOL = "sidecar+pillow"


class CoverExtractionError(RuntimeError):
    """Raised for a controlled cover extraction failure."""


def _local_name(tag: Any) -> str:
    return str(tag).rsplit("}", 1)[-1].lower()


def _bounded_archive_image(archive: ZipFile, member_name: str) -> bytes:
    info = archive.getinfo(member_name)
    if info.flag_bits & 0x1:
        raise CoverExtractionError("encrypted cover images are not supported")
    if info.file_size > MAX_COVER_INPUT_BYTES:
        raise CoverExtractionError("cover image exceeds the safe size limit")
    if info.file_size / max(info.compress_size, 1) > MAX_COVER_COMPRESSION_RATIO:
        raise CoverExtractionError("cover image compression ratio exceeds the safe limit")
    with archive.open(info, "r") as stream:
        payload = stream.read(MAX_COVER_INPUT_BYTES + 1)
    if len(payload) > MAX_COVER_INPUT_BYTES:
        raise CoverExtractionError("cover image exceeds the safe size limit")
    return payload


def _safe_package_member(opf_name: str, href: str) -> str:
    decoded = unquote(href).split("#", 1)[0]
    member = PurePosixPath(opf_name).parent / PurePosixPath(decoded)
    if member.is_absolute() or ".." in member.parts or "\\" in decoded:
        raise CoverExtractionError("EPUB cover path is unsafe")
    return str(member)


def _epub_cover_payload(path: Path) -> bytes | None:
    with ZipFile(path, "r") as archive:
        intake._validate_zip_container(archive, container_name="EPUB")
        container = intake._read_epub_metadata_member(archive, "META-INF/container.xml")
        root = ET.fromstring(container)
        rootfile = next(
            (element for element in root.iter() if _local_name(element.tag) == "rootfile"),
            None,
        )
        opf_name = rootfile.attrib.get("full-path", "") if rootfile is not None else ""
        if not opf_name:
            return None
        opf = ET.fromstring(intake._read_epub_metadata_member(archive, opf_name))

        cover_id = ""
        for element in opf.iter():
            if _local_name(element.tag) != "meta":
                continue
            if str(element.attrib.get("name", "")).lower() == "cover":
                cover_id = str(element.attrib.get("content", ""))
                break

        manifest_items = [element for element in opf.iter() if _local_name(element.tag) == "item"]
        selected = next(
            (
                element
                for element in manifest_items
                if "cover-image" in str(element.attrib.get("properties", "")).split()
            ),
            None,
        )
        if selected is None and cover_id:
            selected = next(
                (
                    element
                    for element in manifest_items
                    if str(element.attrib.get("id", "")) == cover_id
                ),
                None,
            )
        if selected is None:
            selected = next(
                (
                    element
                    for element in manifest_items
                    if "cover" in str(element.attrib.get("href", "")).lower()
                    and PurePosixPath(str(element.attrib.get("href", ""))).suffix.lower()
                    in ARCHIVE_IMAGE_SUFFIXES
                ),
                None,
            )
        if selected is None:
            return None
        href = str(selected.attrib.get("href", ""))
        if not href:
            return None
        return _bounded_archive_image(archive, _safe_package_member(opf_name, href))


def _cbz_cover_payload(path: Path) -> bytes | None:
    with ZipFile(path, "r") as archive:
        members = intake._validate_zip_container(archive, container_name="CBZ")
        image_names = sorted(
            info.filename
            for info in members
            if PurePosixPath(info.filename).suffix.lower() in ARCHIVE_IMAGE_SUFFIXES
        )
        if not image_names:
            return None
        return _bounded_archive_image(archive, image_names[0])


def _load_image(payload: bytes) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(payload), formats=["JPEG", "PNG", "WEBP", "GIF"]) as source:
            source.seek(0)
            source.load()
            return source.copy()


def _render_pdf_cover(path: Path) -> Image.Image | None:
    document = pdfium.PdfDocument(path)
    try:
        if len(document) == 0:
            return None
        page = document[0]
        try:
            width, height = page.get_size()
            longest = max(width, height)
            if longest <= 0:
                return None
            scale = min(2.0, max(COVER_SIZE) / longest)
            bitmap = page.render(scale=scale)
            try:
                return bitmap.to_pil().copy()
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()


def _normalized_cover_bytes(image: Image.Image) -> bytes:
    with image:
        normalized = ImageOps.exif_transpose(image)
        normalized.thumbnail(COVER_SIZE, Image.Resampling.LANCZOS)
        if normalized.mode != "RGB":
            background = Image.new("RGB", normalized.size, "white")
            if "A" in normalized.getbands():
                background.paste(normalized, mask=normalized.getchannel("A"))
            else:
                background.paste(normalized)
            normalized = background
        output = BytesIO()
        normalized.save(output, format="JPEG", quality=88, optimize=True)
        return output.getvalue()


# A scanner's raw cover photo (#128): a 3840x2160 frame is ~8 MP; leave room for
# higher-resolution cameras while refusing decompression bombs.
MAX_SCAN_COVER_PIXELS = 64_000_000
SCANNER_EXTRACTOR_TOOL = "scanner+pillow"


def normalized_cover_from_file(path: Path) -> bytes:
    """Normalized library cover JPEG from an untrusted image file (a scanner cover).

    JPEGs are decoded at a reduced scale (``draft``) close to the cover size, so a
    large photo never needs its full-resolution bitmap in memory.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path, formats=["JPEG", "PNG", "TIFF", "WEBP"]) as source:
                width, height = source.size
                if width <= 0 or height <= 0 or width * height > MAX_SCAN_COVER_PIXELS:
                    raise CoverExtractionError("scan cover dimensions exceed the safe limit")
                source.draft("RGB", COVER_SIZE)
                source.load()
                image = source.copy()
        return _normalized_cover_bytes(image)
    except CoverExtractionError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise CoverExtractionError("scan cover is not a valid image") from exc


def _extract_image(variant: FileVariant, path: Path) -> tuple[Image.Image, str] | None:
    if variant.format == FileVariant.VariantFormat.EPUB:
        payload = _epub_cover_payload(path)
        return (_load_image(payload), "zipfile+pillow") if payload is not None else None
    if variant.format == FileVariant.VariantFormat.CBZ:
        payload = _cbz_cover_payload(path)
        return (_load_image(payload), "zipfile+pillow") if payload is not None else None
    if variant.format == FileVariant.VariantFormat.PDF:
        image = _render_pdf_cover(path)
        return (image, "pypdfium2+pillow") if image is not None else None
    return None


def sidecar_covers_enabled() -> bool:
    return bool(getattr(django_settings, "BOOK_SCANNER_SIDECAR_COVERS_ENABLED", True))


def find_sidecar_cover(job: ImportJob) -> Path | None:
    """Return a sibling ``cover.jpg``/``cover.png`` only if its folder holds a single book.

    The single-book rule is ``intake.single_book_folder``, shared with the
    ``metadata.opf`` sidecar so both sidecars agree about a folder. Uploads, folders
    outside the source root, and symlinked covers are ignored.
    """
    if not sidecar_covers_enabled():
        return None
    folder = intake.single_book_folder(job)
    if folder is None:
        return None
    for name in SIDECAR_COVER_NAMES:
        path = folder.sidecar_file(name)
        if path is not None:
            return path
    return None


def _read_sidecar_cover(path: Path) -> bytes:
    """Read a loose cover file without following symlinks and within the size cap."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        if path.is_symlink():
            raise CoverExtractionError("sidecar cover is a symlink")
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise CoverExtractionError("sidecar cover cannot be opened") from exc
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise CoverExtractionError("sidecar cover is not a regular file")
        if info.st_size > MAX_SIDECAR_COVER_BYTES:
            raise CoverExtractionError("sidecar cover exceeds the safe size limit")
        payload = handle.read(MAX_SIDECAR_COVER_BYTES + 1)
    if len(payload) > MAX_SIDECAR_COVER_BYTES:
        raise CoverExtractionError("sidecar cover exceeds the safe size limit")
    if not payload:
        raise CoverExtractionError("sidecar cover is empty")
    return payload


def _load_sidecar_image(payload: bytes) -> Image.Image:
    """Verify, then decode, an untrusted loose image within pixel limits."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(payload), formats=["JPEG", "PNG"]) as probe:
                width, height = probe.size
                if width <= 0 or height <= 0 or width * height > MAX_SIDECAR_COVER_PIXELS:
                    raise CoverExtractionError("sidecar cover dimensions exceed the safe limit")
                probe.verify()
            with Image.open(BytesIO(payload), formats=["JPEG", "PNG"]) as source:
                source.load()
                return source.copy()
    except CoverExtractionError:
        raise
    except (
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
        UnidentifiedImageError,
        OSError,
        SyntaxError,
        ValueError,
    ) as exc:
        raise CoverExtractionError("sidecar cover is not a valid image") from exc


def _sidecar_cover_payload(import_job: ImportJob, variant: FileVariant) -> bytes | None:
    """Normalized sidecar cover bytes, or ``None`` to fall back to format extraction."""
    path = find_sidecar_cover(import_job)
    if path is None:
        return None
    try:
        image = _load_sidecar_image(_read_sidecar_cover(path))
        try:
            return _normalized_cover_bytes(image)
        except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
            raise CoverExtractionError("sidecar cover could not be normalized") from exc
    except CoverExtractionError as exc:
        logger.warning(
            "sidecar_cover_rejected",
            file_variant_id=variant.id,
            book_id=variant.book_id,
            sidecar_name=path.name,
            reason=str(exc),
        )
        return None


def _format_cover_payload(
    variant: FileVariant,
    storage: StorageBackend,
) -> tuple[bytes, str, str] | None:
    suffix = Path(variant.original_filename).suffix
    # delete_on_close=False lets the path be reopened by name while open (Windows).
    with NamedTemporaryFile(suffix=suffix, delete_on_close=False) as source_file:
        with storage.open(variant.storage_identifier) as source:
            shutil.copyfileobj(source, source_file)
        source_file.close()
        extracted = _extract_image(variant, Path(source_file.name))
    if extracted is None:
        return None
    image, extractor_tool = extracted
    try:
        payload = _normalized_cover_bytes(image)
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise CoverExtractionError(str(exc)) from exc
    extractor_version = (
        f"pypdfium2 {version('pypdfium2')}; Pillow {version('Pillow')}"
        if extractor_tool.startswith("pypdfium2")
        else f"Pillow {version('Pillow')}"
    )
    return payload, extractor_tool, extractor_version


def extract_front_cover(
    variant: FileVariant,
    *,
    import_job: ImportJob | None = None,
) -> CoverImage | None:
    """Create a normalized front cover once, retaining source/tool provenance.

    For a source-folder ``import_job`` whose folder holds a single book, a valid sibling
    ``cover.jpg``/``cover.png`` (Calibre-style) wins over format extraction; an invalid
    one is logged and format extraction is used instead. The sidecar file is only read,
    never moved or deleted, even for MOVE sources.
    """
    existing = CoverImage.objects.filter(
        book=variant.book,
        position=CoverImage.CoverPosition.FRONT,
    ).first()
    if existing is not None:
        return existing

    # Read from wherever the variant lives; write the cover to a writable location
    # (never onto a read-only, indexed-in-place share).
    source_storage = backend_for_location(variant.storage_location)
    cover_location = writable_location_for(variant.storage_location)
    storage = backend_for_location(cover_location)
    sidecar_payload = (
        _sidecar_cover_payload(import_job, variant) if import_job is not None else None
    )
    if sidecar_payload is not None:
        payload = sidecar_payload
        extractor_tool = SIDECAR_EXTRACTOR_TOOL
        extractor_version = f"Pillow {version('Pillow')}"
    else:
        extracted = _format_cover_payload(variant, source_storage)
        if extracted is None:
            return None
        payload, extractor_tool, extractor_version = extracted

    sha256 = hashlib.sha256(payload).hexdigest()
    with NamedTemporaryFile(suffix=".jpg", delete_on_close=False) as output_file:
        output_file.write(payload)
        output_file.close()
        destination = archive_layout.publish_derived(
            storage,
            variant.book,
            output_file.name,
            "cover.jpg",
            legacy_namespace=f"books/{variant.book_id}/covers",
            legacy_filename=f"{uuid4()}-front.jpg",
        )

    try:
        with transaction.atomic():
            book = Book.objects.select_for_update().get(pk=variant.book_id)
            existing = CoverImage.objects.filter(
                book=book,
                position=CoverImage.CoverPosition.FRONT,
            ).first()
            if existing is not None:
                storage.delete(destination)
                return existing
            cover = CoverImage.objects.create(
                book=book,
                position=CoverImage.CoverPosition.FRONT,
                storage_location=cover_location,
                storage_identifier=destination,
                mime_type="image/jpeg",
                size_bytes=len(payload),
                sha256=sha256,
                source_variant=variant,
                extractor_tool=extractor_tool,
                extractor_version=extractor_version,
                source=(
                    CoverImage.Source.SIDECAR
                    if extractor_tool == SIDECAR_EXTRACTOR_TOOL
                    else CoverImage.Source.EXTRACTED
                ),
            )
            book.front_cover = cover
            book.save(update_fields=["front_cover_id", "updated_at"])
            return cover
    except Exception:
        storage.delete(destination)
        raise
