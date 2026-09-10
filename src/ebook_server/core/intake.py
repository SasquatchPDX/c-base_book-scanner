"""Intake service for upload and metadata-driven identification."""

from __future__ import annotations

import hashlib
import mimetypes
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile
from typing import Any, cast
from urllib.parse import unquote
from zipfile import ZipFile, ZipInfo, is_zipfile

import rarfile
import structlog
from bs4 import BeautifulSoup
from charset_normalizer import from_bytes
from defusedxml import ElementTree as ET
from defusedxml.common import DefusedXmlException
from django.conf import settings as django_settings
from django.contrib.auth import get_user_model
from django.utils import timezone
from pypdf import PdfReader

from ebook_server.storage import LocalFilesystemStorage, StorageBackend

from . import identification
from .models import (
    Author,
    Book,
    FileVariant,
    Identifier,
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    SourceLocation,
    StorageLocation,
)
from .storage_registry import backend_for_location

logger = structlog.get_logger(__name__)


class IntakeError(RuntimeError):
    """Raised for controlled intake failures."""


class ValidationError(ValueError):
    """Raised when a user-provided intake value is invalid."""


@dataclass(frozen=True)
class SupportedFormat:
    format_name: str
    mime_type: str


SUPPORTED_FORMATS: dict[str, SupportedFormat] = {
    ".epub": SupportedFormat(
        format_name="epub",
        mime_type="application/epub+zip",
    ),
    ".pdf": SupportedFormat(
        format_name="pdf",
        mime_type="application/pdf",
    ),
    ".mobi": SupportedFormat(
        format_name="mobi",
        mime_type="application/x-mobipocket-ebook",
    ),
    ".txt": SupportedFormat(
        format_name="txt",
        mime_type="text/plain",
    ),
    ".html": SupportedFormat(
        format_name="html",
        mime_type="text/html",
    ),
    ".htm": SupportedFormat(
        format_name="html",
        mime_type="text/html",
    ),
    ".fb2": SupportedFormat(
        format_name="fb2",
        mime_type="application/x-fictionbook+xml",
    ),
    ".djvu": SupportedFormat(
        format_name="djvu",
        mime_type="image/vnd.djvu",
    ),
    ".djv": SupportedFormat(
        format_name="djvu",
        mime_type="image/vnd.djvu",
    ),
    ".cbz": SupportedFormat(
        format_name="cbz",
        mime_type="application/vnd.comicbook+zip",
    ),
    ".cbr": SupportedFormat(
        format_name="cbr",
        mime_type="application/vnd.comicbook-rar",
    ),
}

MAX_UPLOAD_BYTES = getattr(
    django_settings,
    "BOOK_SCANNER_MAX_UPLOAD_BYTES",
    150 * 1024 * 1024,
)
AUTO_ACCEPT_SCORE = identification.AUTO_ACCEPT_SCORE
REVIEW_SCORE = identification.REVIEW_SCORE
MAX_EPUB_METADATA_MEMBER_BYTES = 2 * 1024 * 1024
MAX_EPUB_METADATA_COMPRESSION_RATIO = 100
MAX_FORMAT_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024


def normalize_filename(raw: str) -> str:
    """Return a safe basename for storage and user-visible provenance."""
    if not raw:
        raise ValidationError("filename is required")
    decoded = unquote(raw).strip()
    base = Path(decoded).name
    if base in {"", ".", ".."} or "\\" in decoded or "/" in decoded:
        raise ValidationError("dangerous filename")
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", base)
    safe = safe.strip("._")
    if not safe:
        raise ValidationError("unsafe filename")
    return safe[:220]


def detect_format(filename: str, mime_type: str | None = None) -> tuple[str, str]:
    """Return variant format and MIME type for a candidate file."""
    extension = Path(filename).suffix.lower()
    if extension in SUPPORTED_FORMATS:
        fmt = SUPPORTED_FORMATS[extension]
        return fmt.format_name, fmt.mime_type

    detected = mimetypes.guess_type(filename)[0]
    if detected and detected in {fmt.mime_type for fmt in SUPPORTED_FORMATS.values()}:
        reverse_lookup = next(
            (
                supported
                for supported in SUPPORTED_FORMATS.values()
                if supported.mime_type == detected
            ),
            None,
        )
        if reverse_lookup is not None:
            return reverse_lookup.format_name, reverse_lookup.mime_type

    if mime_type and mime_type in {fmt.mime_type for fmt in SUPPORTED_FORMATS.values()}:
        reverse_lookup = next(
            (
                supported
                for supported in SUPPORTED_FORMATS.values()
                if supported.mime_type == mime_type
            ),
            None,
        )
        if reverse_lookup is not None:
            return reverse_lookup.format_name, reverse_lookup.mime_type

    raise ValidationError(f"unsupported file format: {filename}")


