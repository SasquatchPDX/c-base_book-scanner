"""Intake service for upload and metadata-driven identification."""

from __future__ import annotations

import hashlib
import mimetypes
import os
import re
import shutil
import stat
import time
import uuid
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path, PurePosixPath
from tempfile import NamedTemporaryFile, TemporaryDirectory
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
from django.db import models as django_models
from django.db import transaction
from django.db.models.functions import Coalesce
from django.utils import timezone
from pypdf import PdfReader

from ebook_server.jobs import get_queue
from ebook_server.storage import (
    LocalFilesystemStorage,
    StorageBackend,
    cap_filename,
    ensure_path_storage_ready,
    min_free_bytes,
)

from . import (
    archive_layout,
    genres,
    identification,
    metadata_search,
    referenced_files,
    source_layout,
)
from .models import (
    Author,
    Book,
    CoverImage,
    FileVariant,
    Identifier,
    ImportJob,
    MetadataCandidate,
    MetadataSearch,
    ReviewTask,
    SourceLocation,
    StorageLocation,
    clean_publication_year,
)
from .storage_registry import (
    DEFAULT_ARCHIVE_LOCATION_IDENTIFIER,
    archive_root,
    backend_for_location,
    default_archive_location,
    filesystem_backend_for_location,
)
from .text_header import parse_text_header

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
    # Kindle/Mobipocket family (#23). These share the PalmDB container the MOBI
    # signature check already understands; Calibre handles them downstream.
    ".azw": SupportedFormat(
        format_name="azw",
        mime_type="application/vnd.amazon.ebook",
    ),
    ".azw3": SupportedFormat(
        format_name="azw3",
        mime_type="application/x-mobi8-ebook",
    ),
    ".prc": SupportedFormat(
        format_name="prc",
        mime_type="application/x-mobipocket-ebook",
    ),
    ".rtf": SupportedFormat(
        format_name="rtf",
        mime_type="application/rtf",
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

# PalmDB type+creator codes (bytes 60..68) accepted per MOBI-family format.
# Plain PalmDOC (``TEXtREAd``) is the historical ``.prc`` payload.
PALMDB_SIGNATURES: dict[str, frozenset[bytes]] = {
    "mobi": frozenset({b"BOOKMOBI"}),
    "azw": frozenset({b"BOOKMOBI"}),
    "azw3": frozenset({b"BOOKMOBI"}),
    "prc": frozenset({b"BOOKMOBI", b"TEXtREAd"}),
}

# Audiobooks are not managed by this system (docs/design/32-audiobooks.md): members
# are pointed to a separate audiobook server. Intake recognises audio files only so
# it can skip them with a clear reason instead of lumping them in with unknown files.
AUDIO_EXTENSIONS = frozenset(
    {
        ".m4b",
        ".m4a",
        ".mp3",
        ".aac",
        ".ogg",
        ".oga",
        ".opus",
        ".flac",
        ".wav",
        ".wma",
        ".mka",
        ".ape",
        ".aif",
        ".aiff",
        ".aa",
        ".aax",
        ".aaxc",
    }
)
# Audible files carry DRM. They are never read, hashed, copied or converted here.
DRM_AUDIO_EXTENSIONS = frozenset({".aa", ".aax", ".aaxc"})
AUDIO_SKIP_REASON = "audio (not managed here)"
DRM_AUDIO_SKIP_REASON = "audio (not managed here; DRM-protected Audible file)"


def is_audio_filename(filename: str, mime_type: str | None = None) -> bool:
    """True for audio/audiobook files, which intake skips (see docs/design/32-audiobooks.md).

    The extension decides; a declared ``audio/*`` MIME type only counts when the
    extension is not a supported book format.
    """
    extension = Path(filename).suffix.lower()
    if extension in AUDIO_EXTENSIONS:
        return True
    if extension in SUPPORTED_FORMATS:
        return False
    return bool(mime_type and mime_type.lower().startswith("audio/"))


def is_drm_audio_filename(filename: str) -> bool:
    return Path(filename).suffix.lower() in DRM_AUDIO_EXTENSIONS


def audio_skip_reason(filename: str) -> str:
    return DRM_AUDIO_SKIP_REASON if is_drm_audio_filename(filename) else AUDIO_SKIP_REASON


def audiobook_service_url() -> str:
    """The configured audiobook service URL, or "" when unset or not http(s)."""
    value = str(getattr(django_settings, "BOOK_SCANNER_AUDIOBOOK_SERVICE_URL", "") or "").strip()
    return value if value.startswith(("https://", "http://")) else ""


def audio_upload_rejection_message(filename: str) -> str:
    """Message shown when a member uploads an audio file through the web form."""
    if is_drm_audio_filename(filename):
        lead = (
            "This is a DRM-protected Audible file. This library does not accept or "
            "process DRM-protected files."
        )
    else:
        lead = "Audiobooks and other audio files are not managed by this e-book library."
    service_url = audiobook_service_url()
    if service_url:
        return f"{lead} Please use the hackerspace audiobook service: {service_url}"
    return f"{lead} Please ask the library admins where audiobooks are shared."


MAX_UPLOAD_BYTES = getattr(
    django_settings,
    "BOOK_SCANNER_MAX_UPLOAD_BYTES",
    150 * 1024 * 1024,
)
AUTO_ACCEPT_SCORE = identification.AUTO_ACCEPT_SCORE
REVIEW_SCORE = identification.REVIEW_SCORE
AUTO_ACCEPT_TIE_MARGIN = identification.TIE_MARGIN
MAX_EPUB_METADATA_MEMBER_BYTES = 2 * 1024 * 1024
MAX_EPUB_METADATA_COMPRESSION_RATIO = 100
# Bound for short OPF evidence values (publisher, date) copied into job evidence.
MAX_EPUB_TEXT_FIELD = 255
MAX_FORMAT_METADATA_BYTES = 2 * 1024 * 1024
MAX_ARCHIVE_MEMBERS = 10_000
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 4 * 1024 * 1024 * 1024
# Formats whose metadata is read through Calibre's ``ebook-meta`` instead of a Python
# parser: MOBI and the Kindle-family intake formats (AZW, AZW3, PRC; #23).
EBOOK_META_FORMATS = frozenset({"mobi", "azw", "azw3", "prc"})
EBOOK_META_BINARY = "ebook-meta"
EBOOK_META_TIMEOUT_SECONDS = 30.0
DC_NAMESPACE = "{http://purl.org/dc/elements/1.1/}"
# Sidecar files a library tool such as Calibre keeps next to a book's format files.
# They never count as a separate book when deciding whether a folder holds one book.
SIDECAR_COVER_NAMES = ("cover.jpg", "cover.jpeg", "cover.png")
SIDECAR_METADATA_NAME = "metadata.opf"
SIDECAR_FILE_NAMES = frozenset({*SIDECAR_COVER_NAMES, SIDECAR_METADATA_NAME})
# Evidence key that holds sidecar values next to (never over) embedded metadata.
SIDECAR_EVIDENCE_KEY = "sidecar_metadata"
# Marker the application puts in the metadata.opf it generates itself (same name as
# #119's book_opf.GENERATED_META_NAME); such a file is never read as a sidecar.
GENERATED_OPF_META_NAME = "ebook-server:generated"
SIDECAR_METADATA_FIELDS = (
    "title",
    "authors",
    "language",
    "description",
    "publisher",
    "subjects",
    "identifier",
)
# Below this title similarity an embedded and a sidecar title name different books.
SIDECAR_TITLE_CONFLICT_SCORE = 0.5
PLACEHOLDER_METADATA_VALUES = frozenset({"unknown", "unknown author", "unknown title"})
# Extractor provenance of ``metadata["title"]`` (see ``extract_metadata``).
TITLE_SOURCE_KEY = "title_source"
TITLE_SOURCE_EMBEDDED = "embedded"
TITLE_SOURCE_FILENAME = "filename"
TITLE_SOURCE_SIDECAR = "sidecar"

# ImportJob.evidence keys written when a candidate scores high enough to be accepted
# automatically but is routed to review anyway (shown on the review page).
AUTO_ACCEPT_BLOCKED_KEY = "auto_accept_blocked"
AUTO_ACCEPT_BLOCK_DETAILS_KEY = "auto_accept_block_details"
# Authors from the file's own metadata, offered to the reviewer; never applied
# to a Book without a person confirming them.
SUGGESTED_AUTHORS_KEY = "suggested_authors"
# The provider record names no authors (e.g. an edition without them): accepting it
# would create, or attach to, a Book whose authors nobody verified.
BLOCK_NO_PROVIDER_AUTHORS = "no_provider_authors"
# The provider's authors and the file's embedded authors name different people.
BLOCK_AUTHOR_CONFLICT = "author_conflict"
# The candidate was found only through search terms a local LLM proposed (#26). An LLM
# guess is the weakest evidence the system has, so such candidates always need a person.
BLOCK_LLM_DERIVED = "llm_derived"
LLM_HINT_SOURCE_KEY = "_hint_source"
# Several different books tie for the best score and pass the author checks.
BLOCK_AMBIGUOUS_TIE = "ambiguous_tie"
# The provider record stands for another edition than the file: its ISBNs, language or
# publisher contradict the file's (see identification.edition_conflicts).
BLOCK_EDITION_CONFLICT = "edition_conflict"
# ImportJob.evidence key recording that a tied candidate was accepted instead of the
# first-ranked one, and why the others were passed over.
AUTO_ACCEPT_TIE_BREAK_KEY = "auto_accept_tie_break"
# Floating-point slack for the tie margin: 1.0 - 0.98 is 0.020000000000000018.
_TIE_MARGIN_EPSILON = 1e-9


@dataclass(frozen=True)
class AutoAcceptDecision:
    """Outcome of the auto-accept check: the candidate to accept, or why not."""

    candidate: identification.IdentificationCandidate | None
    blocked_reason: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    # Set when the accepted candidate is not the first-ranked one (see decide_auto_accept).
    tie_break: dict[str, Any] | None = None
    # Candidates at or above the threshold that were not accepted.
    passed_over: tuple[identification.IdentificationCandidate, ...] = ()


def _author_block_reason(
    candidate: identification.IdentificationCandidate,
    file_authors: list[str],
) -> str | None:
    """Why the author checks forbid accepting ``candidate`` automatically, if they do."""
    provider_authors = [
        name for name in candidate.authors if not identification.is_placeholder_author(name)
    ]
    if not provider_authors:
        return BLOCK_NO_PROVIDER_AUTHORS
    if identification.authors_conflict(provider_authors, file_authors):
        return BLOCK_AUTHOR_CONFLICT
    return None


def _block_reason(
    candidate: identification.IdentificationCandidate,
    file_authors: list[str],
    file_evidence: identification.IdentificationInput,
) -> tuple[str | None, list[str]]:
    """Why ``candidate`` may not be accepted automatically (LLM, author, edition checks).

    Returns the block reason (``None`` when it may be accepted) and the edition
    conflicts found, which are reported even when an author check blocks first.
    """
    conflicts = identification.edition_conflicts(candidate, file_evidence)
    if isinstance(candidate.raw, dict) and candidate.raw.get(LLM_HINT_SOURCE_KEY) == "llm":
        return BLOCK_LLM_DERIVED, conflicts
    reason = _author_block_reason(candidate, file_authors)
    if reason is None and conflicts:
        reason = BLOCK_EDITION_CONFLICT
    return reason, conflicts


def _candidate_summary(candidate: identification.IdentificationCandidate) -> dict[str, Any]:
    return {
        "provider_id": candidate.provider_id,
        "provider_ref": candidate.provider_ref,
        "title": candidate.title,
        "authors": list(candidate.authors),
        "score": float(candidate.score),
    }


def _block_details(
    candidate: identification.IdentificationCandidate,
    file_authors: list[str],
) -> dict[str, Any]:
    return {
        "provider_id": candidate.provider_id,
        "provider_ref": candidate.provider_ref,
        "score": float(candidate.score),
        "candidate_authors": list(candidate.authors),
        "file_authors": list(file_authors),
    }


def decide_auto_accept(
    candidates: list[identification.IdentificationCandidate],
    file_authors: list[str],
    *,
    file_evidence: identification.IdentificationInput,
) -> AutoAcceptDecision:
    """Pick the candidate to accept without review, or record why there is none.

    A score at or above ``AUTO_ACCEPT_SCORE`` is necessary but not sufficient: the
    candidate must not have been found through LLM-proposed search terms (those always
    need a person), it must name at least one real author, and its authors must not
    contradict the file's own embedded authors. A high score from an ISBN match alone
    is no evidence about authorship. The candidate must also be the file's edition:
    ``file_evidence`` (the normalised file signals: ISBNs, language, publisher) is
    checked with ``identification.edition_conflicts``, so a file ISBN must be one of the
    edition's own ISBNs and the file's language/publisher must not contradict it.

    Candidates within ``AUTO_ACCEPT_TIE_MARGIN`` of the best score count as tied (two
    providers often both score an ISBN match at 1.0, and their order is only the
    configured provider order). Among tied candidates the first one that passes the
    author *and* edition checks is accepted, so a tied fallback whose identifiers
    contradict the file's is never eligible. If tied candidates that pass describe
    different books, the choice is ambiguous and goes to review.
    """
    top = candidates[0] if candidates else None
    if top is None or top.score < AUTO_ACCEPT_SCORE:
        return AutoAcceptDecision(candidate=None)
    above_threshold = [c for c in candidates if c.score >= AUTO_ACCEPT_SCORE]
    tied = [
        candidate
        for candidate in above_threshold
        if top.score - candidate.score <= AUTO_ACCEPT_TIE_MARGIN + _TIE_MARGIN_EPSILON
    ]
    checks = [_block_reason(candidate, file_authors, file_evidence) for candidate in tied]
    eligible = [
        candidate for candidate, (reason, _) in zip(tied, checks, strict=True) if reason is None
    ]
    if not eligible:
        top_reason, top_conflicts = checks[0]
        details = _block_details(top, file_authors)
        if top_conflicts:
            details = {
                **details,
                "edition_conflicts": top_conflicts,
                **identification.edition_evidence(top, file_evidence),
            }
        return AutoAcceptDecision(
            candidate=None,
            blocked_reason=top_reason,
            details=details,
            passed_over=tuple(above_threshold),
        )
    chosen = eligible[0]
    if any(
        not identification.candidates_describe_same_book(chosen, other) for other in eligible[1:]
    ):
        return AutoAcceptDecision(
            candidate=None,
            blocked_reason=BLOCK_AMBIGUOUS_TIE,
            details={
                **_block_details(top, file_authors),
                "tied_candidates": [_candidate_summary(candidate) for candidate in eligible],
            },
            passed_over=tuple(above_threshold),
        )
    tie_break: dict[str, Any] | None = None
    if chosen is not top:
        passed_over_summaries: list[dict[str, Any]] = []
        for candidate, (reason, conflicts) in zip(tied, checks, strict=True):
            if reason is None:
                continue
            summary = {**_candidate_summary(candidate), "reason": reason}
            if conflicts:
                summary["edition_conflicts"] = conflicts
            passed_over_summaries.append(summary)
        tie_break = {"chosen": _candidate_summary(chosen), "passed_over": passed_over_summaries}
    eligible_ids = {id(candidate) for candidate in eligible}
    return AutoAcceptDecision(
        candidate=chosen,
        tie_break=tie_break,
        passed_over=tuple(
            candidate for candidate in above_threshold if id(candidate) not in eligible_ids
        ),
    )


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
    return cap_filename(safe)


def _sanitize_title_for_filename(title: str) -> str:
    """Replace path separators in a bibliographic title before it becomes a filename.

    A Book's title can legitimately contain "/" - e.g. an Open Library omnibus/anthology
    edition titled "Franchise / The Fun They Had / The Last Question / ..." - which
    normalize_filename's path-traversal guard rejects outright, since a raw "/" is
    indistinguishable there from an actual path. This is a derived display title, not
    untrusted path input, so the character just needs replacing, not rejecting.
    """
    return title.replace("/", "-").replace("\\", "-")


def _fit(model: type[django_models.Model], field_name: str, value: str | None) -> str:
    """Truncate ``value`` to the model field's max_length (PostgreSQL rejects overflow)."""
    text = value or ""
    max_length = getattr(model._meta.get_field(field_name), "max_length", None)
    if max_length and len(text) > max_length:
        return text[:max_length].rstrip()
    return text


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
    if detected_format in PALMDB_SIGNATURES:
        with path.open("rb") as source:
            header = source.read(68)
        if len(header) < 68 or header[60:68] not in PALMDB_SIGNATURES[detected_format]:
            raise ValidationError(
                f"file content is not a {detected_format.upper()} (PalmDB/MOBI) container"
            )
        return
    if detected_format == "rtf":
        with path.open("rb") as source:
            header = source.read(8)
        if not header.removeprefix(b"\xef\xbb\xbf").startswith(b"{\\rtf"):
            raise ValidationError("file content is not RTF")
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
    return LocalFilesystemStorage(str(archive_root()))


def _default_storage_location() -> StorageLocation:
    return default_archive_location()


def _storage_for_location(job: ImportJob) -> LocalFilesystemStorage:
    """Filesystem backend for the location ``job`` archived its file to."""
    location = job.storage_location
    if location is None or location.identifier == DEFAULT_ARCHIVE_LOCATION_IDENTIFIER:
        return _default_storage()
    return filesystem_backend_for_location(location)


def _source_transfer_mode(job: ImportJob) -> str:
    if job.source_location is None:
        return "move"
    return job.source_location.transfer_mode


def _archive_should_preserve_source(job: ImportJob) -> bool:
    return _source_transfer_mode(job) in {
        SourceLocation.TransferMode.COPY,
        SourceLocation.TransferMode.INDEX_IN_PLACE,
    }


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
    min_age = float(getattr(django_settings, "BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS", 60))
    now = time.time()
    for candidate in candidates:
        relative_parts = candidate.relative_to(root).parts
        if any(part.startswith((".", "~")) for part in relative_parts):
            continue
        try:
            if not candidate.is_file():
                continue
            if min_age > 0 and now - candidate.stat().st_mtime < min_age:
                continue  # possibly still being written
        except OSError:
            continue
        yield candidate


def _is_supported_file(path: Path) -> bool:
    try:
        detect_format(path.name)
        return True
    except ValidationError:
        return False


def _book_stem(name: str) -> str:
    return Path(name).stem.casefold()


def _folder_stems_from_jobs(job: ImportJob, folder: Path) -> set[str]:
    """Stems of files previously queued from ``folder``; MOVE sources may already be gone."""
    stems: set[str] = set()
    source_paths = ImportJob.objects.filter(
        source_location=job.source_location,
        source_path__startswith=str(folder),
    ).values_list("source_path", flat=True)
    for raw in source_paths:
        path = Path(raw)
        if path.parent == folder:
            stems.add(_book_stem(path.name))
    return stems


@dataclass(frozen=True)
class SingleBookFolder:
    """A source folder that holds exactly one book, plus the sidecar files found in it."""

    path: Path
    sidecars: dict[str, os.DirEntry[str]]

    def sidecar_file(self, name: str) -> Path | None:
        """The sidecar ``name`` as a regular, non-symlinked file in this folder, if any."""
        entry = self.sidecars.get(name)
        if entry is None:
            return None
        try:
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                return None
        except OSError:
            return None
        return self.path / entry.name


def single_book_folder(job: ImportJob) -> SingleBookFolder | None:
    """Return the job's source folder only if it holds a single book.

    The folder qualifies when every intake-supported file in it, plus every file
    previously queued from it (MOVE sources are deleted after archiving), shares the
    job file's stem, as in a Calibre book folder (``Title - Author.epub``,
    ``Title - Author.pdf``, ``cover.jpg``, ``metadata.opf``). A folder holding several
    distinct books never lends its sidecars to any of them. Uploads have no source
    folder, and folders outside the source root are refused. Cover and metadata
    sidecars both use this one rule, so they always agree about a folder.
    """
    if job.source_location is None or not job.source_path:
        return None
    source = Path(job.source_path)
    folder = source.parent
    try:
        root = Path(job.source_location.root_path).expanduser().resolve(strict=True)
        resolved_folder = folder.resolve(strict=True)
    except OSError:
        return None
    if not resolved_folder.is_relative_to(root):
        return None

    stems = {_book_stem(source.name)}
    sidecars: dict[str, os.DirEntry[str]] = {}
    try:
        with os.scandir(resolved_folder) as entries:
            for entry in entries:
                name = entry.name
                if name.startswith((".", "~")):
                    continue
                lowered = name.casefold()
                if lowered in SIDECAR_FILE_NAMES:
                    sidecars.setdefault(lowered, entry)
                elif _is_supported_file(Path(name)):
                    stems.add(_book_stem(name))
    except OSError:
        return None
    stems |= _folder_stems_from_jobs(job, folder)
    if len(stems) != 1:
        return None
    return SingleBookFolder(path=resolved_folder, sidecars=sidecars)


def sidecar_metadata_enabled() -> bool:
    return bool(getattr(django_settings, "BOOK_SCANNER_SIDECAR_METADATA_ENABLED", True))


def _read_bounded_regular_file(path: Path, max_bytes: int) -> bytes:
    """Read a small untrusted file without following symlinks; ``OSError`` if unusable."""
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    if path.is_symlink():
        raise OSError("refusing to follow a symlink")
    descriptor = os.open(path, flags)
    with os.fdopen(descriptor, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        if info.st_size > max_bytes:
            raise OSError("file exceeds the safe size limit")
        payload = handle.read(max_bytes + 1)
    if len(payload) > max_bytes:
        raise OSError("file exceeds the safe size limit")
    return payload


def read_sidecar_metadata(job: ImportJob) -> dict[str, Any] | None:
    """Fields of a Calibre-style ``metadata.opf`` next to a single-book source file.

    ``None`` when there is no usable sidecar: uploads, the setting is off, no
    ``metadata.opf`` next to the file, the folder holds several books, the sidecar is
    a symlink, too large, malformed, or rejected by defusedxml. A bad sidecar is
    logged and treated as absent; it never fails the import.
    """
    if not sidecar_metadata_enabled() or job.source_location is None or not job.source_path:
        return None
    # Probe the one fixed name first so folders without a sidecar cost no listing.
    probe = Path(job.source_path).parent / SIDECAR_METADATA_NAME
    try:
        if not probe.is_symlink() and not probe.exists():
            return None
    except OSError:
        return None
    folder = single_book_folder(job)
    if folder is None:
        logger.info(
            "sidecar_metadata_ignored",
            import_job_id=str(job.id),
            reason="not_single_book_folder",
        )
        return None
    path = folder.sidecar_file(SIDECAR_METADATA_NAME)
    if path is None:
        logger.warning(
            "sidecar_metadata_rejected",
            import_job_id=str(job.id),
            reason="not_regular_file",
        )
        return None
    try:
        payload = _read_bounded_regular_file(path, MAX_FORMAT_METADATA_BYTES)
        generated = opf_is_app_generated(payload)
        fields = {} if generated else parse_opf_metadata(payload)
    except (OSError, ET.ParseError, DefusedXmlException, ValueError) as exc:
        logger.warning(
            "sidecar_metadata_rejected",
            import_job_id=str(job.id),
            error_type=type(exc).__name__,
        )
        return None
    if generated:
        # Our own derived output (written for the library, #119), not external evidence.
        logger.info(
            "sidecar_metadata_ignored",
            import_job_id=str(job.id),
            reason="generated_by_ebook_server",
        )
        return None
    return {key: value for key, value in fields.items() if value} or None


def _is_placeholder(value: Any) -> bool:
    if isinstance(value, list):
        return all(_is_placeholder(item) for item in value)
    if not isinstance(value, str):
        return not value
    return not value.strip() or value.strip().casefold() in PLACEHOLDER_METADATA_VALUES


def _embedded_title(metadata: dict[str, Any]) -> str | None:
    """The title the file itself embeds, or ``None`` for a filename fallback.

    Uses the extractor's explicit ``title_source`` provenance. A real embedded title
    that happens to equal the filename stem (``Midworld.epub`` titled "Midworld") is
    still embedded evidence. Metadata without provenance is treated as embedded, so a
    contradicting sidecar goes to review instead of overwriting it.
    """
    title = metadata.get("title")
    if not isinstance(title, str) or _is_placeholder(title):
        return None
    if metadata.get(TITLE_SOURCE_KEY) == TITLE_SOURCE_FILENAME:
        return None
    return title


def _metadata_isbns(metadata: dict[str, Any]) -> set[str]:
    isbns: set[str] = set()
    for key in ("isbn", "isbn10", "isbn13", "identifier"):
        value = metadata.get(key)
        if isinstance(value, str):
            isbns.update(identification.normalize_isbns(value))
    return isbns


def _sidecar_conflicts(metadata: dict[str, Any], sidecar: dict[str, Any]) -> list[str]:
    """Fields where the sidecar contradicts the file's own embedded metadata."""
    conflicts: list[str] = []
    embedded_isbns = _metadata_isbns(metadata)
    sidecar_isbns = _metadata_isbns(sidecar)
    if embedded_isbns and sidecar_isbns and not embedded_isbns & sidecar_isbns:
        conflicts.append("isbn")
    # Explicit text-header credits are independent file evidence too (#43).
    # A filename-matching line or a nearby name-like line remains a weak hint:
    # neither may turn a sidecar into a conflict on its own.
    header = metadata.get(identification.TEXT_HEADER_EVIDENCE_KEY)
    header = header if isinstance(header, dict) else {}
    rules = header.get("rules")
    rules = rules if isinstance(rules, list) else []
    explicit_titles = [
        rule["title"]
        for rule in rules
        if isinstance(rule, dict)
        and rule.get("rule") in {"labelled title", "gutenberg header", "title by author"}
        and isinstance(rule.get("title"), str)
        and not _is_placeholder(rule["title"])
    ]
    embedded_title = _embedded_title(metadata)
    titles = [*explicit_titles, *([embedded_title] if embedded_title else [])]
    sidecar_title = sidecar.get("title")
    if isinstance(sidecar_title, str) and any(
        identification.title_similarity(title, sidecar_title) < SIDECAR_TITLE_CONFLICT_SCORE
        for title in titles
    ):
        conflicts.append("title")
    embedded_authors = [
        author
        for author in metadata.get("authors") or []
        if isinstance(author, str) and not _is_placeholder(author)
    ]
    sidecar_authors = [author for author in sidecar.get("authors") or [] if isinstance(author, str)]
    # ``authors`` contains explicit parser rules; ``likely_authors`` is deliberately
    # excluded. Keep the groups separate so agreeing embedded metadata cannot hide a
    # contradictory explicit header credit.
    header_authors = header.get("authors")
    header_authors = (
        [name for name in header_authors if isinstance(name, str) and not _is_placeholder(name)]
        if isinstance(header_authors, list)
        else []
    )
    if sidecar_authors and any(
        authors and identification.author_similarity(authors, sidecar_authors) == 0.0
        for authors in (embedded_authors, header_authors)
    ):
        conflicts.append("authors")
    return conflicts


def apply_sidecar_metadata(
    metadata: dict[str, Any],
    sidecar: dict[str, Any] | None,
) -> dict[str, Any]:
    """Merge sidecar fields into ``metadata`` without overwriting embedded values.

    The sidecar is recorded under ``SIDECAR_EVIDENCE_KEY`` with the fields it filled
    (provenance) and any conflicts. Only gaps are filled: a field the format extractor
    left empty, a placeholder, or a title whose ``title_source`` says it is only the
    filename stem (an embedded title equal to the stem is still embedded). A sidecar
    ISBN is added as ``isbn`` when the file has none, so the embedded ``identifier``
    survives.
    When the sidecar contradicts embedded ISBN, title, or authors, nothing is merged;
    the caller then routes the import to review instead of auto-accepting it.
    """
    if not sidecar:
        return metadata
    merged = dict(metadata)
    conflicts = _sidecar_conflicts(metadata, sidecar)
    applied: list[str] = []
    if not conflicts:
        for key in SIDECAR_METADATA_FIELDS:
            value = sidecar.get(key)
            if not value:
                continue
            if key == "identifier":
                if _metadata_isbns(sidecar) and not _metadata_isbns(metadata):
                    merged["isbn"] = value
                    applied.append("isbn")
                continue
            if key == "title":
                if _embedded_title(metadata) is None:
                    merged["title"] = value
                    merged[TITLE_SOURCE_KEY] = TITLE_SOURCE_SIDECAR
                    applied.append(key)
                continue
            if _is_placeholder(metadata.get(key)):
                merged[key] = value
                applied.append(key)
    merged[SIDECAR_EVIDENCE_KEY] = {
        "source": SIDECAR_METADATA_NAME,
        "fields": {key: sidecar[key] for key in SIDECAR_METADATA_FIELDS if sidecar.get(key)},
        "applied_fields": applied,
        "conflicts": conflicts,
    }
    return merged


def sidecar_conflicts_in(metadata: dict[str, Any]) -> list[str]:
    """Conflicts recorded by ``apply_sidecar_metadata`` (empty without a sidecar)."""
    sidecar = metadata.get(SIDECAR_EVIDENCE_KEY)
    if not isinstance(sidecar, dict):
        return []
    conflicts = sidecar.get("conflicts")
    return [str(item) for item in conflicts] if isinstance(conflicts, list) else []


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
    if is_audio_filename(path.name):
        raise IntakeError(f"{audio_skip_reason(path.name)}: {path.name}")
    if not _is_supported_file(path):
        raise IntakeError(f"unsupported file format: {path.name}")
    safe_name = normalize_filename(path.name)
    source_type = source_type or source_location.default_source_type
    if source_type not in dict(ImportJob.SourceType.choices):
        raise IntakeError(f"invalid source job type: {source_type}")
    if uploader is None:
        uploader = _make_system_user()

    stat = path.stat()
    return ImportJob.objects.create(
        source_type=source_type,
        state=ImportJob.State.DISCOVERED,
        source_location=source_location,
        uploader=uploader,
        source_path=str(path),
        original_filename=path.name,
        normalized_filename=safe_name,
        source_size=stat.st_size,
        source_mtime_ns=stat.st_mtime_ns,
    )


def _already_handled(source_location: SourceLocation, path: Path, normalized: str) -> bool:
    """True when this exact file version was imported, or has failed too often to retry."""
    try:
        stat = path.stat()
    except OSError:
        return True
    same_version = django_models.Q(source_size=stat.st_size, source_mtime_ns=stat.st_mtime_ns)
    jobs = ImportJob.objects.filter(source_path=normalized, source_location=source_location)
    if jobs.filter(same_version, state=ImportJob.State.COMPLETE).exists():
        return True
    failed = jobs.filter(
        same_version | django_models.Q(source_size__isnull=True),
        state=ImportJob.State.FAILED,
    )
    failures = failed.count()
    if failures == 0:
        return False
    max_attempts = int(getattr(django_settings, "BOOK_SCANNER_INGEST_MAX_AUTO_ATTEMPTS", 3))
    if failures >= max_attempts:
        return True
    latest = failed.order_by("-updated_at").first()
    if latest is not None:
        backoff = timedelta(minutes=15 * 2 ** (failures - 1))
        if timezone.now() - latest.updated_at < backoff:
            return True
    return False


@dataclass
class ByteBudget:
    """Archive bytes that may still be queued before the free-space reserve is reached."""

    remaining: int
    exhausted: bool = False

    def take(self, size: int) -> bool:
        if size > self.remaining:
            self.exhausted = True
            return False
        self.remaining -= size
        return True


# Jobs whose source file has not been archived yet; their size is still owed to
# the archive volume.
PENDING_ARCHIVE_STATES = (
    ImportJob.State.DISCOVERED,
    ImportJob.State.HASHING,
    ImportJob.State.METADATA_EXTRACTION,
    ImportJob.State.SIGNAL_NORMALIZATION,
    ImportJob.State.EXTERNAL_IDENTIFICATION,
    ImportJob.State.MATCHED,
    ImportJob.State.ARCHIVING,
)

# States in which an import job still holds a slot of its source's batch: queued or
# being processed by a worker. LOW_CONFIDENCE is excluded because those jobs are
# already archived and only wait for a human reviewer.
IN_FLIGHT_STATES = PENDING_ARCHIVE_STATES


def _source_limit(value: int | None, setting_name: str) -> int | None:
    """Per-source override, else the global setting; ``None`` means unlimited."""
    limit = value if value is not None else int(getattr(django_settings, setting_name, 0) or 0)
    return limit if limit > 0 else None


def source_ingest_capacity(
    source_location: SourceLocation,
    *,
    batch_size: int | None = None,
) -> int | None:
    """How many new import jobs may be queued for a source right now.

    Bounded by the source's batch size (jobs in flight at once) and its hourly rate.
    ``batch_size`` overrides the configured batch size (``ingest_sources --limit``;
    0 means unlimited). Returns ``None`` when neither limit applies.
    """
    if source_location.is_paused:
        return 0
    limits: list[int] = []
    batch = (
        (batch_size or None)
        if batch_size is not None
        else _source_limit(source_location.ingest_batch_size, "BOOK_SCANNER_INGEST_BATCH_SIZE")
    )
    jobs = ImportJob.objects.filter(source_location=source_location)
    if batch is not None:
        in_flight = jobs.filter(state__in=IN_FLIGHT_STATES).count()
        limits.append(max(batch - in_flight, 0))
    per_hour = _source_limit(
        source_location.ingest_max_per_hour, "BOOK_SCANNER_INGEST_MAX_PER_HOUR"
    )
    if per_hour is not None:
        recent = jobs.filter(created_at__gte=timezone.now() - timedelta(hours=1)).count()
        limits.append(max(per_hour - recent, 0))
    return min(limits) if limits else None


def archive_space_budget() -> ByteBudget | None:
    """Bytes that new imports may add to the archive before hitting the reserve.

    Free space on the archive volume, minus ``BOOK_SCANNER_STORAGE_MIN_FREE_BYTES``,
    minus the (approximate) size of imports already queued but not yet archived.
    ``None`` when the check is disabled or free space cannot be determined.
    """
    reserve = min_free_bytes()
    if reserve <= 0:
        return None
    free = _default_storage().free_bytes()
    if free is None:
        return None
    pending = (
        ImportJob.objects.filter(state__in=PENDING_ARCHIVE_STATES)
        .exclude(source_location__transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE)
        .aggregate(
            total=django_models.Sum(Coalesce("source_size", "size_bytes")),
        )["total"]
    )
    return ByteBudget(remaining=max(free - reserve - int(pending or 0), 0))


def discover_source_jobs_for_location(
    source_location: SourceLocation,
    *,
    uploader: Any | None = None,
    limit: int | None = None,
    byte_budget: ByteBudget | None = None,
    skipped_audio: list[int] | None = None,
) -> list[ImportJob]:
    """Create DISCOVERED jobs for new files, stopping after ``limit`` jobs if given.

    Audio files are never turned into jobs. Pass ``skipped_audio`` to receive how
    many were seen (one count appended per call).

    With ``byte_budget``, stop (and mark the budget exhausted) at the first file that
    would not fit, so a bulk import cannot queue more than the archive can hold.
    """
    from . import multipart

    jobs: list[ImportJob] = []
    folder_groups: dict[Path, list[multipart.DetectedGroup]] = {}
    skipped: list[Path] = []
    audio_skipped = 0
    if limit is not None and limit <= 0:
        if skipped_audio is not None:
            skipped_audio.append(0)
        return jobs
    for candidate in discover_source_paths(source_location):
        if is_audio_filename(candidate.name):
            audio_skipped += 1
            logger.debug(
                "intake_audio_skipped",
                source_location_id=source_location.pk,
                drm_protected=is_drm_audio_filename(candidate.name),
            )
            continue
        if not _is_supported_file(candidate):
            skipped.append(candidate)
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
        if active_job_exists or _already_handled(source_location, candidate, normalized):
            continue
        if byte_budget is not None and not referenced_files.is_index_in_place(source_location):
            try:
                size = candidate.stat().st_size
            except OSError:
                continue
            if not byte_budget.take(size):
                logger.warning(
                    "intake_space_budget_exhausted",
                    source_location_id=source_location.pk,
                    queued=len(jobs),
                )
                break
        try:
            job = create_source_import_job(candidate, source_location, uploader=uploader)
        except IntakeError:
            continue
        multipart.assign_discovered_job(job, source_location, folder_groups=folder_groups)
        jobs.append(job)
        if limit is not None and len(jobs) >= limit:
            break
    if skipped:
        # Unsupported files are never imported, but they must not vanish silently (#23).
        logger.warning(
            "source_files_skipped_unsupported",
            source_location_id=str(source_location.id),
            skipped_count=len(skipped),
            skipped_by_extension=skipped_extension_counts(skipped),
        )
    if audio_skipped:
        logger.info(
            "intake_audio_files_skipped",
            source_location_id=source_location.pk,
            count=audio_skipped,
            reason=AUDIO_SKIP_REASON,
        )
    if skipped_audio is not None:
        skipped_audio.append(audio_skipped)
    return jobs


def skipped_extension_label(path: Path) -> str:
    """Return the lowercase extension used to group unsupported files in reports."""
    return path.suffix.lower() or "(no extension)"


def skipped_extension_counts(paths: Iterable[Path]) -> dict[str, int]:
    """Count unsupported files per extension, most common first."""
    counts: dict[str, int] = {}
    for path in paths:
        label = skipped_extension_label(path)
        counts[label] = counts.get(label, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


UNSUPPORTED_SKIP_REASON = "unsupported format"


@dataclass(frozen=True)
class SourcePreview:
    """What discovery would do with every file in one source, without creating jobs.

    ``jobs`` are importable files. ``skipped`` are files that are never imported:
    unsupported formats (``"skipped": "unsupported"``, #23) and audio files
    (``"skipped": "audio"``, see docs/design/32-audiobooks.md). ``rows`` keeps
    discovery order.
    """

    rows: list[dict[str, Any]]

    @property
    def jobs(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if "skipped" not in row]

    @property
    def skipped(self) -> list[dict[str, Any]]:
        return [row for row in self.rows if "skipped" in row]

    @property
    def skipped_audio(self) -> int:
        return sum(1 for row in self.rows if row.get("skipped") == "audio")

    @property
    def skipped_by_extension(self) -> dict[str, int]:
        return skipped_extension_counts(Path(row["path"]) for row in self.skipped)


def preview_source_location(source_location: SourceLocation) -> SourcePreview:
    """Classify every candidate file in a source without creating jobs."""
    from . import multipart

    rows: list[dict[str, Any]] = []
    folder_groups: dict[Path, list[multipart.DetectedGroup]] = {}
    for candidate in discover_source_paths(source_location):
        base: dict[str, Any] = {
            "source_location_id": str(source_location.id),
            "path": str(candidate),
            "filename": candidate.name,
        }
        if is_audio_filename(candidate.name):
            rows.append(
                {
                    **base,
                    "skipped": "audio",
                    "extension": skipped_extension_label(candidate),
                    "drm_protected": is_drm_audio_filename(candidate.name),
                    "reason": audio_skip_reason(candidate.name),
                }
            )
            continue
        if not _is_supported_file(candidate):
            rows.append(
                {
                    **base,
                    "skipped": "unsupported",
                    "extension": skipped_extension_label(candidate),
                    "reason": UNSUPPORTED_SKIP_REASON,
                }
            )
            continue
        row: dict[str, Any] = {**base, "source_type": source_location.default_source_type}
        if source_location.path_layouts:
            # Lets an admin check a folder layout before importing anything.
            hints = source_layout.hints_for_source_file(source_location, str(candidate))
            row["path_layout"] = hints.as_evidence() if hints is not None else None
        if multipart.detection_enabled():
            folder = candidate.parent
            if folder not in folder_groups:
                folder_groups[folder] = multipart.groups_for_folder(folder)
            for detected in folder_groups[folder]:
                part = next((p for p in detected.parts if p.filename == candidate.name), None)
                if part is not None:
                    row["multipart_group"] = detected.key
                    row["part_label"] = part.label
                    break
        rows.append(row)
    return SourcePreview(rows=rows)


def preview_source_jobs(
    source_location: SourceLocation,
) -> list[dict[str, Any]]:
    """Rows for files discovery would import, plus rows for skipped audio files.

    Audio rows carry ``"skipped": "audio"`` and a ``reason``; they never become jobs.
    Unsupported-format files are listed by ``preview_source_location``.
    """
    return [
        row
        for row in preview_source_location(source_location).rows
        if row.get("skipped") != "unsupported"
    ]


def compute_sha256(path: str) -> str:
    digest = hashlib.sha256()
    file_path = Path(path)
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_upload_to_staging(uploaded_file: Any, staging_root: Path) -> tuple[str, int]:
    ensure_path_storage_ready(staging_root)
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
    """Embedded metadata of ``file_path`` with explicit title provenance.

    Every result carries ``title_source``: ``"embedded"`` when the title came from
    the file's own metadata, ``"filename"`` when the file had none and the filename
    stem stands in for it. Consumers must use this field rather than comparing the
    title with the filename, because a real embedded title often equals the stem.
    """
    metadata = _extract_format_metadata(
        file_path,
        detected_format,
        original_filename=original_filename,
    )
    return _with_title_provenance(metadata, original_filename)


def _with_title_provenance(metadata: dict[str, Any], original_filename: str) -> dict[str, Any]:
    title = metadata.get("title")
    if isinstance(title, str) and title.strip():
        metadata["title"] = str(title)
        metadata[TITLE_SOURCE_KEY] = TITLE_SOURCE_EMBEDDED
    else:
        metadata["title"] = Path(original_filename).stem
        metadata[TITLE_SOURCE_KEY] = TITLE_SOURCE_FILENAME
    return metadata


def _extract_format_metadata(
    file_path: str,
    detected_format: str,
    *,
    original_filename: str,
) -> dict[str, Any]:
    """Per-format extraction; ``title`` is ``None`` when the file embeds none."""
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
        if detected_format in EBOOK_META_FORMATS:
            return _extract_ebook_meta_metadata(
                file_path,
                detected_format,
                original_filename=original_filename,
            )
    except (ValidationError, DefusedXmlException):
        raise
    except Exception:
        return {"title": None}
    return {"title": None}


def _extract_text_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    payload = _read_metadata_sample(Path(file_path))
    text, encoding = _decode_text_sample(payload)
    metadata: dict[str, Any] = {
        "title": None,
        "raw_metadata": {"encoding": encoding},
        "container_type": "txt",
    }
    # Plain text has no metadata container; its first lines often name title/author.
    # Only the extracted hints and the rules that fired are kept, never the lines.
    header = parse_text_header(text, Path(original_filename).stem)
    if header is not None:
        metadata[identification.TEXT_HEADER_EVIDENCE_KEY] = header
    return metadata


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
        "title": title,
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
        "title": title,
        "authors": authors,
        "language": language,
        "description": description,
        "raw_metadata": {},
        "container_type": "fb2",
    }


def _extract_cbz_metadata(file_path: str, *, original_filename: str) -> dict[str, Any]:
    fallback: dict[str, Any] = {
        "title": None,
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
        "title": value("Title"),
        "authors": authors,
        "subjects": identification.normalize_subjects((value("Genre") or "").split(",")),
        "language": value("LanguageISO"),
        "description": value("Summary"),
        "publisher": value("Publisher"),
        "raw_metadata": {"comic_info": True},
        "container_type": "cbz",
    }


def _run_ebook_meta_to_opf(file_path: str) -> bytes | None:
    """Return the OPF document Calibre's ``ebook-meta`` writes for ``file_path``.

    ``None`` means "no usable metadata" (tool missing, failed, timed out, or wrote
    nothing within the size cap); the caller then falls back to filename signals.
    The command goes through the conversion runner, which kills the whole process
    group on timeout so a hung Calibre helper cannot outlive the job.
    """
    # Imported here: conversion imports this module at load time.
    from .conversion import ConversionError, _run_conversion_command

    binary = shutil.which(EBOOK_META_BINARY)
    if binary is None:
        logger.warning("ebook_meta_unavailable", binary=EBOOK_META_BINARY)
        return None
    with TemporaryDirectory(prefix="ebook-meta-") as workdir:
        opf_path = Path(workdir) / "metadata.opf"
        try:
            result = _run_conversion_command(
                [binary, str(Path(file_path).resolve()), "--to-opf", str(opf_path)],
                timeout=EBOOK_META_TIMEOUT_SECONDS,
            )
        except (OSError, ConversionError) as exc:
            logger.warning("ebook_meta_failed", error_type=type(exc).__name__)
            return None
        if result.returncode != 0:
            logger.warning("ebook_meta_failed", returncode=result.returncode)
            return None
        try:
            if opf_path.stat().st_size > MAX_FORMAT_METADATA_BYTES:
                logger.warning("ebook_meta_output_too_large")
                return None
            payload = opf_path.read_bytes()
        except OSError:
            return None
    return payload or None


def _extract_ebook_meta_metadata(
    file_path: str,
    detected_format: str,
    *,
    original_filename: str,
) -> dict[str, Any]:
    """MOBI and other Kindle-family metadata, read via ``ebook-meta --to-opf``."""
    fallback: dict[str, Any] = {
        "title": None,
        "raw_metadata": {},
        "container_type": detected_format,
    }
    payload = _run_ebook_meta_to_opf(file_path)
    if payload is None:
        return fallback
    try:
        fields = parse_opf_metadata(payload)
    except (ET.ParseError, DefusedXmlException) as exc:
        logger.warning("ebook_meta_opf_unparseable", error_type=type(exc).__name__)
        return fallback
    return {
        **fields,
        "title": fields["title"],
        "raw_metadata": {"metadata_tool": EBOOK_META_BINARY},
        "container_type": detected_format,
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
        "title": title,
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
                "title": None,
                "raw_metadata": {},
                "container_type": "epub",
            }
        root = ET.fromstring(container_doc)
        rootfile = root.find(".//{urn:oasis:names:tc:opendocument:xmlns:container}rootfile")
        opf_path = rootfile.attrib.get("full-path") if rootfile is not None else None
        if not opf_path:
            return {
                "title": None,
                "raw_metadata": {},
                "container_type": "epub",
            }
        if not isinstance(opf_path, str):
            return {
                "title": None,
                "raw_metadata": {},
                "container_type": "epub",
            }
        if opf_path.startswith("/"):
            opf_path = opf_path[1:]
        opf_bytes = _read_epub_metadata_member(archive, opf_path)
        opf_root = ET.fromstring(opf_bytes)
        # Namespace-safe extraction; these fields are best-effort only.
        title = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}title")
        authors = _epub_creator_authors(opf_root)
        language = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}language")
        description = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}description")
        # Publisher and date are evidence for the publisher bonus and later review (#37).
        publisher = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}publisher")
        raw_date = _find_text_any(opf_root, ".//{http://purl.org/dc/elements/1.1/}date")
        subjects = identification.normalize_subjects(
            [
                "".join(element.itertext())
                for element in opf_root.iterfind(".//{http://purl.org/dc/elements/1.1/}subject")
            ]
        )
        identifier = _pick_epub_identifier(
            [
                (element.attrib, "".join(element.itertext()).strip())
                for element in opf_root.iterfind(".//{http://purl.org/dc/elements/1.1/}identifier")
            ],
        )
        return {
            "title": title,
            "authors": authors,
            "language": language,
            "description": description,
            "subjects": subjects,
            "identifier": identifier,
            "publisher": publisher[:MAX_EPUB_TEXT_FIELD] if publisher else None,
            "date": raw_date[:MAX_EPUB_TEXT_FIELD] if raw_date else None,
            "raw_metadata": {
                "container_xml": container_doc.decode("utf-8", errors="ignore"),
            },
            "container_type": "epub",
        }


