"""Read-only previews of the files attached to a review task (issue #6).

Every helper here treats the file as untrusted: archive members are looked up by exact
name (never joined onto a filesystem path), reads are size-bounded, markup is rebuilt
through an allow-list sanitizer (nh3/ammonia), and images are inlined as bounded
``data:`` URIs so the sandboxed preview frame never needs to fetch anything.
"""

from __future__ import annotations

import base64
import html
import posixpath
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import unquote, urlsplit
from zipfile import BadZipFile, ZipFile, ZipInfo

import nh3
from bs4 import BeautifulSoup
from bs4.element import Tag
from charset_normalizer import from_bytes
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from django.conf import settings as django_settings

from .intake import ValidationError, _validate_zip_container
from .models import FileVariant, ImportJob, ReviewTask, StorageLocation

PREVIEW_PDF = "pdf"
PREVIEW_TEXT = "text"
PREVIEW_DOCUMENT = "document"
PREVIEW_IMAGES = "images"
PREVIEW_IMAGE = "image"
PREVIEW_NONE = "none"

_PREVIEW_KIND_BY_FORMAT = {
    "pdf": PREVIEW_PDF,
    "txt": PREVIEW_TEXT,
    "html": PREVIEW_DOCUMENT,
    "fb2": PREVIEW_DOCUMENT,
    "epub": PREVIEW_DOCUMENT,
    "cbz": PREVIEW_IMAGES,
    "png": PREVIEW_IMAGE,
}

# Content types the file endpoint may serve inline; anything else is an attachment.
INLINE_CONTENT_TYPES = {"pdf": "application/pdf", "png": "image/png"}

TEXT_PAGE_CHARS = 200_000
MAX_ARCHIVE_COMPRESSION_RATIO = 100
MAX_EPUB_SPINE_ITEMS = 2_000
MAX_CBZ_PAGES = 5_000
_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
_IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
)
_DATA_IMAGE_URI = re.compile(r"^data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+$")
_SOURCE_KEY = re.compile(r"^(?:import|v[1-9][0-9]{0,18})$")
# Elements whose content must disappear along with the tag.
_DROP_WITH_CONTENT = {
    "script",
    "style",
    "head",
    "title",
    "noscript",
    "template",
    "iframe",
    "object",
    "embed",
    "svg",
    "math",
    "textarea",
    "select",
}


class PreviewUnavailable(Exception):
    """The file cannot be previewed; the message is safe to show to the reviewer."""


def _limit(name: str, default: int) -> int:
    return int(getattr(django_settings, name, default))


def max_text_bytes() -> int:
    return _limit("BOOK_SCANNER_READER_MAX_TEXT_BYTES", 4 * 1024 * 1024)


def max_document_bytes() -> int:
    return _limit("BOOK_SCANNER_READER_MAX_DOCUMENT_BYTES", 8 * 1024 * 1024)


def max_inline_image_bytes() -> int:
    return _limit("BOOK_SCANNER_READER_MAX_INLINE_IMAGE_BYTES", 12 * 1024 * 1024)


def max_page_image_bytes() -> int:
    return _limit("BOOK_SCANNER_READER_MAX_PAGE_IMAGE_BYTES", 32 * 1024 * 1024)


def preview_kind(file_format: str) -> str:
    return _PREVIEW_KIND_BY_FORMAT.get(file_format.lower(), PREVIEW_NONE)


@dataclass(frozen=True)
class ReaderSource:
    """One previewable file attached to a review task."""

    key: str
    label: str
    file_format: str
    mime_type: str
    original_filename: str
    size_bytes: int
    storage_location: StorageLocation
    storage_identifier: str
    variant_id: int | None = None
    book_id: int | None = None

    @property
    def kind(self) -> str:
        return preview_kind(self.file_format)


def _variant_source(variant: FileVariant) -> ReaderSource:
    origin = variant.get_source_type_display()
    return ReaderSource(
        key=f"v{variant.pk}",
        label=f"{variant.format.upper()} · {origin}",
        file_format=variant.format,
        mime_type=variant.mime_type,
        original_filename=variant.original_filename,
        size_bytes=variant.size_bytes,
        storage_location=variant.storage_location,
        storage_identifier=variant.storage_identifier,
        variant_id=variant.pk,
        book_id=variant.book_id,
    )