def validate_container_signature(file_path: str, detected_format: str) -> None:
    """Reject obvious extension/MIME spoofing before invoking metadata parsers."""
    path = Path(file_path)
    if detected_format == "pdf":
        with path.open("rb") as source:
            if source.read(5) != b"%PDF-":
                raise ValidationError("file content is not a PDF")
        return
    if detected_format == "epub":
        if not is_zipfile(path):
            raise ValidationError("file content is not an EPUB/ZIP container")
        with ZipFile(path, "r") as archive:
            members = _validate_zip_container(archive, container_name="EPUB")
            if "META-INF/container.xml" not in {info.filename for info in members}:
                raise ValidationError("EPUB package metadata is missing")
        return
    if detected_format == "mobi":
        with path.open("rb") as source:
            header = source.read(68)
        if len(header) < 68 or header[60:68] != b"BOOKMOBI":
            raise ValidationError("file content is not a MOBI container")
        return
    if detected_format in {"txt", "html", "fb2"}:
        sample = _read_metadata_sample(path)
        text, _encoding = _decode_text_sample(sample)
        lowered = text.lstrip("\ufeff\t\r\n ").lower()
        if detected_format == "html" and not (
            lowered.startswith("<!doctype html") or "<html" in lowered[:4096]
        ):
            raise ValidationError("file content is not HTML")
        if detected_format == "fb2" and "<fictionbook" not in lowered[:65536]:
            raise ValidationError("file content is not FictionBook XML")
        return
    if detected_format == "djvu":
        with path.open("rb") as source:
            header = source.read(16)
        if (
            len(header) < 16
            or header[:8] != b"AT&TFORM"
            or header[12:16]
            not in {
                b"DJVU",
                b"DJVM",
            }
        ):
            raise ValidationError("file content is not a DjVu container")
        return
    if detected_format == "cbz":
        if not is_zipfile(path):
            raise ValidationError("file content is not a CBZ/ZIP container")
        with ZipFile(path, "r") as archive:
            members = _validate_zip_container(archive, container_name="CBZ")
            image_suffixes = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif"}
            if not any(
                PurePosixPath(info.filename).suffix.lower() in image_suffixes for info in members
            ):
                raise ValidationError("CBZ container has no supported page images")
        return
    if detected_format == "cbr" and not rarfile.is_rarfile(path):
        raise ValidationError("file content is not a CBR/RAR container")


def _read_metadata_sample(path: Path) -> bytes:
    with path.open("rb") as source:
        payload = source.read(MAX_FORMAT_METADATA_BYTES)
    if not payload:
        raise ValidationError("document is empty")
    return payload


def _validate_zip_container(archive: ZipFile, *, container_name: str) -> list[ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_ARCHIVE_MEMBERS:
        raise ValidationError(f"{container_name} archive has too many members")
    total_uncompressed = 0
    for info in members:
        member_path = PurePosixPath(info.filename)
        if info.filename.startswith(("/", "\\")) or "\\" in info.filename:
            raise ValidationError(f"{container_name} archive contains an unsafe member path")
        if ".." in member_path.parts:
            raise ValidationError(f"{container_name} archive contains an unsafe member path")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValidationError(f"{container_name} archive expands beyond the safe size limit")
    return members


def _decode_text_sample(payload: bytes) -> tuple[str, str]:
    match = from_bytes(payload).best()
    if match is None:
        raise ValidationError("text encoding could not be determined")
    text = str(match)
    if match.chaos > 0.3 or "\x00" in text:
        raise ValidationError("text document contains binary data")
    return text, str(match.encoding or "unknown")


def _default_storage() -> LocalFilesystemStorage:
    base = django_settings.APP_DATA_DIR
    return LocalFilesystemStorage(str(base / "library-artifacts"))


def _default_storage_location() -> StorageLocation:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": "default",
        },
    )
    return location


def _source_transfer_mode(job: ImportJob) -> str:
    if job.source_location is None:
        return "move"
    return job.source_location.transfer_mode


def _archive_should_preserve_source(job: ImportJob) -> bool:
    return _source_transfer_mode(job) == "copy"


def _verify_archive(storage: StorageBackend, location: str, expected_sha: str) -> bool:
    if not location:
        return False
    if not storage.exists(location):
        return False
    try:
        return storage.hash(location) == expected_sha
    except FileNotFoundError:
        return False


def discover_source_paths(
    source_location: SourceLocation,
) -> Iterable[Path]:
    """Yield candidate files for a configured source location."""
    root = Path(source_location.root_path).expanduser().resolve()
    if not root.exists():
        raise IntakeError(f"source location does not exist: {root}")
    if not root.is_dir():
        raise IntakeError(f"source location is not a directory: {root}")
    candidates: Iterable[Path]
    if source_location.recursive:
        candidates = root.rglob("*")
    else:
        candidates = (child for child in root.iterdir())
    for candidate in candidates:
        if not candidate.is_file():
            continue
        yield candidate