def _epub_creator_authors(opf_root: Any) -> list[str]:
    """Every author-role dc:creator, in order: coauthors are all genuine evidence.

    Creators with another ``opf:role`` (editor, translator, ...) are skipped; when no
    creator is marked as an author, the first creator is kept as before.
    """
    creators = [
        (element, " ".join("".join(element.itertext()).split()))
        for element in opf_root.iterfind(".//{http://purl.org/dc/elements/1.1/}creator")
    ]
    creators = [(element, text) for element, text in creators if text]
    authors = [text for element, text in creators if _opf_creator_is_author(element)]
    if not authors and creators:
        authors = [creators[0][1]]
    return list(dict.fromkeys(authors))


def _pick_epub_identifier(entries: list[tuple[Any, str]]) -> str | None:
    """Choose the best dc:identifier: an ISBN anywhere in the list beats the first entry.

    Calibre-produced EPUBs commonly list a UUID first and the ISBN later.
    """
    texts = [(attrs, text) for attrs, text in entries if text]
    if not texts:
        return None
    for attrs, text in texts:  # explicitly marked ISBNs first
        scheme = " ".join(str(value) for value in attrs.values()).lower()
        if ("isbn" in scheme or "isbn" in text.lower()) and identification.normalize_isbns(text):
            return text
    for _attrs, text in texts:
        if "uuid" in text.lower():
            continue
        if identification.normalize_isbns(text):
            return text
    return texts[0][1]


