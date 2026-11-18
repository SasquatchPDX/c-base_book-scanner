"""Authenticated, read-only preview endpoints for the review-page reader (issue #6)."""

from __future__ import annotations

import html
import re
from collections.abc import Iterator
from typing import BinaryIO

import structlog
from django.contrib.auth.decorators import login_required
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
    StreamingHttpResponse,
)
from django.http.response import HttpResponseBase
from django.utils.http import content_disposition_header
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET

from . import review_reader
from .models import ReviewTask
from .review_reader import PreviewUnavailable, ReaderSource
from .review_views import _can_manage_review_task
from .storage_registry import StorageConfigurationError, backend_for_location
from .view_helpers import _profile_for_request

logger = structlog.get_logger(__name__)

_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")
_STREAM_CHUNK = 64 * 1024


def _task_source(
    request: HttpRequest, task_id: int, source_key: str
) -> ReaderSource | HttpResponse:
    profile = _profile_for_request(request)
    if profile is None or profile.is_disabled:
        return HttpResponseForbidden()
    try:
        task = ReviewTask.objects.select_related(
            "import_job__file_variant__storage_location",
            "import_job__storage_location",
            "scan_job",
        ).get(pk=task_id)
    except ReviewTask.DoesNotExist:
        return HttpResponseNotFound()
    if not _can_manage_review_task(profile, task):
        return HttpResponseForbidden()
    source = review_reader.find_source(task, source_key)
    if source is None:
        return HttpResponseNotFound("file not attached to this review")
    return source


def _open(source: ReaderSource) -> BinaryIO | HttpResponse:
    try:
        storage = backend_for_location(source.storage_location)
        return storage.open(source.storage_identifier)
    except StorageConfigurationError:
        return HttpResponse("file storage unavailable", status=503)
    except (FileNotFoundError, ValueError):
        return HttpResponseNotFound("file missing")
    except OSError:
        return HttpResponse("storage unavailable", status=503)


def _harden(response: HttpResponseBase) -> HttpResponseBase:
    response["X-Content-Type-Options"] = "nosniff"
    response["Cache-Control"] = "private, no-store"
    return response


def _unavailable(exc: PreviewUnavailable, source: ReaderSource) -> HttpResponse:
    logger.info("review_reader_preview_unavailable", source=source.key, format=source.file_format)
    return JsonResponse({"error": str(exc)}, status=422)


def _parse_range(header: str, size: int) -> tuple[int, int] | None:
    match = _RANGE.match(header.strip())
    if match is None or size <= 0:
        return None
    first, last = match.groups()
    if first == "" and last == "":
        return None
    if first == "":
        start, end = max(size - int(last), 0), size - 1
    else:
        start = int(first)
        end = min(int(last), size - 1) if last else size - 1
    if start > end or start >= size:
        return None
    return start, end