def _is_supported_file(path: Path) -> bool:
    try:
        detect_format(path.name)
        return True
    except ValidationError:
        return False


def _make_system_user() -> Any:
    model = get_user_model()
    user, _ = model.objects.get_or_create(username="system-source-ingest")
    return user


def create_source_import_job(
    path: Path,
    source_location: SourceLocation,
    *,
    uploader: Any | None = None,
    source_type: str | None = None,
) -> ImportJob:
    if not path.is_file():
        raise IntakeError(f"source path is not a file: {path}")
    if not _is_supported_file(path):
        raise IntakeError(f"unsupported file format: {path.name}")
    safe_name = normalize_filename(path.name)
    source_type = source_type or source_location.default_source_type
    if source_type not in dict(ImportJob.SourceType.choices):
        raise IntakeError(f"invalid source job type: {source_type}")
    if uploader is None:
        uploader = _make_system_user()

    return ImportJob.objects.create(
        source_type=source_type,
        state=ImportJob.State.DISCOVERED,
        source_location=source_location,
        uploader=uploader,
        source_path=str(path),
        original_filename=path.name,
        normalized_filename=safe_name,
    )


def discover_source_jobs_for_location(
    source_location: SourceLocation,
    *,
    uploader: Any | None = None,
) -> list[ImportJob]:
    jobs: list[ImportJob] = []
    for candidate in discover_source_paths(source_location):
        if not _is_supported_file(candidate):
            continue
        normalized = str(candidate.resolve())
        active_job_exists = ImportJob.objects.filter(
            source_path=normalized,
            source_location=source_location,
            state__in=[
                ImportJob.State.DISCOVERED,
                ImportJob.State.HASHING,
                ImportJob.State.METADATA_EXTRACTION,
                ImportJob.State.SIGNAL_NORMALIZATION,
                ImportJob.State.EXTERNAL_IDENTIFICATION,
                ImportJob.State.MATCHED,
                ImportJob.State.LOW_CONFIDENCE,
                ImportJob.State.ARCHIVING,
            ],
        ).exists()
        if active_job_exists:
            continue
        try:
            jobs.append(create_source_import_job(candidate, source_location, uploader=uploader))
        except IntakeError:
            continue
    return jobs


def preview_source_jobs(
    source_location: SourceLocation,
) -> list[dict[str, str]]:
    return [
        {
            "source_location_id": str(source_location.id),
            "path": str(candidate),
            "filename": candidate.name,
            "source_type": source_location.default_source_type,
        }
        for candidate in discover_source_paths(source_location)
        if _is_supported_file(candidate)
    ]


def compute_sha256(path: str) -> str:
    digest = hashlib.sha256()
    file_path = Path(path)
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_upload_to_staging(uploaded_file: Any, staging_root: Path) -> tuple[str, int]:
    staging_root.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile(
        delete=False,
        dir=staging_root,
        prefix="upload-",
        suffix=".tmp",
    ) as upload_temp:
        total = 0
        for chunk in uploaded_file.chunks():
            payload = chunk
            if not isinstance(payload, bytes):
                payload = payload.encode()
            total += len(payload)
            if total > MAX_UPLOAD_BYTES:
                upload_temp.close()
                Path(upload_temp.name).unlink(missing_ok=True)
                raise ValidationError("upload exceeds configured size limit")
            upload_temp.write(payload)
        staged_path = upload_temp.name
    return staged_path, total


def extract_metadata(
    file_path: str,
    detected_format: str,
    *,
    original_filename: str,
) -> dict[str, Any]:
    try:
        if detected_format == "pdf":
            return _extract_pdf_metadata(file_path, original_filename=original_filename)
        if detected_format == "epub":
            return _extract_epub_metadata(file_path, original_filename=original_filename)
        if detected_format == "txt":
            return _extract_text_metadata(file_path, original_filename=original_filename)
        if detected_format == "html":
            return _extract_html_metadata(file_path, original_filename=original_filename)
        if detected_format == "fb2":
            return _extract_fb2_metadata(file_path, original_filename=original_filename)
        if detected_format == "cbz":
            return _extract_cbz_metadata(file_path, original_filename=original_filename)
    except (ValidationError, DefusedXmlException):
        raise
    except Exception:
        return {"title": Path(original_filename).stem}
    return {"title": Path(original_filename).stem}


def _extract_text_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    payload = _read_metadata_sample(Path(file_path))
    _text, encoding = _decode_text_sample(payload)
    return {
        "title": Path(original_filename).stem,
        "raw_metadata": {"encoding": encoding},
        "container_type": "txt",
    }