def _opf_creator_is_author(element: Any) -> bool:
    """True unless the dc:creator carries a non-author ``opf:role`` (editor, translator...)."""
    role = next(
        (str(value) for key, value in element.attrib.items() if _xml_local_name(key) == "role"),
        "",
    )
    return not role or role.strip().lower() == "aut"


def opf_is_app_generated(payload: bytes) -> bool:
    """True when an OPF carries ``<meta name="ebook-server:generated" content="true"/>``.

    That marker is what this application writes into the ``metadata.opf`` it generates
    for a book folder (#119); such a file is our own output, never sidecar evidence.
    Parsed with defusedxml like every OPF here, so hostile or malformed documents raise
    ``DefusedXmlException`` / ``ParseError`` for the caller to handle.
    """
    root = ET.fromstring(payload)
    for node in root.iter():
        tag = node.tag if isinstance(node.tag, str) else ""
        if tag.rsplit("}", 1)[-1] != "meta":
            continue
        if node.attrib.get("name") == GENERATED_OPF_META_NAME:
            return str(node.attrib.get("content", "")).strip().lower() == "true"
    return False


def parse_opf_metadata(payload: bytes) -> dict[str, Any]:
    """Parse the Dublin-Core fields of a standalone OPF package document.

    Shared by every OPF this module reads outside an EPUB container: the document
    ``ebook-meta --to-opf`` writes for Kindle-family files and a Calibre
    ``metadata.opf`` sidecar. Parsing goes through defusedxml, so a document with
    entity declarations raises ``DefusedXmlException``; malformed XML raises
    ``ParseError``. Callers decide how to degrade.
    """
    root = ET.fromstring(payload)

    def texts(name: str) -> list[str]:
        values = []
        for element in root.iterfind(f".//{DC_NAMESPACE}{name}"):
            value = " ".join("".join(element.itertext()).split())
            if value:
                values.append(value)
        return values

    def first(name: str) -> str | None:
        values = texts(name)
        return values[0] if values else None

    authors: list[str] = []
    for element in root.iterfind(f".//{DC_NAMESPACE}creator"):
        name = " ".join("".join(element.itertext()).split())
        if name and name not in authors and _opf_creator_is_author(element):
            authors.append(name)
    return {
        "title": first("title"),
        "authors": authors,
        "language": first("language"),
        "description": first("description"),
        "publisher": first("publisher"),
        "subjects": identification.normalize_subjects(texts("subject")),
        "identifier": _pick_epub_identifier(
            [
                (element.attrib, "".join(element.itertext()).strip())
                for element in root.iterfind(f".//{DC_NAMESPACE}identifier")
            ],
        ),
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
    *,
    signals: identification.IdentificationInput,
    outcomes: list[identification.ProviderOutcome],
    passed_over: Iterable[identification.IdentificationCandidate] = (),
) -> None:
    """Record the intake search run and its candidates.

    ``passed_over`` candidates stay in REVIEW state whatever their score.
    """
    with transaction.atomic():
        # Ensure idempotence if jobs are retried: drop the earlier intake attempt.
        metadata_search.discard_intake_runs(job)
        search = metadata_search.record_search(
            trigger=MetadataSearch.Trigger.INTAKE,
            signals=signals,
            outcomes=outcomes,
            candidate_count=len(candidates),
            import_job=job,
            requested_by_id=job.uploader_id,
        )
        metadata_search.save_candidates(
            search=search,
            candidates=candidates,
            match_threshold=AUTO_ACCEPT_SCORE,
            passed_over=passed_over,
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
    inferred_title = _fit(
        ReviewTask,
        "inferred_title",
        signals.best_title() or Path(job.original_filename).stem,
    )
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


def variant_source_type_for_job(job: ImportJob) -> str:
    """FileVariant provenance for an import: upload, incoming folder, or network source."""
    mapping: dict[str, str] = {
        ImportJob.SourceType.UPLOAD: FileVariant.SourceType.UPLOAD,
        ImportJob.SourceType.INCOMING_FOLDER: FileVariant.SourceType.INCOMING_FOLDER,
        ImportJob.SourceType.NETWORK_SOURCE: FileVariant.SourceType.NETWORK_SOURCE,
    }
    return mapping.get(str(job.source_type), FileVariant.SourceType.UPLOAD)


def _create_or_reuse_file_variant(
    job: ImportJob,
    book: Book,
    *,
    storage_identifier: str | None = None,
    source_type: str = "",
    part_index: int | None = None,
    part_label: str = "",
    reuse_identical: bool = True,
    converter_options: dict[str, Any] | None = None,
) -> FileVariant:
    """The job's FileVariant on ``book``; by default any existing identical file is reused.

    ``reuse_identical=False`` (multi-part parts, #60) always creates a new variant on
    ``book`` from the job's own archived copy, so another Book's variant is never
    returned for this Book.
    """
    if not source_type:
        source_type = variant_source_type_for_job(job)
    if reuse_identical:
        duplicate_variant = FileVariant.objects.filter(sha256=job.sha256).first()
        if duplicate_variant is not None:
            return duplicate_variant

    storage = _default_storage()
    if not storage_identifier and job.storage_identifier:
        # Archived while awaiting review: move it from _unsorted/ into the book folder.
        archive_layout.place_pending_original(job, book, _storage_for_location(job))
    storage_id = storage_identifier or job.storage_identifier
    if not storage_id:
        if Path(job.source_path).exists():
            storage_location = _default_storage_location()
            storage.ensure_free_space(Path(job.source_path).stat().st_size)
            storage_id = archive_layout.publish_original(storage, job, book, job.source_path)
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
        part_index=part_index,
        part_label=_fit(FileVariant, "part_label", part_label),
        converter_options=converter_options or {},
        **referenced_files.reference_fields_for_job(job),
    )