def _ranged_iterator(stream: BinaryIO, start: int, length: int) -> Iterator[bytes]:
    try:
        stream.seek(start)
        remaining = length
        while remaining > 0:
            chunk = stream.read(min(_STREAM_CHUNK, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk
    finally:
        stream.close()


def _int_param(request: HttpRequest, name: str) -> int:
    try:
        return max(int(request.GET.get(name, "1")), 1)
    except ValueError:
        return 1


@require_GET
@login_required
def review_reader_file(request: HttpRequest, task_id: int, source_key: str) -> HttpResponseBase:
    """Stream the raw file: inline only for PDF/PNG, otherwise as an attachment."""
    source = _task_source(request, task_id, source_key)
    if isinstance(source, HttpResponse):
        return source
    stream = _open(source)
    if isinstance(stream, HttpResponse):
        return stream

    inline_type = review_reader.INLINE_CONTENT_TYPES.get(source.file_format.lower())
    as_attachment = inline_type is None or request.GET.get("download") == "1"
    content_type = inline_type or "application/octet-stream"
    try:
        size = stream.seek(0, 2)
        stream.seek(0)
    except OSError:
        size = source.size_bytes

    byte_range = None
    if not as_attachment and "HTTP_RANGE" in request.META:
        byte_range = _parse_range(request.META["HTTP_RANGE"], size)
        if byte_range is None:
            stream.close()
            response = HttpResponse(status=416)
            response["Content-Range"] = f"bytes */{size}"
            return _harden(response)

    if byte_range is not None:
        start, end = byte_range
        ranged = StreamingHttpResponse(
            _ranged_iterator(stream, start, end - start + 1),
            status=206,
            content_type=content_type,
        )
        ranged["Content-Range"] = f"bytes {start}-{end}/{size}"
        ranged["Content-Length"] = str(end - start + 1)
        file_response: HttpResponseBase = ranged
    else:
        file_response = FileResponse(stream, content_type=content_type)
        file_response["Content-Length"] = str(size)
    file_response["Accept-Ranges"] = "bytes" if not as_attachment else "none"
    file_response["Content-Disposition"] = content_disposition_header(
        as_attachment,
        source.original_filename or f"{source.key}.{source.file_format}",
    ) or ("attachment" if as_attachment else "inline")
    # A navigated-to copy must never run active content on this origin.
    file_response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return _harden(file_response)


@require_GET
@login_required
def review_reader_text(request: HttpRequest, task_id: int, source_key: str) -> HttpResponseBase:
    """One page of a server-decoded plain-text file as JSON."""
    source = _task_source(request, task_id, source_key)
    if isinstance(source, HttpResponse):
        return source
    if source.kind != review_reader.PREVIEW_TEXT:
        return HttpResponseNotFound("no text preview for this format")
    stream = _open(source)
    if isinstance(stream, HttpResponse):
        return stream
    with stream:
        page = review_reader.read_text_page(stream, _int_param(request, "page"))
    return _harden(
        JsonResponse(
            {
                "text": page.text,
                "page": page.page,
                "page_count": page.page_count,
                "truncated": page.truncated,
                "encoding": page.encoding,
            },
        ),
    )


@require_GET
@login_required
def review_reader_parts(request: HttpRequest, task_id: int, source_key: str) -> HttpResponseBase:
    """The navigable parts of a document (EPUB chapters) or comic (CBZ pages)."""
    source = _task_source(request, task_id, source_key)
    if isinstance(source, HttpResponse):
        return source
    if source.kind not in {review_reader.PREVIEW_DOCUMENT, review_reader.PREVIEW_IMAGES}:
        return HttpResponseNotFound("no part list for this format")
    file_format = source.file_format.lower()
    if file_format in {"html", "fb2"}:
        return _harden(JsonResponse({"parts": [{"index": 1, "label": source.original_filename}]}))
    stream = _open(source)
    if isinstance(stream, HttpResponse):
        return stream
    try:
        with stream:
            if file_format == "epub":
                parts = review_reader.epub_parts(stream)
            else:
                parts = review_reader.cbz_parts(stream)
    except PreviewUnavailable as exc:
        return _unavailable(exc, source)
    return _harden(
        JsonResponse({"parts": [{"index": part.index, "label": part.label} for part in parts]}),
    )


@require_GET
@login_required
@xframe_options_sameorigin
def review_reader_document(
    request: HttpRequest,
    task_id: int,
    source_key: str,
) -> HttpResponseBase:
    """A sanitized, script-free HTML rendering for the sandboxed preview frame."""
    source = _task_source(request, task_id, source_key)
    if isinstance(source, HttpResponse):
        return source
    if source.kind != review_reader.PREVIEW_DOCUMENT:
        return HttpResponseNotFound("no document preview for this format")
    stream = _open(source)
    if isinstance(stream, HttpResponse):
        return stream
    part = _int_param(request, "part")
    file_format = source.file_format.lower()
    try:
        with stream:
            if file_format == "epub":
                fragment = review_reader.epub_part_html(stream, part)
            elif file_format == "fb2":
                fragment = review_reader.fb2_document(stream)
            else:
                fragment = review_reader.html_document(stream)
    except PreviewUnavailable as exc:
        fragment = f"<p><em>{html.escape(str(exc))}</em></p>"
        logger.info("review_reader_preview_unavailable", source=source.key, format=file_format)
    response = HttpResponse(
        review_reader.wrap_document(fragment, source.original_filename),
        content_type="text/html; charset=utf-8",
    )
    response["Content-Security-Policy"] = review_reader.DOCUMENT_CSP
    response["Referrer-Policy"] = "no-referrer"
    return _harden(response)


@require_GET
@login_required
def review_reader_page_image(
    request: HttpRequest,
    task_id: int,
    source_key: str,
    page: int,
) -> HttpResponseBase:
    """One validated page image from a comic archive."""
    source = _task_source(request, task_id, source_key)
    if isinstance(source, HttpResponse):
        return source
    if source.kind != review_reader.PREVIEW_IMAGES:
        return HttpResponseNotFound("no page images for this format")
    stream = _open(source)
    if isinstance(stream, HttpResponse):
        return stream
    try:
        with stream:
            payload, content_type = review_reader.cbz_page(stream, page)
    except PreviewUnavailable as exc:
        return _unavailable(exc, source)
    response = HttpResponse(payload, content_type=content_type)
    response["Content-Security-Policy"] = "sandbox; default-src 'none'"
    return _harden(response)