def reader_sources(task: ReviewTask) -> list[ReaderSource]:
    """List the files a reviewer of ``task`` may read, the import's own file first."""
    sources: list[ReaderSource] = []
    book_ids: list[int] = []
    seen_variants: set[int] = set()

    job: ImportJob | None = task.import_job
    if job is not None:
        if job.file_variant_id is not None and job.file_variant is not None:
            sources.append(_variant_source(job.file_variant))
            seen_variants.add(job.file_variant.pk)
        elif job.storage_location is not None and job.storage_identifier:
            file_format = job.detected_format or ""
            sources.append(
                ReaderSource(
                    key="import",
                    label=f"{(file_format or 'file').upper()} · Imported file",
                    file_format=file_format,
                    mime_type=job.mime_type or "application/octet-stream",
                    original_filename=job.original_filename,
                    size_bytes=job.size_bytes,
                    storage_location=job.storage_location,
                    storage_identifier=job.storage_identifier,
                ),
            )
        if job.book_id is not None:
            book_ids.append(job.book_id)
    scan_job = task.scan_job
    if scan_job is not None and scan_job.book_id is not None:
        book_ids.append(scan_job.book_id)
    if task.resolved_book_id is not None:
        book_ids.append(task.resolved_book_id)

    if book_ids:
        variants = (
            FileVariant.objects.filter(
                book_id__in=book_ids,
                processing_state=FileVariant.ProcessingState.COMPLETE,
            )
            .select_related("storage_location")
            .order_by("book_id", "id")
        )
        for variant in variants:
            if variant.pk in seen_variants:
                continue
            seen_variants.add(variant.pk)
            sources.append(_variant_source(variant))
    return sources


def find_source(task: ReviewTask, key: str) -> ReaderSource | None:
    """Resolve ``key`` only among the task's own sources (no arbitrary variant ids)."""
    if not _SOURCE_KEY.match(key):
        return None
    for source in reader_sources(task):
        if source.key == key:
            return source
    return None


# --- plain text -----------------------------------------------------------------------


@dataclass(frozen=True)
class TextPage:
    text: str
    page: int
    page_count: int
    truncated: bool
    encoding: str