def _extract_html_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    payload = _read_metadata_sample(Path(file_path))
    text, encoding = _decode_text_sample(payload)
    soup = BeautifulSoup(text, "html.parser")

    def meta_content(*names: str) -> str | None:
        wanted = {name.lower() for name in names}
        for tag in soup.find_all("meta"):
            key = str(tag.get("name") or tag.get("property") or "").lower()
            value = tag.get("content")
            if key in wanted and isinstance(value, str) and value.strip():
                return value.strip()
        return None

    title = meta_content("og:title", "dc.title")
    if title is None and soup.title is not None:
        title = soup.title.get_text(" ", strip=True) or None
    author = meta_content("author", "dc.creator", "dcterms.creator")
    return {
        "title": title or Path(original_filename).stem,
        "authors": [author] if author else [],
        "language": meta_content("language", "dc.language"),
        "description": meta_content("description", "og:description", "dc.description"),
        "identifier": meta_content("isbn", "dc.identifier", "dcterms.identifier"),
        "publisher": meta_content("publisher", "dc.publisher"),
        "raw_metadata": {"encoding": encoding},
        "container_type": "html",
    }


def _xml_local_name(tag: Any) -> str:
    raw = str(tag)
    return raw.rsplit("}", 1)[-1].lower()


def _xml_element_text(element: Any, child_name: str | None = None) -> str | None:
    target = element
    if child_name is not None:
        target = next(
            (child for child in element if _xml_local_name(child.tag) == child_name),
            None,
        )
        if target is None:
            return None
    value = " ".join("".join(target.itertext()).split())
    return value or None


def _extract_fb2_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    title: str | None = None
    language: str | None = None
    description: str | None = None
    authors: list[str] = []
    inside_title_info = False
    payload = _read_metadata_sample(Path(file_path))
    for event, element in ET.iterparse(BytesIO(payload), events=("start", "end")):
        local_name = _xml_local_name(element.tag)
        if event == "start" and local_name == "title-info":
            inside_title_info = True
            continue
        if event == "end" and inside_title_info:
            if local_name == "book-title":
                title = _xml_element_text(element)
            elif local_name == "lang":
                language = _xml_element_text(element)
            elif local_name == "annotation":
                description = _xml_element_text(element)
            elif local_name == "author":
                parts = [
                    _xml_element_text(element, child_name)
                    for child_name in ("first-name", "middle-name", "last-name", "nickname")
                ]
                author = " ".join(part for part in parts if part)
                if author:
                    authors.append(author)
            elif local_name == "title-info":
                break
    return {
        "title": title or Path(original_filename).stem,
        "authors": authors,
        "language": language,
        "description": description,
        "raw_metadata": {},
        "container_type": "fb2",
    }


def _extract_cbz_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    fallback = {
        "title": Path(original_filename).stem,
        "raw_metadata": {},
        "container_type": "cbz",
    }
    with ZipFile(file_path, "r") as archive:
        member_name = next(
            (name for name in archive.namelist() if name.lower().endswith("comicinfo.xml")),
            None,
        )
        if member_name is None:
            return fallback
        payload = _read_zip_metadata_member(archive, member_name, container_name="CBZ")
    root = ET.fromstring(payload)

    def value(name: str) -> str | None:
        element = next(
            (child for child in root if _xml_local_name(child.tag) == name.lower()),
            None,
        )
        return _xml_element_text(element) if element is not None else None

    authors = [author.strip() for author in (value("Writer") or "").split(",") if author.strip()]
    return {
        "title": value("Title") or fallback["title"],
        "authors": authors,
        "language": value("LanguageISO"),
        "description": value("Summary"),
        "publisher": value("Publisher"),
        "raw_metadata": {"comic_info": True},
        "container_type": "cbz",
    }


def _extract_pdf_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    reader = PdfReader(file_path)
    metadata: Any = reader.metadata or {}
    metadata_dict = (
        {str(key): str(value) for key, value in metadata.items()}
        if hasattr(metadata, "items")
        else {}
    )
    title = metadata.get("/Title") if hasattr(metadata, "get") else None
    author = metadata.get("/Author") if hasattr(metadata, "get") else None
    isbn = metadata.get("/ISBN") if hasattr(metadata, "get") else None
    return {
        "title": title or Path(original_filename).stem,
        "publisher": metadata.get("/Publisher") if hasattr(metadata, "get") else None,
        "language": metadata.get("/Lang") if hasattr(metadata, "get") else None,
        "authors": [author] if author else [],
        "identifier": str(isbn) if isbn else None,
        "raw_metadata": metadata_dict,
        "container_type": "pdf",
    }