def discard_redundant_archive_copy(job: ImportJob, *, keep: FileVariant) -> None:
    """Delete ``job``'s own archive copy once ``keep`` (same bytes) stands in for it.

    Only a copy this application wrote to the default archive is removed, never a file
    referenced in place, never the file ``keep`` (or any other variant) points at, and
    only after the surrounding transaction commits. The source itself is untouched.
    """
    identifier = job.storage_identifier
    location = job.storage_location
    if not identifier or location is None or location.read_only:
        return
    if location.pk != _default_storage_location().pk:
        return
    if keep.storage_location_id == location.pk and keep.storage_identifier == identifier:
        return
    if FileVariant.objects.filter(
        storage_location_id=location.pk, storage_identifier=identifier
    ).exists():
        return
    if keep.sha256 != job.sha256:
        return

    def _delete() -> None:
        try:
            _default_storage().delete(identifier)
        except Exception as exc:  # an orphaned copy is harmless; never fail the decision
            logger.warning(
                "intake_redundant_archive_copy_not_deleted",
                import_job_id=str(job.id),
                error_type=type(exc).__name__,
            )
            return
        logger.info(
            "intake_redundant_archive_copy_deleted",
            import_job_id=str(job.id),
            kept_file_variant_id=keep.id,
        )

    transaction.on_commit(_delete)


