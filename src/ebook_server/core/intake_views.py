"""HTTP views for digital uploads and member import status."""

from __future__ import annotations

from pathlib import Path

import structlog
from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.db import transaction
from django.http import (
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods

from ebook_server.jobs import enqueue

from . import (
    intake,
    intake_progress,
    request_limits,
)
from .models import (
    ImportJob,
)
from .view_helpers import (
    _active_library_profile,
    _cleanup_staged_upload,
    _coerce_uuid,
    _limited_response,
    _wants_html,
)

logger = structlog.get_logger(__name__)


@require_http_methods(["GET", "POST"])
@login_required
def upload_file(request: HttpRequest) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    if request.method == "GET":
        return render(
            request,
            "core/upload.html",
            {
                "max_upload_bytes": django_settings.BOOK_SCANNER_MAX_UPLOAD_BYTES,
                "accepted_extensions": ",".join(sorted(intake.SUPPORTED_FORMATS)),
                "supported_formats": ", ".join(
                    extension.removeprefix(".").upper()
                    for extension in sorted(intake.SUPPORTED_FORMATS)
                ),
                "audiobook_service_url": intake.audiobook_service_url(),
            },
        )

    uploaded_file = request.FILES.get("file")
    if uploaded_file is None:
        return HttpResponseBadRequest("missing_file")
    original_name = uploaded_file.name
    if not original_name:
        return HttpResponseBadRequest("filename is required")

    try:
        normalized_filename = intake.normalize_filename(original_name)
    except intake.ValidationError as exc:
        return HttpResponseBadRequest(str(exc))
    if intake.is_audio_filename(original_name, uploaded_file.content_type):
        # Audiobooks live on a separate service (docs/design/32-audiobooks.md); the
        # file is rejected before it is staged, so no ImportJob is ever created.
        logger.info(
            "upload_audio_rejected",
            drm_protected=intake.is_drm_audio_filename(original_name),
        )
        return HttpResponse(
            intake.audio_upload_rejection_message(original_name),
            status=415,
            content_type="text/plain; charset=utf-8",
        )
    try:
        detected_format, detected_mime = intake.detect_format(
            original_name,
            uploaded_file.content_type,
        )
    except intake.ValidationError as exc:
        return HttpResponseBadRequest(str(exc))

    staging_root = Path(django_settings.APP_DATA_DIR) / "uploads"
    try:
        reservation = request_limits.reserve_upload(profile, uploaded_file.size or 0)
    except request_limits.RequestLimited as exc:
        return _limited_response("upload", exc)
    source_path = ""
    committed = False
    try:
        source_path, size_bytes = intake.write_upload_to_staging(uploaded_file, staging_root)
        with transaction.atomic():
            request_limits.finish_reservation(reservation, size_bytes)
            job = ImportJob.objects.create(
                source_type=ImportJob.SourceType.UPLOAD,
                state=ImportJob.State.DISCOVERED,
                uploader=profile.user,
                source_path=source_path,
                original_filename=original_name,
                normalized_filename=normalized_filename,
                detected_format=detected_format,
                mime_type=detected_mime,
                size_bytes=size_bytes,
            )
        committed = True
    except request_limits.RequestLimited as exc:
        return _limited_response("upload", exc)
    except ValueError as exc:
        return HttpResponseBadRequest(str(exc))
    except OSError:
        return HttpResponse("storage unavailable", status=503)
    finally:
        if not committed:
            reservation.delete()
            if source_path:
                _cleanup_staged_upload(source_path)
    try:
        enqueue(intake.process_import_job, str(job.id))
    except Exception as exc:  # pragma: no cover - transport/path specific
        return HttpResponseBadRequest(str(exc))
    if _wants_html(request):
        return redirect("imports-status", job_id=job.id)
    return JsonResponse({"job_id": str(job.id), "state": job.state}, status=201)


@require_GET
@login_required
def intake_progress_view(request: HttpRequest) -> HttpResponseBase:
    """Admin page: how much is still left to import, per source, uploads and scanner.

    Database aggregates only (``core.intake_progress``); nothing is scanned or hashed.
    """
    profile = _active_library_profile(request)
    if profile is None or not profile.is_admin():
        return HttpResponseForbidden()
    progress = intake_progress.intake_progress()
    if _wants_html(request):
        return render(request, "core/intake_progress.html", {"progress": progress})
    return JsonResponse(progress)


@require_GET
@login_required
def import_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    if _coerce_uuid(job_id) is None:
        return HttpResponseBadRequest("invalid job id")
    try:
        job = ImportJob.objects.get(pk=job_id)
    except ImportJob.DoesNotExist:
        return HttpResponseNotFound()
    payload = {
        "job_id": str(job.id),
        "state": job.state,
        "filename": job.original_filename,
        "normalized_filename": job.normalized_filename,
        "sha256": job.sha256,
        "error": job.last_error,
        "book_id": str(job.book_id) if job.book_id else None,
        "file_variant_id": str(job.file_variant_id) if job.file_variant_id else None,
        "storage_identifier": job.storage_identifier,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }
    if _wants_html(request):
        return render(request, "core/import_status.html", {"job": job, "payload": payload})
    return JsonResponse(payload)