def read_text_page(stream: BinaryIO, page: int) -> TextPage:
    """Decode a bounded prefix of a text file and return one page of it."""
    limit = max_text_bytes()
    payload = stream.read(limit + 1)
    truncated = len(payload) > limit
    payload = payload[:limit]
    match = from_bytes(payload[: 64 * 1024]).best()
    encoding = str(match.encoding) if match is not None and match.encoding else "utf-8"
    try:
        text = payload.decode(encoding, errors="replace")
    except LookupError:
        encoding = "utf-8"
        text = payload.decode(encoding, errors="replace")
    text = text.replace("\x00", "")
    page_count = max(1, -(-len(text) // TEXT_PAGE_CHARS))
    page = min(max(page, 1), page_count)
    start = (page - 1) * TEXT_PAGE_CHARS
    return TextPage(
        text=text[start : start + TEXT_PAGE_CHARS],
        page=page,
        page_count=page_count,
        truncated=truncated,
        encoding=encoding,
    )


# --- sanitized HTML ---------------------------------------------------------------------


def _attribute_filter(tag: str, attribute: str, value: str) -> str | None:
    if attribute in {"href", "src", "cite", "srcset", "longdesc", "usemap"}:
        if tag == "img" and attribute == "src" and _DATA_IMAGE_URI.match(value):
            return value
        if tag == "a" and attribute == "href" and value.startswith("#"):
            return value
        return None
    return value


def sanitize_fragment(markup: str) -> str:
    """Allow-list sanitize an HTML fragment for the sandboxed preview frame."""
    return nh3.clean(
        markup,
        clean_content_tags=_DROP_WITH_CONTENT,
        url_schemes={"data"},
        attribute_filter=_attribute_filter,
        link_rel="noopener noreferrer",
    )


ImageResolver = Callable[[str], str | None]


def _image_signature(payload: bytes) -> str | None:
    for signature, content_type in _IMAGE_SIGNATURES:
        if payload.startswith(signature):
            return content_type
    if payload[:4] == b"RIFF" and payload[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_data_uri(payload: bytes) -> str | None:
    content_type = _image_signature(payload)
    if content_type is None:
        return None
    return f"data:{content_type};base64,{base64.b64encode(payload).decode('ascii')}"


def _body_markup(raw: bytes | str, resolve_image: ImageResolver | None) -> str:
    soup = BeautifulSoup(raw, "html.parser")
    # SVG wrappers are dropped wholesale; keep cover images they reference as <img>.
    for svg_image in soup.find_all("image"):
        href = svg_image.get("xlink:href") or svg_image.get("href")
        replacement = soup.new_tag("img")
        if isinstance(href, str):
            replacement["src"] = href
        svg_parent = svg_image.find_parent("svg")
        if svg_parent is not None:
            svg_parent.insert_before(replacement)
        svg_image.decompose()
    for img in soup.find_all("img"):
        src = img.get("src")
        data_uri = resolve_image(src) if resolve_image and isinstance(src, str) else None
        if data_uri is None:
            img["src"] = ""
            if not img.get("alt"):
                img["alt"] = "[image]"
        else:
            img["src"] = data_uri
    body = soup.body if isinstance(soup.body, Tag) else soup
    return body.decode_contents()


def sanitize_document(raw: bytes | str, resolve_image: ImageResolver | None = None) -> str:
    return sanitize_fragment(_body_markup(raw, resolve_image))


class _ImageBudget:
    def __init__(self) -> None:
        self.remaining = max_inline_image_bytes()

    def take(self, size: int) -> bool:
        if size > self.remaining:
            return False
        self.remaining -= size
        return True


# --- archive helpers --------------------------------------------------------------------


def _open_zip(stream: BinaryIO, container_name: str) -> tuple[ZipFile, dict[str, ZipInfo]]:
    try:
        archive = ZipFile(stream)
    except (BadZipFile, OSError) as exc:
        raise PreviewUnavailable(f"The {container_name} archive could not be opened.") from exc
    try:
        members = _validate_zip_container(archive, container_name=container_name)
    except ValidationError as exc:
        archive.close()
        raise PreviewUnavailable(f"The {container_name} archive failed safety checks.") from exc
    return archive, {info.filename: info for info in members if not info.is_dir()}


def _read_member(archive: ZipFile, info: ZipInfo, limit: int) -> bytes | None:
    if info.file_size > limit:
        return None
    if info.compress_size and info.file_size / info.compress_size > MAX_ARCHIVE_COMPRESSION_RATIO:
        return None
    try:
        with archive.open(info) as member:
            payload = member.read(limit + 1)
    except (BadZipFile, OSError, RuntimeError, NotImplementedError):
        return None
    if len(payload) > limit:
        return None
    return payload


def _resolve_member_name(base_dir: str, href: str) -> str | None:
    """Resolve a relative archive reference; ``None`` for anything leaving the archive."""
    parts = urlsplit(href)
    if parts.scheme or parts.netloc or not parts.path:
        return None
    path = unquote(parts.path)
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    resolved = posixpath.normpath(posixpath.join(base_dir, path))
    if resolved.startswith("../") or resolved in {"..", "."}:
        return None
    return resolved


def _member_image_resolver(
    archive: ZipFile,
    members: dict[str, ZipInfo],
    base_dir: str,
    budget: _ImageBudget,
) -> ImageResolver:
    def resolve(href: str) -> str | None:
        name = _resolve_member_name(base_dir, href)
        if name is None:
            return None
        info = members.get(name)
        if info is None or not budget.take(info.file_size):
            return None
        payload = _read_member(archive, info, max_page_image_bytes())
        return image_data_uri(payload) if payload is not None else None

    return resolve


# --- EPUB -------------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentPart:
    index: int
    label: str


_CONTAINER_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_OPF_NS = "{http://www.idpf.org/2007/opf}"
_XHTML_TYPES = {"application/xhtml+xml", "text/html", "application/x-dtbook+xml"}


def _parse_xml(payload: bytes) -> Any:
    try:
        return ET.fromstring(payload)
    except (ET.ParseError, DefusedXmlException, ValueError) as exc:
        raise PreviewUnavailable("The book's package metadata could not be parsed.") from exc


def _epub_spine(archive: ZipFile, members: dict[str, ZipInfo]) -> list[str]:
    container_info = members.get("META-INF/container.xml")
    if container_info is None:
        raise PreviewUnavailable("The EPUB has no container manifest.")
    container_payload = _read_member(archive, container_info, 256 * 1024)
    if container_payload is None:
        raise PreviewUnavailable("The EPUB container manifest is unreadable.")
    container = _parse_xml(container_payload)
    rootfile = container.find(f".//{_CONTAINER_NS}rootfile")
    opf_path = rootfile.get("full-path") if rootfile is not None else None
    opf_name = _resolve_member_name("", opf_path) if opf_path else None
    opf_info = members.get(opf_name) if opf_name else None
    if opf_info is None:
        raise PreviewUnavailable("The EPUB package document is missing.")
    opf_payload = _read_member(archive, opf_info, 4 * 1024 * 1024)
    if opf_payload is None:
        raise PreviewUnavailable("The EPUB package document is too large.")
    package = _parse_xml(opf_payload)
    opf_dir = posixpath.dirname(opf_info.filename)
    manifest: dict[str, tuple[str, str]] = {}
    for item in package.iter(f"{_OPF_NS}item"):
        item_id, href = item.get("id"), item.get("href")
        if item_id and href:
            manifest[item_id] = (href, item.get("media-type", ""))
    spine: list[str] = []
    for itemref in package.iter(f"{_OPF_NS}itemref"):
        entry = manifest.get(itemref.get("idref", ""))
        if entry is None or entry[1] not in _XHTML_TYPES:
            continue
        name = _resolve_member_name(opf_dir, entry[0])
        if name is not None and name in members and name not in spine:
            spine.append(name)
        if len(spine) >= MAX_EPUB_SPINE_ITEMS:
            break
    if not spine:
        raise PreviewUnavailable("The EPUB has no readable chapters.")
    return spine


def epub_parts(stream: BinaryIO) -> list[DocumentPart]:
    archive, members = _open_zip(stream, "EPUB")
    with archive:
        spine = _epub_spine(archive, members)
    return [
        DocumentPart(index=index, label=posixpath.basename(name))
        for index, name in enumerate(spine, start=1)
    ]


def epub_part_html(stream: BinaryIO, part: int) -> str:
    archive, members = _open_zip(stream, "EPUB")
    with archive:
        spine = _epub_spine(archive, members)
        if not 1 <= part <= len(spine):
            raise PreviewUnavailable("That chapter does not exist.")
        name = spine[part - 1]
        payload = _read_member(archive, members[name], max_document_bytes())
        if payload is None:
            raise PreviewUnavailable("This chapter is too large to preview.")
        resolver = _member_image_resolver(
            archive,
            members,
            posixpath.dirname(name),
            _ImageBudget(),
        )
        return sanitize_document(payload, resolver)


# --- HTML and FB2 -----------------------------------------------------------------------


def _read_bounded(stream: BinaryIO, limit: int) -> bytes:
    payload = stream.read(limit + 1)
    if len(payload) > limit:
        raise PreviewUnavailable("This file is too large to preview in the browser.")
    return payload


def html_document(stream: BinaryIO) -> str:
    return sanitize_document(_read_bounded(stream, max_document_bytes()))


_FB2_BLOCKS = {
    "section": "section",
    "title": "h2",
    "subtitle": "h3",
    "p": "p",
    "v": "p",
    "text-author": "p",
    "epigraph": "blockquote",
    "cite": "blockquote",
    "annotation": "blockquote",
    "poem": "div",
    "stanza": "div",
    "table": "table",
    "tr": "tr",
    "td": "td",
    "th": "th",
}
_FB2_INLINE = {
    "strong": "strong",
    "emphasis": "em",
    "strikethrough": "s",
    "sub": "sub",
    "sup": "sup",
    "code": "code",
}
_XLINK_HREF = "{http://www.w3.org/1999/xlink}href"


def _local_name(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _fb2_children(element: Any, images: Callable[[str], str | None]) -> Iterable[str]:
    if element.text:
        yield html.escape(element.text)
    for child in element:
        yield _fb2_element(child, images)
        if child.tail:
            yield html.escape(child.tail)


def _fb2_element(element: Any, images: Callable[[str], str | None]) -> str:
    name = _local_name(element.tag)
    if name == "empty-line":
        return "<br>"
    if name == "image":
        href = element.get(_XLINK_HREF) or element.get("href") or ""
        data_uri = images(href[1:]) if href.startswith("#") else None
        return f'<img src="{data_uri}" alt="">' if data_uri else '<img alt="[image]">'
    inner = "".join(_fb2_children(element, images))
    html_tag = _FB2_BLOCKS.get(name) or _FB2_INLINE.get(name)
    if html_tag is None:
        return inner
    return f"<{html_tag}>{inner}</{html_tag}>"


def fb2_document(stream: BinaryIO) -> str:
    root = _parse_xml(_read_bounded(stream, max_document_bytes()))
    binaries: dict[str, str] = {}
    for element in root.iter():
        if _local_name(element.tag) == "binary" and element.get("id"):
            binaries[element.get("id")] = element.text or ""
    budget = _ImageBudget()

    def images(binary_id: str) -> str | None:
        encoded = binaries.get(binary_id)
        if not encoded or not budget.take(len(encoded)):
            return None
        try:
            payload = base64.b64decode(encoded, validate=False)
        except ValueError:
            return None
        return image_data_uri(payload)

    bodies = [element for element in root if _local_name(element.tag) == "body"]
    if not bodies:
        raise PreviewUnavailable("The FB2 file has no body.")
    markup = "".join("".join(_fb2_children(body, images)) for body in bodies)
    return sanitize_fragment(markup)


# --- CBZ --------------------------------------------------------------------------------


def _natural_key(name: str) -> list[Any]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def _cbz_page_names(members: dict[str, ZipInfo]) -> list[str]:
    names = [
        name
        for name in members
        if name.lower().endswith(_IMAGE_EXTENSIONS)
        and not name.startswith("__MACOSX/")
        and not posixpath.basename(name).startswith(".")
    ]
    return sorted(names, key=_natural_key)[:MAX_CBZ_PAGES]


def cbz_parts(stream: BinaryIO) -> list[DocumentPart]:
    archive, members = _open_zip(stream, "CBZ")
    with archive:
        names = _cbz_page_names(members)
    if not names:
        raise PreviewUnavailable("The comic archive contains no page images.")
    return [
        DocumentPart(index=index, label=posixpath.basename(name))
        for index, name in enumerate(names, start=1)
    ]


def cbz_page(stream: BinaryIO, page: int) -> tuple[bytes, str]:
    archive, members = _open_zip(stream, "CBZ")
    with archive:
        names = _cbz_page_names(members)
        if not 1 <= page <= len(names):
            raise PreviewUnavailable("That page does not exist.")
        payload = _read_member(archive, members[names[page - 1]], max_page_image_bytes())
    if payload is None:
        raise PreviewUnavailable("This page image is too large to preview.")
    content_type = _image_signature(payload)
    if content_type is None:
        raise PreviewUnavailable("This page is not a supported image.")
    return payload, content_type


# --- document wrapper -------------------------------------------------------------------

DOCUMENT_CSP = (
    "default-src 'none'; img-src data:; style-src 'unsafe-inline'; "
    "base-uri 'none'; form-action 'none'; frame-ancestors 'self'; sandbox"
)

_DOCUMENT_STYLE = (
    "body{margin:0 auto;padding:24px 28px;max-width:46em;font:17px/1.6 Georgia,serif;"
    "color:#17211b;background:#fffdf8;overflow-wrap:anywhere}"
    "img{max-width:100%;height:auto}table{border-collapse:collapse}"
    "td,th{border:1px solid #d9ded8;padding:4px 6px}"
    "blockquote{margin:1em 1.5em;color:#3e4a43}"
)


def wrap_document(fragment: str, title: str) -> str:
    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        f"<title>{html.escape(title)}</title><style>{_DOCUMENT_STYLE}</style></head>"
        f"<body>{fragment}</body></html>"
    )