def _try_extract_front_cover(variant: FileVariant, job: ImportJob | None = None) -> None:
    from . import covers

    try:
        cover = covers.extract_front_cover(variant, import_job=job)
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


def extract_front_cover_job(file_variant_id: int, import_job_id: str | None = None) -> None:
    """Background job: extract a variant's front cover once.

    Idempotent: ``covers.extract_front_cover`` returns the Book's existing front cover,
    so a repeated or duplicate delivery does no storage work.
    """
    variant = (
        FileVariant.objects.select_related("book", "storage_location")
        .filter(pk=file_variant_id)
        .first()
    )
    if variant is None:
        logger.info("cover_extraction_variant_missing", file_variant_id=file_variant_id)
        return
    job = (
        ImportJob.objects.select_related("source_location").filter(pk=import_job_id).first()
        if import_job_id
        else None
    )
    _try_extract_front_cover(variant, job)


def schedule_front_cover_extraction(
    variant: FileVariant,
    job: ImportJob | None = None,
    *,
    inline_fallback: bool = True,
) -> None:
    """Extract the front cover in a background job after the current transaction commits.

    Cover extraction copies the whole archived file from network storage and parses or
    renders it, so review decisions must not do it inside the request or while holding
    row locks. Without a configured queue (development only; production requires Redis)
    the extraction runs inline after commit when ``inline_fallback`` is set, and is
    skipped otherwise (batch review); it is optional and never blocks the decision.
    """
    if CoverImage.objects.filter(
        book_id=variant.book_id,
        position=CoverImage.CoverPosition.FRONT,
    ).exists():
        return
    file_variant_id = variant.pk
    import_job_id = str(job.pk) if job is not None else None

    def dispatch() -> None:
        try:
            queue = get_queue()
            if queue is None:
                if inline_fallback:
                    extract_front_cover_job(file_variant_id, import_job_id)
                else:
                    logger.warning(
                        "cover_extraction_skipped_no_queue",
                        file_variant_id=file_variant_id,
                    )
                return
            queue.enqueue(extract_front_cover_job, file_variant_id, import_job_id)
        except Exception as exc:  # optional work; the committed decision stands
            logger.warning(
                "cover_extraction_dispatch_failed",
                file_variant_id=file_variant_id,
                error_type=type(exc).__name__,
            )

    transaction.on_commit(dispatch)


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
        title=_fit(Book, "title", title),
        publisher=_fit(Book, "publisher", publisher),
        language=_fit(Book, "language", language),
        description=description or "",
        edition=_fit(Book, "edition", edition),
    )
    book.publication_year = clean_publication_year(publication_year)
    for raw_author in authors or []:
        author, _ = Author.objects.get_or_create(full_name=_fit(Author, "full_name", raw_author))
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
    source_type: str = "",
    selected_candidate: MetadataCandidate | None = None,
    reviewer: Any | None = None,
    decision: str = "",
    decision_notes: str = "",
    decision_payload: dict[str, Any] | None = None,
    propagate_to_siblings: bool = True,
    inline_cover_fallback: bool = True,
) -> FileVariant:
    """Resolve a reviewed import; ``source_type`` defaults to the job's origin.

    Other formats of the same book waiting in review are then offered ``book``
    (``_propagate_book_to_siblings``), under the same sibling-group lock. Front-cover
    extraction is queued after commit (``schedule_front_cover_extraction``); pass
    ``inline_cover_fallback=False`` where it must never run in the caller's process.
    """
    with _sibling_group_lock(job):
        variant = _resolve_import(
            job=job,
            review_task=review_task,
            book=book,
            source_type=source_type,
            selected_candidate=selected_candidate,
            reviewer=reviewer,
            decision=decision,
            decision_notes=decision_notes,
            decision_payload=decision_payload,
            inline_cover_fallback=inline_cover_fallback,
        )
        if propagate_to_siblings:
            _propagate_book_to_siblings(job, book, inline_cover_fallback=inline_cover_fallback)
    return variant


def _resolve_import(
    *,
    job: ImportJob,
    review_task: ReviewTask,
    book: Book,
    source_type: str,
    selected_candidate: MetadataCandidate | None,
    reviewer: Any | None,
    decision: str,
    decision_notes: str,
    decision_payload: dict[str, Any] | None,
    inline_cover_fallback: bool = True,
) -> FileVariant:
    if not decision:
        decision = cast(str, ReviewTask.Decision.ACCEPT_CANDIDATE)
    if review_task.task_type == ReviewTask.TaskType.MULTIPART_WORK:
        from . import multipart

        # Any identifying action on a multi-part review confirms the grouping: every
        # archived part becomes an ordered part FileVariant of ``book``.
        confirmed = multipart.confirm_parts(
            lead_job=job,
            review_task=review_task,
            book=book,
            inline_cover_fallback=inline_cover_fallback,
        )
        payload = dict(decision_payload if decision_payload is not None else {})
        payload["multipart_confirmed"] = {
            "import_group_id": str(job.import_group_id),
            "file_variant_ids": [part.id for part in confirmed.variants],
            # Detected parts the reviewer acknowledged confirming the book without.
            "missing_parts": confirmed.absent,
            # Acknowledged parts identical to another Book's file, included as parts of
            # this Book (own variant, identical file recorded as provenance).
            "duplicate_parts": confirmed.duplicates,
        }
        decision_payload = payload
        job.refresh_from_db()
        variant = job.file_variant or confirmed.variants[0]
    else:
        variant = _create_or_reuse_file_variant(job, book, source_type=source_type)
        schedule_front_cover_extraction(variant, job, inline_fallback=inline_cover_fallback)
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


# --- Formats of one book (#39) ---------------------------------------------------------
#
# With ``SourceLocation.group_sibling_formats`` (opt-in), files in one source folder with
# the same case-folded file name and different extensions are "siblings": probably
# formats of one book. A sibling's already linked Book is offered as one more, strong
# candidate; it is never a bypass of verification:
#
# * the file is still looked up externally, and its candidates are stored;
# * the Book is scored with the provider scorer plus ``SIBLING_FORMAT_BONUS`` and must
#   reach the auto-accept threshold;
# * embedded ISBNs that conflict with the Book's ISBNs, or a confident provider match for
#   a different book, send the file to review instead;
# * a review task a reviewer has CLAIMED is never resolved automatically.

