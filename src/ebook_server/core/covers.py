"""Bounded front-cover extraction for imported book variants."""

from __future__ import annotations

import hashlib
import shutil
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
from defusedxml import ElementTree as ET
from django.db import transaction
from PIL import Image, ImageOps, UnidentifiedImageError

from . import intake
from .models import Book, CoverImage, FileVariant
from .storage_registry import backend_for_location

MAX_COVER_INPUT_BYTES = 25 * 1024 * 1024
MAX_COVER_COMPRESSION_RATIO = 200
COVER_SIZE = (1200, 1600)
ARCHIVE_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp"}


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


def extract_front_cover(variant: FileVariant) -> CoverImage | None:
    """Create a normalized front cover once, retaining source/tool provenance."""
    existing = CoverImage.objects.filter(
        book=variant.book,
        position=CoverImage.CoverPosition.FRONT,
    ).first()
    if existing is not None:
        return existing

    storage = backend_for_location(variant.storage_location)
    suffix = Path(variant.original_filename).suffix
    with NamedTemporaryFile(suffix=suffix) as source_file:
        with storage.open(variant.storage_identifier) as source:
            shutil.copyfileobj(source, source_file)
        source_file.flush()
        extracted = _extract_image(variant, Path(source_file.name))
    if extracted is None:
        return None
    image, extractor_tool = extracted
    try:
        payload = _normalized_cover_bytes(image)
    except (Image.DecompressionBombError, UnidentifiedImageError, OSError) as exc:
        raise CoverExtractionError(str(exc)) from exc

    sha256 = hashlib.sha256(payload).hexdigest()
    destination = storage.generate_destination(
        f"books/{variant.book_id}/covers",
        f"{uuid4()}-front.jpg",
    )
    with NamedTemporaryFile(suffix=".jpg") as output_file:
        output_file.write(payload)
        output_file.flush()
        storage.store(output_file.name, destination)

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
                storage_location=variant.storage_location,
                storage_identifier=destination,
                mime_type="image/jpeg",
                size_bytes=len(payload),
                sha256=sha256,
                source_variant=variant,
                extractor_tool=extractor_tool,
                extractor_version=(
                    f"pypdfium2 {version('pypdfium2')}; Pillow {version('Pillow')}"
                    if extractor_tool.startswith("pypdfium2")
                    else f"Pillow {version('Pillow')}"
                ),
            )
            book.front_cover = cover
            book.save(update_fields=["front_cover_id", "updated_at"])
            return cover
    except Exception:
        storage.delete(destination)
        raise