def _extract_epub_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    with ZipFile(file_path, "r") as archive:
        try:
            container_doc = _read_epub_metadata_member(
                archive,
                "META-INF/container.xml",
            )
        except KeyError:
            return {
                "title": Path(original_filename).stem,
                "raw_metadata": {},
                "container_type": "epub",
            }
        root = ET.fromstring(container_doc)
        rootfile = root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        opf_path = rootfile.attrib.get("full-path") if rootfile is not None else None
        if not opf_path:
            return {
                "title": Path(original_filename).stem,
                "raw_metadata": {},
                "container_type": "epub",
            }
        if not isinstance(opf_path, str):
            return {
                "title": Path(original_filename).stem,
                "raw_metadata": {},
                "container_type": "epub",
            }
        if opf_path.startswith("/"):
            opf_path = opf_path[1:]
        opf_bytes = _read_epub_metadata_member(archive, opf_path)
        opf_root = ET.fromstring(opf_bytes)
        # Namespace-safe extraction; these fields are best-effort only.
        title = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}title")
        author = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}creator")
        language = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}language")
        description = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}description")
        identifier = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}identifier")
        return {
            "title": title or Path(original_filename).stem,
            "authors": [author] if author else [],
            "language": language,
            "description": description,
            "identifier": identifier,
            "raw_metadata": {
                "container_xml": container_doc.decode("utf-8", errors="ignore"),
            },
            "container_type": "epub",
        }


def _read_epub_metadata_member(archive: ZipFile, member_name: str) -> bytes:
    """Read a small EPUB metadata member while rejecting archive-bomb inputs."""
    return _read_zip_metadata_member(archive, member_name, container_name="EPUB")


def _read_zip_metadata_member(
    archive: ZipFile,
    member_name: str,
    *,
    container_name: str,
) -> bytes:
    """Read one bounded metadata member from an untrusted ZIP container."""
    info = archive.getinfo(member_name)
    if info.flag_bits & 0x1:
        raise ValidationError(f"encrypted {container_name} metadata is not supported")
    if info.file_size > MAX_EPUB_METADATA_MEMBER_BYTES:
        raise ValidationError(f"{container_name} metadata member exceeds safe size limit")
    compressed_size = max(info.compress_size, 1)
    if info.file_size / compressed_size > MAX_EPUB_METADATA_COMPRESSION_RATIO:
        raise ValidationError(f"{container_name} metadata compression ratio exceeds safe limit")
    with archive.open(info, "r") as member:
        payload = member.read(MAX_EPUB_METADATA_MEMBER_BYTES + 1)
    if len(payload) > MAX_EPUB_METADATA_MEMBER_BYTES:
        raise ValidationError(f"{container_name} metadata member exceeds safe size limit")
    return payload


def _find_text_any(root: ET.Element, expression: str) -> str | None:
    element = root.find(expression)
    if element is None:
        return None
    return "".join(element.itertext()).strip() or None


def _save_metadata_candidates(
    job: ImportJob,
    candidates: list[identification.IdentificationCandidate],
) -> None:
    # Ensure idempotence if jobs are retried.
    MetadataCandidate.objects.filter(import_job=job).delete()
    for candidate in candidates:
        state = MetadataCandidate.CandidateState.REVIEW
        if candidate.score >= AUTO_ACCEPT_SCORE:
            state = MetadataCandidate.CandidateState.MATCH
        MetadataCandidate.objects.create(
            import_job=job,
            provider_id=candidate.provider_id,
            provider_ref=candidate.provider_ref,
            title=candidate.title,
            authors=candidate.authors,
            isbn10=candidate.isbn10 or "",
            isbn13=candidate.isbn13 or "",
            score=Decimal(str(candidate.score)),
            raw_payload=candidate.raw,
            language=candidate.language or "",
            publisher=candidate.publisher or "",
            description=candidate.description or "",
            publication_year=candidate.publication_year,
            state=state,
        )


def _create_review_task(
    *,
    job: ImportJob,
    candidates: list[identification.IdentificationCandidate],
) -> ReviewTask:
    existing = ReviewTask.objects.filter(
        import_job=job,
        state__in=(ReviewTask.State.OPEN, ReviewTask.State.CLAIMED),
    ).first()
    signals = identification.build_identification_input(
        source_path=job.source_path,
        original_filename=job.original_filename,
        extracted_metadata=job.evidence if isinstance(job.evidence, dict) else {},
    )
    inferred_title = signals.best_title() or Path(job.original_filename).stem
    inferred_authors = signals.author_hints
    provider_snapshot = [
        {
            "provider_id": candidate.provider_id,
            "provider_ref": candidate.provider_ref,
            "score": float(candidate.score),
        }
        for candidate in candidates
    ]
    if existing is None:
        return ReviewTask.objects.create(
            import_job=job,
            created_by=job.uploader,
            uploader=job.uploader,
            source_filename=job.original_filename,
            source_path_hint=job.source_path,
            inferred_title=inferred_title,
            inferred_authors=inferred_authors,
            embedded_metadata=job.evidence if isinstance(job.evidence, dict) else {},
            provider_snapshot=provider_snapshot,
            state=ReviewTask.State.OPEN,
            decision_payload={"candidate_count": len(candidates)},
        )
    existing.claimed_by = None
    existing.state = ReviewTask.State.OPEN
    existing.decision = ReviewTask.Decision.UNRESOLVED
    existing.decision_payload = {"candidate_count": len(candidates)}
    existing.inferred_title = inferred_title
    existing.inferred_authors = inferred_authors
    existing.embedded_metadata = job.evidence if isinstance(job.evidence, dict) else {}
    existing.provider_snapshot = provider_snapshot
    existing.selected_candidate = None
    existing.decision_notes = ""
    existing.decided_at = None
    existing.reviewer = None
    existing.save(
        update_fields=[
            "state",
            "decision",
            "decision_payload",
            "inferred_title",
            "inferred_authors",
            "embedded_metadata",
            "provider_snapshot",
            "selected_candidate_id",
            "decision_notes",
            "decided_at",
            "reviewer_id",
            "updated_at",
        ],
    )
    return existing