SIBLING_EVIDENCE_KEY = "format_sibling_of"
SIBLING_REVIEW_PAYLOAD_KEY = "format_sibling"


@dataclass(frozen=True)
class SiblingAssessment:
    sibling: ImportJob
    book: Book
    score: float
    accepted: bool
    reason: str

    def as_evidence(self) -> dict[str, Any]:
        return {
            "import_job": str(self.sibling.id),
            "filename": self.sibling.original_filename,
            "book_id": self.book.pk,
            "score": round(self.score, 4),
            "accepted": self.accepted,
            "reason": self.reason,
        }


def _groups_sibling_formats(job: ImportJob) -> bool:
    location = job.source_location
    return bool(location is not None and location.group_sibling_formats and job.source_path)


@contextmanager
def _sibling_group_lock(job: ImportJob) -> Iterator[None]:
    """Serialise sibling decisions for one source (short database sections only).

    Without it, two formats auto-accepted at the same time could provision two Books,
    and a file reaching review just after its sibling was linked would be missed.
    Locks the SourceLocation row; a no-op for sources without format grouping.
    """
    if not _groups_sibling_formats(job):
        yield
        return
    with transaction.atomic():
        list(
            SourceLocation.objects.select_for_update()
            .filter(pk=job.source_location_id)
            .values_list("pk", flat=True)
        )
        yield


def _sibling_jobs(job: ImportJob) -> list[ImportJob]:
    """Import jobs for other formats of the same book: same folder, same file name."""
    if not _groups_sibling_formats(job):
        return []
    path = Path(job.source_path)
    stem = path.stem.casefold()
    # ``source_path`` is stored with the OS separator (``str(Path.resolve())``), so
    # compare Path objects rather than "/"-joined string prefixes.
    candidates = (
        ImportJob.objects.filter(
            source_location_id=job.source_location_id,
            source_path__startswith=str(path.parent),
        )
        .exclude(pk=job.pk)
        .exclude(source_path=job.source_path)
        .select_related("book", "source_location")
        .order_by("created_at", "id")
    )
    return [
        candidate
        for candidate in candidates
        if Path(candidate.source_path).parent == path.parent
        and Path(candidate.source_path).stem.casefold() == stem
    ]


def sibling_group_key(job: ImportJob) -> tuple[int, str, str] | None:
    """Key shared by all formats of one book (``_sibling_jobs``), or ``None``.

    ``(source location id, parent folder, case-folded file stem)``; only for sources with
    ``group_sibling_formats``.
    """
    if not _groups_sibling_formats(job) or job.source_location_id is None:
        return None
    path = Path(job.source_path)
    return (job.source_location_id, str(path.parent), path.stem.casefold())


def _resolved_sibling(job: ImportJob) -> ImportJob | None:
    for sibling in _sibling_jobs(job):
        if sibling.state == ImportJob.State.COMPLETE and sibling.book is not None:
            return sibling
    return None


def _isbn13s(values: Iterable[str]) -> set[str]:
    return {
        isbn
        for value in values
        for isbn in identification.normalize_isbns(value)
        if len(isbn) == 13
    }


def _book_as_candidate(
    book: Book, *, prefer_isbns: set[str]
) -> identification.IdentificationCandidate:
    identifiers = list(
        book.identifiers.filter(
            kind__in=(Identifier.IdentifierType.ISBN10, Identifier.IdentifierType.ISBN13)
        ).values_list("value", flat=True)
    )
    # Use the Book's ISBN that equals the file's when there is one, so scoring sees it.
    matching = [value for value in identifiers if _isbn13s([value]) & prefer_isbns]
    ordered = matching + [value for value in identifiers if value not in matching]
    isbn10 = next((value for value in ordered if len(value) == 10), None)
    isbn13 = next((value for value in ordered if len(value) == 13), None)
    return identification.IdentificationCandidate(
        provider_id="library",
        provider_ref=f"book:{book.pk}",
        title=book.title,
        authors=[author.full_name for author in book.authors.all()],
        isbn10=isbn10,
        isbn13=isbn13,
        language=book.language or None,
        description=None,
        publisher=book.publisher or None,
        edition=book.edition or None,
        publication_year=None,
        score=0.0,
        raw={},
    )


def _assess_sibling_book(
    *,
    signals: identification.IdentificationInput,
    sibling: ImportJob,
    book: Book,
    top_candidate: identification.IdentificationCandidate | None,
    file_authors: list[str],
) -> SiblingAssessment:
    """Whether a file may join its sibling's Book (see the block comment above)."""
    file_isbns = _isbn13s(signals.isbns)
    book_isbns = _isbn13s(
        book.identifiers.filter(
            kind__in=(Identifier.IdentifierType.ISBN10, Identifier.IdentifierType.ISBN13)
        ).values_list("value", flat=True)
    )
    book_candidate = _book_as_candidate(book, prefer_isbns=file_isbns)
    score = min(
        1.0,
        identification.score_candidate(signals, book_candidate).score
        + identification.SIBLING_FORMAT_BONUS,
    )
    if file_isbns and book_isbns and not file_isbns & book_isbns:
        reason, accepted = "conflicting_isbn", False
    elif identification.authors_conflict(book_candidate.authors, file_authors):
        reason, accepted = BLOCK_AUTHOR_CONFLICT, False
    elif (
        top_candidate is not None
        and top_candidate.score >= AUTO_ACCEPT_SCORE
        and not identification.same_book(top_candidate, book_candidate)
    ):
        reason, accepted = "provider_match_differs", False
    elif score < AUTO_ACCEPT_SCORE:
        reason, accepted = "below_threshold", False
    else:
        reason, accepted = "accepted", True
    return SiblingAssessment(
        sibling=sibling, book=book, score=score, accepted=accepted, reason=reason
    )


def _record_sibling_evidence(job: ImportJob, assessment: SiblingAssessment) -> None:
    evidence = dict(job.evidence) if isinstance(job.evidence, dict) else {}
    evidence[SIBLING_EVIDENCE_KEY] = assessment.as_evidence()
    job.evidence = evidence
    _save_job(job, update_fields=["evidence", "updated_at"])


def _top_saved_candidate(job: ImportJob) -> identification.IdentificationCandidate | None:
    row = job.metadata_candidates.order_by("-score", "id").first()
    if row is None:
        return None
    return identification.IdentificationCandidate(
        provider_id=row.provider_id,
        provider_ref=row.provider_ref,
        title=row.title,
        authors=[author for author in row.authors or [] if isinstance(author, str)],
        isbn10=row.isbn10 or None,
        isbn13=row.isbn13 or None,
        language=row.language or None,
        description=row.description or None,
        publisher=row.publisher or None,
        edition=None,
        publication_year=row.publication_year,
        score=float(row.score),
        raw={},
    )


def _annotate_review_task(task: ReviewTask, payload: dict[str, Any]) -> None:
    decision_payload = (
        dict(task.decision_payload) if isinstance(task.decision_payload, dict) else {}
    )
    decision_payload[SIBLING_REVIEW_PAYLOAD_KEY] = payload
    task.decision_payload = decision_payload
    task.save(update_fields=["decision_payload", "updated_at"])


def _offer_book_to_waiting_job(
    waiting: ImportJob,
    book: Book,
    *,
    source_job: ImportJob,
    inline_cover_fallback: bool = True,
) -> bool:
    """Resolve a sibling waiting in review with ``book`` if the evidence allows it.

    Call under ``_sibling_group_lock``. Returns whether the job was linked.
    """
    if waiting.state != ImportJob.State.LOW_CONFIDENCE or not waiting.storage_identifier:
        return False
    task = (
        ReviewTask.objects.select_for_update()
        .filter(
            import_job=waiting,
            state__in=(ReviewTask.State.OPEN, ReviewTask.State.CLAIMED),
        )
        .first()
    )
    if task is None:
        return False
    if task.state == ReviewTask.State.CLAIMED:
        # A reviewer is working on it: never decide for them, only point at the Book.
        _annotate_review_task(
            task,
            {
                "import_job": str(source_job.id),
                "filename": source_job.original_filename,
                "book_id": book.pk,
                "accepted": False,
                "reason": "claimed_by_reviewer",
            },
        )
        return False
    evidence = waiting.evidence if isinstance(waiting.evidence, dict) else {}
    signals = identification.build_identification_input(
        source_path=waiting.source_path,
        original_filename=waiting.original_filename,
        extracted_metadata=evidence,
    )
    assessment = _assess_sibling_book(
        signals=signals,
        sibling=source_job,
        book=book,
        top_candidate=_top_saved_candidate(waiting),
        file_authors=identification.evidence_author_names(evidence),
    )
    _record_sibling_evidence(waiting, assessment)
    if not assessment.accepted:
        _annotate_review_task(task, assessment.as_evidence())
        logger.info(
            "intake_format_sibling_not_linked",
            import_job_id=str(waiting.id),
            sibling_import_job_id=str(source_job.id),
            book_id=book.pk,
            reason=assessment.reason,
        )
        return False
    _resolve_import(
        job=waiting,
        review_task=task,
        book=book,
        source_type="",
        selected_candidate=None,
        reviewer=None,
        decision=cast(str, ReviewTask.Decision.MERGED_BOOK),
        decision_notes=(
            f"Another format of '{source_job.original_filename}' (same folder and file "
            f"name); linked automatically at score {assessment.score:.2f}."
        ),
        decision_payload={
            "rule": "group_sibling_formats",
            SIBLING_REVIEW_PAYLOAD_KEY: assessment.as_evidence(),
        },
        inline_cover_fallback=inline_cover_fallback,
    )
    logger.info(
        "intake_linked_format_sibling",
        import_job_id=str(waiting.id),
        sibling_import_job_id=str(source_job.id),
        book_id=book.pk,
        score=assessment.score,
        from_review=True,
    )
    return True


def _propagate_book_to_siblings(
    job: ImportJob,
    book: Book,
    *,
    inline_cover_fallback: bool = True,
) -> None:
    """Offer ``book`` to other formats of it that wait in review.

    Siblings not processed yet consider it themselves in ``process_import_job``. A
    failure here never undoes the already completed ``job``.
    """
    if not _groups_sibling_formats(job):
        return
    with _sibling_group_lock(job):
        for sibling in _sibling_jobs(job):
            if sibling.state != ImportJob.State.LOW_CONFIDENCE:
                continue
            try:
                with transaction.atomic():
                    _offer_book_to_waiting_job(
                        sibling,
                        book,
                        source_job=job,
                        inline_cover_fallback=inline_cover_fallback,
                    )
            except Exception:
                logger.exception(
                    "intake_sibling_propagation_failed",
                    import_job_id=str(job.id),
                    sibling_import_job_id=str(sibling.id),
                )


def _archive_for_review(
    job: ImportJob,
    candidates: list[identification.IdentificationCandidate],
) -> None:
    """Create the review task, archive the original, and leave the job LOW_CONFIDENCE."""
    with transaction.atomic():
        # Rolled back with the task if this worker no longer owns the job.
        _create_review_task(job=job, candidates=candidates)
        job.state = ImportJob.State.LOW_CONFIDENCE
        _save_job(job, update_fields=["state", "updated_at"])
    _archive_awaiting_review(job)
    if not _groups_sibling_formats(job):
        return
    with _sibling_group_lock(job):
        # A sibling linked while this file was being identified did not see it waiting;
        # offer its Book now. A sibling already assessed above is not offered again.
        if SIBLING_EVIDENCE_KEY in (job.evidence or {}):
            return
        sibling = _resolved_sibling(job)
        if sibling is not None and sibling.book is not None:
            _offer_book_to_waiting_job(job, sibling.book, source_job=sibling)


def _complete_with_book(
    job: ImportJob,
    book: Book,
    *,
    destination: str,
    storage_location: StorageLocation,
) -> FileVariant:
    """Link an archived, verified job to ``book`` as a new FileVariant.

    Atomic, so the FileVariant is rolled back if this worker lost the job meanwhile.
    """
    with transaction.atomic():
        return _link_variant_and_complete(
            job, book, destination=destination, storage_location=storage_location
        )