def _create_or_reuse_file_variant(
    job: ImportJob,
    book: Book,
    *,
    storage_identifier: str | None = None,
    source_type: str = "",
) -> FileVariant:
    if not source_type:
        source_type = cast(str, FileVariant.SourceType.UPLOAD)
    duplicate_variant = FileVariant.objects.filter(sha256=job.sha256).first()
    if duplicate_variant is not None:
        return duplicate_variant

    storage = _default_storage()
    storage_id = storage_identifier or job.storage_identifier
    if not storage_id:
        if Path(job.source_path).exists():
            storage_location = _default_storage_location()
            storage_id = storage.generate_destination(f"books/{job.id}", job.normalized_filename)
            storage.store(job.source_path, storage_id)
            job.storage_location = storage_location
            job.storage_identifier = storage_id
        else:
            raise ValueError("source artifact is unavailable for variant creation")

    if job.storage_location is None:
        job.storage_location = _default_storage_location()

    if not job.normalized_filename:
        job.normalized_filename = normalize_filename(job.original_filename)

    return FileVariant.objects.create(
        book=book,
        format=job.detected_format,
        mime_type=job.mime_type,
        original_filename=job.original_filename,
        storage_location=job.storage_location,
        storage_identifier=storage_id,
        size_bytes=job.size_bytes,
        sha256=job.sha256,
        source_type=source_type,
        uploader=job.uploader,
        imported_at=timezone.now(),
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )


def _try_extract_front_cover(variant: FileVariant) -> None:
    from . import covers

    try:
        cover = covers.extract_front_cover(variant)
    except Exception as exc:  # cover generation is optional; preserve successful intake
        logger.warning(
            "cover_extraction_failed",
            file_variant_id=variant.id,
            book_id=variant.book_id,
            format=variant.format,
            error_type=type(exc).__name__,
        )
        return
    if cover is not None:
        logger.info(
            "cover_extraction_succeeded",
            file_variant_id=variant.id,
            book_id=variant.book_id,
            cover_id=cover.id,
            format=variant.format,
        )


def _provision_book_from_manual_metadata(
    *,
    title: str,
    authors: list[str] | None = None,
    publisher: str | None = None,
    language: str | None = None,
    description: str | None = None,
    edition: str | None = None,
    publication_year: int | None = None,
    isbn10: str | None = None,
    isbn13: str | None = None,
) -> Book:
    book = Book.objects.create(
        title=title,
        publisher=publisher or "",
        language=language or "",
        description=description or "",
        edition=edition or "",
    )
    if publication_year:
        try:
            book.publication_date = date(publication_year, 1, 1)
        except ValueError:
            pass
    for raw_author in authors or []:
        author, _ = Author.objects.get_or_create(full_name=raw_author)
        book.authors.add(author)
    if isbn10:
        Identifier.objects.get_or_create(
            book=book,
            kind=Identifier.IdentifierType.ISBN10,
            value=isbn10,
            defaults={"normalized_value": isbn10},
        )
    if isbn13:
        Identifier.objects.get_or_create(
            book=book,
            kind=Identifier.IdentifierType.ISBN13,
            value=isbn13,
            defaults={"normalized_value": isbn13},
        )
    book.save()
    return book


def resolve_import_with_review_data(
    *,
    job: ImportJob,
    review_task: ReviewTask,
    book: Book,
    source_type: str,
    selected_candidate: MetadataCandidate | None = None,
    reviewer: Any | None = None,
    decision: str = "",
    decision_notes: str = "",
    decision_payload: dict[str, Any] | None = None,
) -> FileVariant:
    if not source_type:
        source_type = cast(str, FileVariant.SourceType.UPLOAD)
    if not decision:
        decision = cast(str, ReviewTask.Decision.ACCEPT_CANDIDATE)
    variant = _create_or_reuse_file_variant(job, book, source_type=source_type)
    _try_extract_front_cover(variant)
    review_task.state = ReviewTask.State.RESOLVED
    review_task.decision = decision
    review_task.decision_notes = decision_notes
    review_task.decided_at = timezone.now()
    if decision_payload is not None:
        review_task.decision_payload = decision_payload
    review_task.resolved_book = book
    review_task.selected_candidate = selected_candidate
    review_task.reviewer = reviewer
    review_task.save(
        update_fields=[
            "state",
            "decision",
            "decision_notes",
            "decision_payload",
            "resolved_book_id",
            "selected_candidate_id",
            "reviewer_id",
            "decided_at",
            "updated_at",
        ],
    )
    job.book = book
    job.file_variant = variant
    job.storage_location = variant.storage_location
    job.storage_identifier = variant.storage_identifier
    job.state = ImportJob.State.COMPLETE
    job.save(
        update_fields=[
            "state",
            "book_id",
            "file_variant_id",
            "storage_location_id",
            "storage_identifier",
            "updated_at",
        ],
    )
    return variant


def _provision_book_from_metadata(
    *,
    candidate: identification.IdentificationCandidate | None,
    fallback_title: str,
    extra_metadata: dict[str, Any],
) -> Book:
    if candidate is not None:
        if candidate.isbn13:
            identifier = (
                Identifier.objects.filter(
                    value=candidate.isbn13,
                )
                .select_related("book")
                .first()
            )
            if identifier is not None:
                return identifier.book
        if candidate.isbn10:
            identifier = (
                Identifier.objects.filter(
                    value=candidate.isbn10,
                )
                .select_related("book")
                .first()
            )
            if identifier is not None:
                return identifier.book

        candidate_author_set = {name.lower() for name in candidate.authors}
        for book in Book.objects.filter(title__iexact=candidate.title):
            known_authors = [a.full_name.lower() for a in book.authors.all()]
            if candidate_author_set and candidate_author_set.intersection(known_authors):
                return book

        book = Book.objects.create(title=candidate.title)
        for author_name in candidate.authors:
            author, _ = Author.objects.get_or_create(full_name=author_name)
            book.authors.add(author)
        if candidate.isbn10:
            Identifier.objects.get_or_create(
                book=book,
                kind=Identifier.IdentifierType.ISBN10,
                value=candidate.isbn10,
                defaults={"normalized_value": candidate.isbn10},
            )
        if candidate.isbn13:
            Identifier.objects.get_or_create(
                book=book,
                kind=Identifier.IdentifierType.ISBN13,
                value=candidate.isbn13,
                defaults={"normalized_value": candidate.isbn13},
            )
        if candidate.publisher:
            book.publisher = candidate.publisher
        if candidate.language:
            book.language = candidate.language
        if candidate.description:
            book.description = candidate.description
        if candidate.publication_year:
            try:
                book.publication_date = date(candidate.publication_year, 1, 1)
            except ValueError:
                pass
        book.edition = candidate.edition or book.edition
        book.save()
        return book

    # Fallback provisional metadata
    fallback_title = fallback_title or extra_metadata.get("title") or "Unknown title"
    return Book.objects.create(title=fallback_title)


def _archive_source(storage: LocalFilesystemStorage, job: ImportJob) -> tuple[str, bool]:
    storage_location = _default_storage_location()
    destination = storage.generate_destination(f"books/{job.id}", job.normalized_filename)
    if _archive_should_preserve_source(job):
        storage.copy(job.source_path, destination)
    else:
        storage.store(job.source_path, destination)
    job.storage_location = storage_location
    job.storage_identifier = destination
    verified = _verify_archive(storage, destination, job.sha256)
    return destination, verified


def _finalize_source_cleanup(job: ImportJob, verified: bool) -> None:
    if not verified:
        return
    if _archive_should_preserve_source(job):
        return
    Path(job.source_path).unlink(missing_ok=True)