def _link_variant_and_complete(
    job: ImportJob,
    book: Book,
    *,
    destination: str,
    storage_location: StorageLocation,
) -> FileVariant:
    job.storage_identifier = destination
    job.storage_location = storage_location
    # Move it out of _unsorted/ into the book's archive folder (no-op under "job").
    archive_layout.place_pending_original(job, book, _storage_for_location(job))
    destination = job.storage_identifier
    job.normalized_filename = normalize_filename(
        f"{_sanitize_title_for_filename(book.title)}{Path(job.normalized_filename).suffix}",
    )
    variant = FileVariant.objects.create(
        book=book,
        format=job.detected_format,
        mime_type=job.mime_type,
        original_filename=job.original_filename,
        storage_location=storage_location,
        storage_identifier=destination,
        size_bytes=job.size_bytes,
        sha256=job.sha256,
        source_type=variant_source_type_for_job(job),
        uploader=job.uploader,
        imported_at=timezone.now(),
        processing_state=FileVariant.ProcessingState.COMPLETE,
        **referenced_files.reference_fields_for_job(job),
    )
    _try_extract_front_cover(variant, job)
    job.book = book
    job.file_variant = variant
    job.state = ImportJob.State.COMPLETE
    _save_job(
        job,
        update_fields=[
            "state",
            "book_id",
            "file_variant_id",
            "storage_location_id",
            "storage_identifier",
            "normalized_filename",
            "updated_at",
        ],
        release=True,
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

        book = Book.objects.create(title=_fit(Book, "title", candidate.title))
        for author_name in candidate.authors:
            author, _ = Author.objects.get_or_create(
                full_name=_fit(Author, "full_name", author_name),
            )
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
            book.publisher = _fit(Book, "publisher", candidate.publisher)
        if candidate.language:
            book.language = _fit(Book, "language", candidate.language)
        if candidate.description:
            book.description = candidate.description
        year = clean_publication_year(candidate.publication_year)
        if year is not None:
            book.publication_year = year
        book.edition = _fit(Book, "edition", candidate.edition) or book.edition
        book.save()
        return book

    # Fallback provisional metadata
    fallback_title = fallback_title or extra_metadata.get("title") or "Unknown title"
    return Book.objects.create(title=_fit(Book, "title", fallback_title))


def _archive_source(storage: LocalFilesystemStorage, job: ImportJob) -> tuple[str, bool]:
    """Archive the source (COPY/MOVE) or reference it in place (INDEX_IN_PLACE).

    Sets ``job.storage_location``/``storage_identifier``; callers must use those rather
    than assuming the default archive location.
    """
    if referenced_files.is_index_in_place(job.source_location):
        verified = referenced_files.index_source_file(job)
        return job.storage_identifier, verified
    storage_location = _default_storage_location()
    # The Book is not known yet: under a book-scoped layout the file goes to _unsorted/
    # and is moved into the book folder once identification or review settles the Book.
    destination = archive_layout.pending_destination(storage, job)
    # Checked per file, so a bulk import already queued still stops before the disk fills.
    storage.ensure_free_space(job.size_bytes or Path(job.source_path).stat().st_size)
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


def _failed_job_retry_due(job: ImportJob) -> bool:
    """A FAILED job may be re-run by a queue retry while retry attempts remain.

    The failure handler records ``next_retry_at`` and increments ``attempt_count`` before
    re-raising, so RQ's scheduled retry arrives here with the job still FAILED. Jobs that
    failed without being scheduled for retry (``next_retry_at`` unset, e.g. source file
    missing) and jobs past the retry budget stay failed until an operator resets them
    (``ingest_sources --retry-failed``).
    """
    max_retries = int(getattr(django_settings, "BOOK_SCANNER_QUEUE_RETRY_MAX", 0))
    return job.next_retry_at is not None and job.attempt_count <= max_retries


def _archive_awaiting_review(job: ImportJob) -> None:
    """Archive a job that waits for a reviewer, then leave it LOW_CONFIDENCE.

    The source is only removed (MOVE sources) after the archive copy verified. The
    final save releases this worker's lease.
    """
    job.state = ImportJob.State.ARCHIVING
    _save_job(job, update_fields=["state", "updated_at"])
    destination, verified = _archive_source(_default_storage(), job)
    job.storage_identifier = destination
    _save_job(
        job,
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
    _save_job(
        job,
        update_fields=[
            "state",
            "storage_identifier",
            "storage_location_id",
            "updated_at",
        ],
        release=True,
    )


def _settle_multipart_part(job: ImportJob) -> None:
    """Exit hook of a grouped part's intake run; see ``multipart.settle_part``."""
    from . import multipart

    if job.import_group_id is None:
        return
    try:
        multipart.settle_part(job)
    except Exception as exc:  # the part itself is safely recorded; retried on next settle
        logger.warning(
            "multipart_settle_failed",
            import_group_id=str(job.import_group_id),
            import_job_id=str(job.id),
            error_type=type(exc).__name__,
        )


# --- Exclusive ownership of an import job ---------------------------------------------
#
# More than one queue delivery may exist for one ImportJob: ``ingest_sources`` re-queues
# DISCOVERED jobs it cannot see in RQ (the snapshot can be stale), RQ retries a job, an
# operator re-queues it. Only one delivery may ever process it. A delivery therefore
# claims the job with one conditional UPDATE that sets ``lease_owner`` to a fresh token,
# and every later write of the job (each phase transition, the archive location, the
# final state) is a conditional UPDATE ``WHERE lease_owner = <token>`` that also renews
# the lease. A delivery that cannot claim the job returns without touching it, whatever
# state it loaded (DISCOVERED, HASHING, ... ARCHIVING). A worker whose lease was taken
# away (``recover_stale_jobs``) stops at its next write instead of overwriting the row.

# A worker owns the job (holds its lease) while it is in one of these states.
ACTIVE_IMPORT_STATES = (
    ImportJob.State.HASHING,
    ImportJob.State.METADATA_EXTRACTION,
    ImportJob.State.SIGNAL_NORMALIZATION,
    ImportJob.State.EXTERNAL_IDENTIFICATION,
    ImportJob.State.MATCHED,
    ImportJob.State.ARCHIVING,
)


class ImportLeaseLostError(Exception):
    """This worker no longer owns the import job; it must stop without writing."""


class ImportCancelledError(ImportLeaseLostError):
    """An operator cancelled the running import (``cancel_source_imports``)."""


def import_lease_duration() -> timedelta:
    """How long a claim stays valid without a phase transition renewing it.

    Defaults to the RQ job timeout plus five minutes: RQ kills a work horse at its job
    timeout, so once a lease has expired its worker is gone and the job is stale.
    """
    seconds = int(getattr(django_settings, "BOOK_SCANNER_INGEST_LEASE_SECONDS", 0) or 0)
    if seconds <= 0:
        timeout = int(getattr(django_settings, "BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS", 1800))
        seconds = max(timeout, 60) + 300
    return timedelta(seconds=seconds)


def claim_import_job(job: ImportJob) -> bool:
    """Atomically take exclusive ownership of ``job`` and move it to HASHING.

    Only an unowned job that is waiting to run can be claimed: DISCOVERED, or FAILED
    with a queue retry due. One conditional UPDATE decides, so of any number of
    concurrent deliveries exactly one wins. Jobs in an active phase are never claimed
    by a delivery, even when their lease has expired; ``recover_stale_jobs`` handles
    those explicitly. On success ``job`` carries the lease token used by later writes.
    """
    now = timezone.now()
    token = uuid.uuid4().hex
    expires = now + import_lease_duration()
    max_retries = int(getattr(django_settings, "BOOK_SCANNER_QUEUE_RETRY_MAX", 0))
    waiting = django_models.Q(state=ImportJob.State.DISCOVERED) | django_models.Q(
        state=ImportJob.State.FAILED,
        next_retry_at__isnull=False,
        attempt_count__lte=max_retries,
    )
    unowned = django_models.Q(lease_owner="") | django_models.Q(lease_expires_at__lt=now)
    claimed = (
        ImportJob.objects.filter(pk=job.pk)
        .filter(waiting & unowned)
        .update(
            state=ImportJob.State.HASHING,
            lease_owner=token,
            lease_expires_at=expires,
            updated_at=now,
        )
    )
    if not claimed:
        return False
    job.state = ImportJob.State.HASHING
    job.lease_owner = token
    job.lease_expires_at = expires
    job.updated_at = now
    return True


def _save_job(job: ImportJob, update_fields: list[str], *, release: bool = False) -> None:
    """Persist ``update_fields`` of ``job``, only while this worker still owns it.

    A job without a lease (review decisions, sibling bookkeeping) is saved normally. For
    an owned job the row is fetched and locked by primary key (``SELECT ... FOR
    UPDATE``, unconditionally, so the lock is held whatever the row contains), its
    state and owner are inspected in Python, and the save happens inside that same
    transaction. ``cancel_source_imports`` locks the same row, so a cancellation cannot
    commit between the check and the save. The save renews the lease, or with
    ``release`` clears it (the job leaves the active phases). Raises
    ``ImportCancelledError`` when the job was cancelled and ``ImportLeaseLostError``
    when this worker's token is no longer the owner; nothing is written then.
    """
    token = job.lease_owner
    if not token:
        job.save(update_fields=update_fields)
        return
    with transaction.atomic():
        locked = ImportJob.objects.select_for_update().filter(pk=job.pk).first()
        if locked is not None and locked.state == ImportJob.State.CANCELLED:
            raise ImportCancelledError(f"import job {job.pk} was cancelled")
        if locked is None or locked.lease_owner != token:
            raise ImportLeaseLostError(str(job.pk))
        if release:
            job.lease_owner = ""
            job.lease_expires_at = None
        else:
            job.lease_expires_at = timezone.now() + import_lease_duration()
        job.save(update_fields=[*update_fields, "lease_owner", "lease_expires_at"])


# Cancellable: queued or running, but not yet ARCHIVING (its source may already be in
# the archive). LOW_CONFIDENCE jobs are archived and waiting for review.
CANCELLABLE_IMPORT_STATES = tuple(
    state for state in IN_FLIGHT_STATES if state != ImportJob.State.ARCHIVING
)


def cancel_source_imports(source_location: SourceLocation) -> list[str]:
    """Pause a source and cancel its import jobs that have not started archiving.

    Returns the ids of the cancelled jobs. Jobs already ARCHIVING are allowed to
    finish, because their source file may already have been moved into the archive.
    LOW_CONFIDENCE jobs are archived and waiting for review, so they are untouched.
    Cancelled files are rediscovered by ``ingest_sources`` once the source is resumed.

    The jobs' rows are locked (``SELECT ... FOR UPDATE``) and their lease cleared in
    the same transaction. A worker in the middle of an owned save holds the row lock,
    so the cancellation waits for that save to commit and then re-reads the state: a
    job that just reached ARCHIVING is no longer cancelled. A worker whose job was
    cancelled stops at its next save (``ImportCancelledError``) without writing.
    """
    with transaction.atomic():
        SourceLocation.objects.filter(pk=source_location.pk).update(is_paused=True)
        source_location.is_paused = True
        ids = [
            str(pk)
            for pk in ImportJob.objects.select_for_update()
            .filter(source_location=source_location, state__in=CANCELLABLE_IMPORT_STATES)
            .values_list("pk", flat=True)
        ]
        ImportJob.objects.filter(pk__in=ids, state__in=CANCELLABLE_IMPORT_STATES).update(
            state=ImportJob.State.CANCELLED,
            last_error="cancelled by operator",
            next_retry_at=None,
            lease_owner="",
            lease_expires_at=None,
            updated_at=timezone.now(),
        )
    logger.info(
        "intake_source_cancelled",
        source_location_id=source_location.pk,
        cancelled=len(ids),
    )
    return ids


def stale_import_jobs(now: datetime | None = None) -> list[ImportJob]:
    """Jobs in an active phase whose worker is gone: lease expired, or never leased.

    Rows without a lease date predate leases (or were written by an older worker);
    they count as stale once untouched for a lease duration.
    """
    now = now or timezone.now()
    lease = import_lease_duration()
    return list(
        ImportJob.objects.filter(state__in=ACTIVE_IMPORT_STATES)
        .filter(
            django_models.Q(lease_expires_at__lt=now)
            | django_models.Q(lease_expires_at__isnull=True, updated_at__lt=now - lease),
        )
        .order_by("updated_at"),
    )


def recover_stale_import_job(job: ImportJob, reason: str) -> bool:
    """Mark a stale job FAILED and drop its lease; False if it changed meanwhile.

    Conditional on the owner, state and expiry read by ``stale_import_jobs``, so a
    worker that renewed its lease in the meantime is not touched. The worker that held
    the lease, if it is somehow still running, stops at its next write. The job is not
    retried automatically (its source may be half moved): ``ingest_sources
    --retry-failed`` re-queues it once the operator has checked the source.
    """
    recovered = ImportJob.objects.filter(
        pk=job.pk,
        state=job.state,
        lease_owner=job.lease_owner,
        lease_expires_at=job.lease_expires_at,
    ).update(
        state=ImportJob.State.FAILED,
        last_error=f"recovered by recover_stale_jobs: {reason}",
        next_retry_at=None,
        lease_owner="",
        lease_expires_at=None,
        updated_at=timezone.now(),
    )
    if recovered:
        # A recovered part of a multi-part work is now missing; let its group settle.
        job.refresh_from_db()
        _settle_multipart_part(job)
    return bool(recovered)


def process_import_job(job_id: str) -> None:
    """Process one intake job from staging file to durable archive and optional matching.

    Safe to deliver more than once: only the delivery that claims the job's lease does
    any work, and it stops as soon as it loses the lease.
    """
    job = ImportJob.objects.select_related("uploader", "source_location").get(pk=job_id)
    if job.state in (ImportJob.State.COMPLETE, ImportJob.State.CANCELLED):
        # CANCELLED: RQ may redeliver an abandoned job after a worker restart.
        return
    if job.state == ImportJob.State.FAILED and not _failed_job_retry_due(job):
        return
    if (
        job.state == ImportJob.State.DISCOVERED
        and job.source_location is not None
        and job.source_location.is_paused
    ):
        # Leave the job DISCOVERED; ingest_sources re-queues it after resume.
        logger.info("intake_skipped_paused_source", import_job_id=str(job.id))
        return
    loaded_state = job.state
    if not claim_import_job(job):
        # Another delivery owns the job (or it is no longer waiting to run).
        logger.info(
            "intake_duplicate_delivery_skipped",
            import_job_id=str(job.id),
            loaded_state=loaded_state,
        )
        return
    try:
        _process_claimed_import_job(job)
    except ImportCancelledError:
        # Keep CANCELLED; failing would schedule an RQ retry of a cancelled job.
        logger.info("intake_cancelled", import_job_id=str(job.id))
    except ImportLeaseLostError:
        logger.warning("intake_lease_lost", import_job_id=str(job.id))
    finally:
        # Every exit of a claimed run (archived, duplicate, failed, source missing,
        # exception, lost lease) settles a grouped part, so its group never waits forever.
        _settle_multipart_part(job)


def _process_claimed_import_job(job: ImportJob) -> None:
    """Run intake for a job this worker has claimed (``job.lease_owner`` is set)."""
    from . import multipart

    source_path = Path(job.source_path)
    if not source_path.exists():
        job.last_error = "source file missing"
        job.state = ImportJob.State.FAILED
        _save_job(job, update_fields=["state", "last_error", "updated_at"], release=True)
        return

    try:
        # The claim was a bulk UPDATE; save once so HASHING is observable (post_save).
        _save_job(job, update_fields=["state", "updated_at"])
        job.sha256 = compute_sha256(job.source_path)
        job.size_bytes = source_path.stat().st_size
        detected_format, mime_type = detect_format(job.original_filename, job.mime_type or None)
        validate_container_signature(job.source_path, detected_format)
        job.detected_format = detected_format
        job.mime_type = mime_type
        if not job.normalized_filename:
            job.normalized_filename = normalize_filename(job.original_filename)
        job.state = ImportJob.State.METADATA_EXTRACTION
        _save_job(
            job,
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
        if duplicate_variant is not None and multipart.pending_group_for(job) is not None:
            # A part of a pending multi-part work belongs to that work: it must not be
            # resolved onto the other Book and silently drop out of the group (#60).
            logger.info(
                "multipart_part_identical_to_library_file",
                import_job_id=str(job.id),
                import_group_id=str(job.import_group_id),
                file_variant_id=duplicate_variant.id,
                book_id=duplicate_variant.book_id,
            )
            duplicate_variant = None
        if duplicate_variant is not None:
            job.book = duplicate_variant.book
            job.file_variant = duplicate_variant
            job.storage_identifier = duplicate_variant.storage_identifier
            job.storage_location = duplicate_variant.storage_location
            job.state = ImportJob.State.COMPLETE
            _save_job(
                job,
                update_fields=[
                    "state",
                    "book_id",
                    "file_variant_id",
                    "storage_identifier",
                    "storage_location_id",
                    "updated_at",
                ],
                release=True,
            )
            # A duplicate that is only referenced in place lives on storage the
            # application does not control, so it never authorizes deleting a MOVE source.
            _finalize_source_cleanup(
                job,
                not duplicate_variant.storage_location.read_only
                and _verify_archive(
                    backend_for_location(duplicate_variant.storage_location),
                    duplicate_variant.storage_identifier,
                    job.sha256,
                ),
            )
            _try_extract_front_cover(duplicate_variant, job)
            _propagate_book_to_siblings(job, duplicate_variant.book)
            return

        metadata = extract_metadata(
            job.source_path,
            detected_format,
            original_filename=job.original_filename,
        )
        metadata = apply_sidecar_metadata(metadata, read_sidecar_metadata(job))
        if job.source_location is not None:
            path_hints = source_layout.hints_for_source_file(job.source_location, job.source_path)
            if path_hints is not None:
                metadata[identification.PATH_LAYOUT_EVIDENCE_KEY] = path_hints.as_evidence()
        job.evidence = metadata
        job.state = ImportJob.State.SIGNAL_NORMALIZATION
        _save_job(
            job,
            update_fields=[
                "evidence",
                "state",
                "updated_at",
            ],
        )
        if multipart.pending_group_for(job) is not None:
            # One part of a suspected multi-part work: archive it like any import
            # awaiting review, but identify the work once for the whole group. A part
            # identical to a file of another Book is archived too; the group review
            # shows it as a duplicate part (see multipart.MEMBER_DUPLICATE).
            _archive_awaiting_review(job)
            return

        signals = identification.build_identification_input(
            source_path=job.source_path,
            original_filename=job.original_filename,
            extracted_metadata=metadata,
        )
        job.state = ImportJob.State.EXTERNAL_IDENTIFICATION
        _save_job(job, update_fields=["state", "updated_at"])
        candidates, provider_outcomes = identification.resolve_candidates_with_report(signals)
        file_authors = identification.evidence_author_names(metadata)
        decision = decide_auto_accept(candidates, file_authors, file_evidence=signals)
        if decision.blocked_reason is not None:
            metadata[AUTO_ACCEPT_BLOCKED_KEY] = decision.blocked_reason
            metadata[AUTO_ACCEPT_BLOCK_DETAILS_KEY] = decision.details
            metadata[SUGGESTED_AUTHORS_KEY] = file_authors
            job.evidence = metadata
            _save_job(job, update_fields=["evidence", "updated_at"])
            logger.info(
                "intake_auto_accept_blocked",
                import_job_id=str(job.id),
                reason=decision.blocked_reason,
                provider_id=decision.details.get("provider_id"),
                score=decision.details.get("score"),
            )
        elif decision.tie_break is not None:
            metadata[AUTO_ACCEPT_TIE_BREAK_KEY] = decision.tie_break
            job.evidence = metadata
            _save_job(job, update_fields=["evidence", "updated_at"])
            logger.info(
                "intake_auto_accept_tie_break",
                import_job_id=str(job.id),
                provider_id=decision.tie_break["chosen"]["provider_id"],
                passed_over_count=len(decision.tie_break["passed_over"]),
            )
        _save_metadata_candidates(
            job,
            candidates,
            signals=signals,
            outcomes=provider_outcomes,
            passed_over=decision.passed_over,
        )

        sidecar_conflicts = sidecar_conflicts_in(metadata)
        if sidecar_conflicts:
            # Curated sidecar metadata contradicts the file itself: a human decides.
            logger.info(
                "sidecar_metadata_conflict",
                import_job_id=str(job.id),
                fields=sidecar_conflicts,
            )
        # The provider candidate allowed to auto-accept (None below the threshold or when
        # the author checks block it), and the raw top candidate for comparisons.
        top_candidate = decision.candidate
        raw_top_candidate = candidates[0] if candidates else None
        # Another format of this book may already be linked (#39): its Book becomes a
        # strong candidate, but only joins when the evidence agrees.
        assessment: SiblingAssessment | None = None
        sibling = _resolved_sibling(job)
        if sibling is not None and sibling.book is not None:
            assessment = _assess_sibling_book(
                signals=signals,
                sibling=sibling,
                book=sibling.book,
                top_candidate=raw_top_candidate,
                file_authors=file_authors,
            )
            _record_sibling_evidence(job, assessment)

        if assessment is not None:
            # Accepted: join the sibling's Book. Rejected: the evidence conflicts with
            # it, so a human decides even if a provider candidate scored high.
            needs_review = not assessment.accepted
        else:
            needs_review = top_candidate is None
        if sidecar_conflicts:
            needs_review = True
        if needs_review and assessment is None and not sidecar_conflicts:
            # No provider record may be accepted. The file's own independent evidence
            # (path, text header, embedded metadata) may still agree on what it is.
            from . import local_identity

            local = local_identity.local_consensus(metadata, signals, candidates)
            if local is not None:
                top_candidate = local.as_candidate()
                metadata[local_identity.LOCAL_IDENTITY_KEY] = local.evidence
                job.evidence = metadata
                _save_job(job, update_fields=["evidence", "updated_at"])
                logger.info(
                    "intake_local_consensus_accepted",
                    import_job_id=str(job.id),
                    title_origins=list(local.title_origins),
                    author_source=local.author_source,
                )
                needs_review = False
        if needs_review:
            _archive_for_review(job, candidates)
            if assessment is None:
                # Weak or no signal: the local LLM may propose better search terms (#26).
                # Off by default; never blocks or fails the import.
                from . import llm_identification

                llm_identification.maybe_request_after_intake(job, candidates)
            return

        job.state = ImportJob.State.MATCHED
        _save_job(job, update_fields=["state", "updated_at"])
        storage = _default_storage()
        job.state = ImportJob.State.ARCHIVING
        _save_job(job, update_fields=["state", "updated_at"])
        destination, verified = _archive_source(storage, job)
        storage_location = job.storage_location or _default_storage_location()
        # Record where the archive copy went and confirm this worker still owns the
        # job before a MOVE source is deleted.
        _save_job(job, update_fields=["storage_identifier", "storage_location_id", "updated_at"])
        _finalize_source_cleanup(job, verified)
        if not verified:
            raise RuntimeError("archive verification failed")

        with _sibling_group_lock(job), transaction.atomic():
            book: Book | None = assessment.book if assessment is not None else None
            if book is None and _groups_sibling_formats(job):
                # A sibling may have been linked while this file was being archived.
                late = _resolved_sibling(job)
                if late is not None and late.book is not None:
                    late_assessment = _assess_sibling_book(
                        signals=signals,
                        sibling=late,
                        book=late.book,
                        top_candidate=raw_top_candidate,
                        file_authors=file_authors,
                    )
                    _record_sibling_evidence(job, late_assessment)
                    if not late_assessment.accepted:
                        _create_review_task(job=job, candidates=candidates)
                        job.storage_identifier = destination
                        job.storage_location = storage_location
                        job.state = ImportJob.State.LOW_CONFIDENCE
                        _save_job(
                            job,
                            update_fields=[
                                "state",
                                "storage_identifier",
                                "storage_location_id",
                                "updated_at",
                            ],
                            release=True,
                        )
                        return
                    assessment = late_assessment
                    book = late.book
            if book is None:
                assert top_candidate is not None
                book = _provision_book_from_metadata(
                    candidate=top_candidate,
                    fallback_title=top_candidate.title,
                    extra_metadata=metadata,
                )
                genres.assign_automatic(
                    book,
                    provider_subjects=top_candidate.subjects,
                    embedded_subjects=genres.embedded_subjects(metadata),
                )
            _complete_with_book(
                job, book, destination=destination, storage_location=storage_location
            )
            if assessment is not None:
                logger.info(
                    "intake_linked_format_sibling",
                    import_job_id=str(job.id),
                    sibling_import_job_id=str(assessment.sibling.id),
                    book_id=book.pk,
                    score=assessment.score,
                    from_review=False,
                )
            _propagate_book_to_siblings(job, book)
    except ImportLeaseLostError:
        raise
    except Exception as exc:  # pragma: no cover - failure path depends on input surface
        job.last_error = str(exc)
        job.state = ImportJob.State.FAILED
        job.attempt_count += 1
        job.next_retry_at = datetime.now(tz=UTC)
        # Raises ImportLeaseLostError (no retry) if another owner took over meanwhile.
        _save_job(
            job,
            update_fields=[
                "state",
                "last_error",
                "attempt_count",
                "next_retry_at",
                "updated_at",
            ],
            release=True,
        )
        logger.error(
            "intake_failed",
            import_job_id=str(job.id),
            source_type=job.source_type,
            error_recorded=True,
        )
        raise