def process_import_job(job_id: str) -> None:
    """Process one intake job from staging file to durable archive and optional matching."""
    job = ImportJob.objects.select_related("uploader", "source_location").get(pk=job_id)
    if job.state in {ImportJob.State.COMPLETE, ImportJob.State.FAILED}:
        return

    source_path = Path(job.source_path)
    if not source_path.exists():
        job.last_error = "source file missing"
        job.state = ImportJob.State.FAILED
        job.save(update_fields=["state", "last_error", "updated_at"])
        return

    try:
        job.state = ImportJob.State.HASHING
        job.save(update_fields=["state", "updated_at"])

        job.sha256 = compute_sha256(job.source_path)
        job.size_bytes = source_path.stat().st_size
        detected_format, mime_type = detect_format(job.original_filename, job.mime_type or None)
        validate_container_signature(job.source_path, detected_format)
        job.detected_format = detected_format
        job.mime_type = mime_type
        if not job.normalized_filename:
            job.normalized_filename = normalize_filename(job.original_filename)
        job.state = ImportJob.State.METADATA_EXTRACTION
        job.save(
            update_fields=[
                "state",
                "sha256",
                "size_bytes",
                "detected_format",
                "mime_type",
                "updated_at",
            ],
        )

        duplicate_variant = FileVariant.objects.filter(sha256=job.sha256).first()
        if duplicate_variant is not None:
            job.book = duplicate_variant.book
            job.file_variant = duplicate_variant
            job.storage_identifier = duplicate_variant.storage_identifier
            job.storage_location = duplicate_variant.storage_location
            job.state = ImportJob.State.COMPLETE
            job.save(
                update_fields=[
                    "state",
                    "book_id",
                    "file_variant_id",
                    "storage_identifier",
                    "storage_location_id",
                    "updated_at",
                ],
            )
            _finalize_source_cleanup(
                job,
                _verify_archive(
                    backend_for_location(duplicate_variant.storage_location),
                    duplicate_variant.storage_identifier,
                    job.sha256,
                ),
            )
            _try_extract_front_cover(duplicate_variant)
            return

        metadata = extract_metadata(
            job.source_path,
            detected_format,
            original_filename=job.original_filename,
        )
        job.evidence = metadata
        job.state = ImportJob.State.SIGNAL_NORMALIZATION
        job.save(
            update_fields=[
                "evidence",
                "state",
                "updated_at",
            ],
        )

        signals = identification.build_identification_input(
            source_path=job.source_path,
            original_filename=job.original_filename,
            extracted_metadata=metadata,
        )
        job.state = ImportJob.State.EXTERNAL_IDENTIFICATION
        job.save(update_fields=["state", "updated_at"])
        candidates = identification.resolve_candidates(signals)
        _save_metadata_candidates(job, candidates)

        top_candidate = candidates[0] if candidates else None
        if top_candidate is None or top_candidate.score < AUTO_ACCEPT_SCORE:
            _create_review_task(job=job, candidates=candidates)
            job.state = ImportJob.State.LOW_CONFIDENCE
            job.save(update_fields=["state", "updated_at"])
            job.state = ImportJob.State.ARCHIVING
            job.save(update_fields=["state", "updated_at"])
            destination, verified = _archive_source(_default_storage(), job)
            job.storage_identifier = destination
            job.storage_location = _default_storage_location()
            job.save(
                update_fields=[
                    "storage_identifier",
                    "storage_location_id",
                    "state",
                    "updated_at",
                ],
            )
            _finalize_source_cleanup(job, verified)
            if not verified:
                raise RuntimeError("archive verification failed")
            job.state = ImportJob.State.LOW_CONFIDENCE
            job.save(
                update_fields=[
                    "state",
                    "storage_identifier",
                    "storage_location_id",
                    "updated_at",
                ],
            )
            return

        job.state = ImportJob.State.MATCHED
        job.save(update_fields=["state", "updated_at"])
        storage = _default_storage()
        storage_location = _default_storage_location()
        job.state = ImportJob.State.ARCHIVING
        job.save(update_fields=["state", "updated_at"])
        destination, verified = _archive_source(storage, job)
        _finalize_source_cleanup(job, verified)
        if not verified:
            raise RuntimeError("archive verification failed")

        book = _provision_book_from_metadata(
            candidate=top_candidate,
            fallback_title=top_candidate.title,
            extra_metadata=metadata,
        )
        job.storage_identifier = destination
        job.storage_location = storage_location
        job.normalized_filename = normalize_filename(
            f"{book.title}{Path(job.normalized_filename).suffix}",
        )
        variant = FileVariant.objects.create(
            book=book,
            format=detected_format,
            mime_type=mime_type,
            original_filename=job.original_filename,
            storage_location=storage_location,
            storage_identifier=destination,
            size_bytes=job.size_bytes,
            sha256=job.sha256,
            source_type=FileVariant.SourceType.UPLOAD,
            uploader=job.uploader,
            imported_at=timezone.now(),
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )
        _try_extract_front_cover(variant)

        job.book = book
        job.file_variant = variant
        job.storage_location = storage_location
        job.storage_identifier = destination
        job.state = ImportJob.State.COMPLETE
        job.save(
            update_fields=[
                "state",
                "book_id",
                "file_variant_id",
                "storage_location_id",
                "storage_identifier",
                "normalized_filename",
                "updated_at",
            ],
        )
    except Exception as exc:  # pragma: no cover - failure path depends on input surface
        job.last_error = str(exc)
        job.state = ImportJob.State.FAILED
        job.attempt_count += 1
        job.next_retry_at = datetime.now(tz=UTC)
        job.save(
            update_fields=[
                "state",
                "last_error",
                "attempt_count",
                "next_retry_at",
                "updated_at",
            ],
        )
        logger.error(
            "intake_failed",
            import_job_id=str(job.id),
            source_type=job.source_type,
            error_recorded=True,
        )
        raise
