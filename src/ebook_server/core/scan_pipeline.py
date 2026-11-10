"""Scan-tailor to library pipeline helpers."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import img2pdf
import structlog
from django.conf import settings as django_settings
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone
from PIL import Image
from rq import get_current_job

from ebook_server.jobs import get_queue, is_stale_running
from ebook_server.storage import LocalFilesystemStorage

from . import conversion, intake, ocr
from .models import (
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanTailorStageState,
    StorageLocation,
)

logger = structlog.get_logger(__name__)


class ScanPipelineError(RuntimeError):
    """Raised when scan-job pipeline assembly fails."""


def _scan_storage() -> LocalFilesystemStorage:
    return LocalFilesystemStorage(str(Path(django_settings.APP_DATA_DIR) / "scan-artifacts"))


def _library_storage() -> LocalFilesystemStorage:
    return intake._default_storage()


def _library_location() -> StorageLocation:
    return intake._default_storage_location()


def _ordered_pages(job: ScanJob) -> list[ScanPage]:
    return list(job.pages.all().order_by("logical_sequence_number", "side"))


def _active_output_state(page: ScanPage) -> ScanTailorStageState | None:
    return (
        ScanTailorStageState.objects.filter(
            scan_job=page.scan_job,
            scan_page=page,
            stage=ScanTailorStageState.Stage.OUTPUT.value,
            status=ScanTailorStageState.StageStatus.SUCCESS,
        )
        .order_by("-revision", "-created_at")
        .first()
    )


def _select_page_source_identifier(
    page: ScanPage,
    *,
    allow_raw_pages: bool = True,
) -> tuple[str, str] | None:
    output_state = _active_output_state(page)
    if (
        output_state is not None
        and output_state.output_storage_identifier
        and output_state.output_storage_identifier != ""
    ):
        return ("scan_stage_output", output_state.output_storage_identifier)

    if allow_raw_pages:
        active_version = page.active_version()
        if active_version is not None:
            return ("scan_page_version", active_version.storage_identifier)
    return None


def _current_cleaned_variant_ids_for_scan_job(job: ScanJob) -> set[int]:
    if job.book_id is None:
        return set()

    return set(
        FileVariant.objects.filter(
            book_id=job.book_id,
            source_type=FileVariant.SourceType.CLEANED,
            processing_state=FileVariant.ProcessingState.COMPLETE,
            converter_options__scan_job_id=str(job.id),
        ).values_list("id", flat=True),
    )


def _invalidate_file_variants_and_descendants(variant_ids: set[int]) -> int:
    """Mark variants and descendants derived through source_variant as failed."""
    if not variant_ids:
        return 0

    stale_ids: set[int] = set(variant_ids)
    frontier: set[int] = set(variant_ids)

    while frontier:
        descendants = set(
            FileVariant.objects.filter(source_variant_id__in=frontier).values_list(
                "id",
                flat=True,
            ),
        )
        frontier = descendants - stale_ids
        stale_ids.update(descendants)

    return FileVariant.objects.filter(id__in=stale_ids).update(
        processing_state=FileVariant.ProcessingState.FAILED,
    )


def invalidate_scan_job_library_artifacts(scan_job: ScanJob | str) -> int:
    """Invalidate scan-derived library artifacts when upstream scan data changes."""
    if isinstance(scan_job, str):
        scan_job_obj = ScanJob.objects.filter(pk=scan_job).first()
        if scan_job_obj is None:
            return 0
    else:
        scan_job_obj = scan_job

    if scan_job_obj.book_id is None:
        return 0

    root_variants = _current_cleaned_variant_ids_for_scan_job(scan_job_obj)
    return _invalidate_file_variants_and_descendants(root_variants)


def pipeline_progress(job: ScanJob) -> dict[str, Any]:
    """Return lightweight completion metadata for scan job users."""
    pages = _ordered_pages(job)
    page_count = len(pages)
    resolved_sources: list[dict[str, Any]] = []

    for page in pages:
        source = _select_page_source_identifier(page)
        if source is None:
            resolved_sources.append(
                {
                    "page_id": str(page.id),
                    "status": "missing",
                    "source": None,
                },
            )
        else:
            source_kind, source_identifier = source
            try:
                exists = _scan_storage().resolve_location(source_identifier).is_file()
            except ValueError:
                exists = False
            resolved_sources.append(
                {
                    "page_id": str(page.id),
                    "status": "ready" if exists else "missing",
                    "source_kind": source_kind,
                    "source": source_identifier,
                    "source_exists": exists,
                },
            )

    ready_count = sum(1 for source in resolved_sources if source["status"] == "ready")
    processed_count = sum(
        1
        for source in resolved_sources
        if source.get("status") == "ready" and source.get("source_kind") == "scan_stage_output"
    )
    raw_count = sum(
        1
        for source in resolved_sources
        if source.get("status") == "ready" and source.get("source_kind") == "scan_page_version"
    )

    result: dict[str, Any] = {
        "page_count": page_count,
        "ready_pages": ready_count,
        "missing_pages": [
            entry["page_id"] for entry in resolved_sources if entry["status"] != "ready"
        ],
        "processed_pages": processed_count,
        "raw_pages": raw_count,
        "stage_counts": {"output_stage_success": processed_count},
    }
    result["finalization_jobs"] = [
        finalization_job_payload(processing_job)
        for processing_job in ProcessingJob.objects.filter(
            job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
            details__scan_job_id=str(job.id),
        ).order_by("-scheduled_at")[:5]
    ]

    if job.book_id is None:
        result["cleaned_variant_id"] = None
        return result

    cleaned = (
        FileVariant.objects.filter(
            book_id=job.book_id,
            source_type=FileVariant.SourceType.CLEANED,
            processing_state=FileVariant.ProcessingState.COMPLETE,
            converter_options__scan_job_id=str(job.id),
        )
        .order_by("-created_at")
        .first()
    )
    result["cleaned_variant_id"] = cleaned.id if cleaned is not None else None

    result["ocr_jobs"] = [
        {
            "job_id": str(ocr_job.job_id),
            "state": ocr_job.state,
            "source_type": ocr_job.details.get("source_type"),
        }
        for ocr_job in _scan_job_ocr_jobs(job)
    ]
    return result


def _scan_job_ocr_jobs(job: ScanJob) -> list[ProcessingJob]:
    query = ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.OCR)
    if job.book_id is None:
        return []

    active_cleaned_variant_ids = _current_cleaned_variant_ids_for_scan_job(job)

    if connection.features.supports_json_field_contains:
        query = query.filter(
            Q(
                details__contains={
                    "source_type": "scan_job",
                    "scan_job_id": str(job.id),
                },
            )
            | Q(file_variant__book_id=job.book_id),
        )
    jobs = list(query)
    return [
        ocr_job
        for ocr_job in jobs
        if (
            (ocr_job.details or {}).get("source_type") == "scan_job"
            and str((ocr_job.details or {}).get("scan_job_id", "")) == str(job.id)
        )
        or (
            ocr_job.file_variant_id is not None
            and ocr_job.file_variant_id in active_cleaned_variant_ids
            and ocr_job.file_variant is not None
            and ocr_job.file_variant.book_id == job.book_id
        )
    ]


def _collect_cleaned_sources(
    job: ScanJob,
    *,
    allow_raw_pages: bool,
) -> list[tuple[ScanPage, str, str]]:
    entries: list[tuple[ScanPage, str, str]] = []
    pages = _ordered_pages(job)
    if not pages:
        raise ScanPipelineError("scan job has no pages")

    missing_page_ids: list[str] = []
    for page in pages:
        source = _select_page_source_identifier(page, allow_raw_pages=allow_raw_pages)
        if source is None:
            missing_page_ids.append(str(page.id))
            continue
        source_kind, source_identifier = source
        entries.append((page, source_kind, source_identifier))

    if missing_page_ids:
        requirement = "processed output" if not allow_raw_pages else "page source"
        raise ScanPipelineError(
            f"missing {requirement} for {len(missing_page_ids)} page(s): "
            + ", ".join(missing_page_ids),
        )
    return entries


def _normalize_page_image(path: Path, scratch: Path, index: int) -> Path:
    """Return a path img2pdf can embed, flattening transparency onto white."""
    try:
        with Image.open(path) as image:
            has_alpha = image.mode in {"RGBA", "LA", "PA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            if not has_alpha:
                return path
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, (255, 255, 255))
            background.paste(rgba, mask=rgba.getchannel("A"))
            flattened = scratch / f"page-{index:05d}.jpg"
            dpi = image.info.get("dpi")
            background.save(
                flattened,
                format="JPEG",
                quality=95,
                **({"dpi": dpi} if isinstance(dpi, tuple) else {}),
            )
            return flattened
    except OSError as exc:
        raise ScanPipelineError(f"cannot read page image {path.name}: {exc}") from exc


def _build_pdf_from_images(image_paths: Sequence[Path]) -> Path:
    with tempfile.NamedTemporaryFile(
        prefix="scan-cleaned-",
        suffix=".pdf",
        delete=False,
    ) as output_file:
        destination = Path(output_file.name)

    try:
        if not image_paths:
            raise ScanPipelineError("no page images available")

        # Stream straight to disk: hundreds of high-resolution pages must never be
        # held in memory at once. Alpha-channel pages (which img2pdf rejects) are
        # flattened to RGB in a scratch directory first.
        with tempfile.TemporaryDirectory(prefix="scan-pdf-norm-") as scratch:
            normalized = [
                str(_normalize_page_image(path, Path(scratch), index))
                for index, path in enumerate(image_paths)
            ]
            with destination.open("wb") as output_stream:
                img2pdf.convert(normalized, outputstream=output_stream)
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def create_cleaned_variant_for_scan_job(
    job: ScanJob,
    *,
    request_name: str | None = None,
    allow_raw_pages: bool = False,
) -> FileVariant:
    if job.book_id is None:
        raise ScanPipelineError("scan job is not linked to a book")

    sources = _collect_cleaned_sources(job, allow_raw_pages=allow_raw_pages)

    storage = _scan_storage()
    resolved_paths: list[Path] = []
    lineage: list[dict[str, Any]] = []

    for page, source_kind, source_identifier in sources:
        source_path = storage.resolve_location(source_identifier)
        if not source_path.exists():
            raise ScanPipelineError(
                f"missing source artifact for page {page.id}: {source_identifier}",
            )
        resolved_paths.append(source_path)
        lineage.append(
            {
                "scan_page_id": str(page.id),
                "logical_sequence_number": page.logical_sequence_number,
                "side": page.side,
                "source_kind": source_kind,
                "storage_identifier": source_identifier,
            },
        )

    pdf_path = _build_pdf_from_images(resolved_paths)
    file_storage = _library_storage()
    safe_name = (request_name or "cleaned").strip().replace(" ", "_")
    output_sha = intake.compute_sha256(str(pdf_path))
    output_size = pdf_path.stat().st_size

    existing = (
        FileVariant.objects.filter(
            book_id=job.book_id,
            format="pdf",
            source_type=FileVariant.SourceType.CLEANED,
            sha256=output_sha,
        )
        .filter(storage_location_id=_library_location().id)
        .first()
    )
    if existing is not None:
        pdf_path.unlink(missing_ok=True)
        return existing

    destination = file_storage.generate_destination(
        f"books/{job.book_id}/cleaned",
        f"{uuid.uuid4()}-{safe_name}.pdf",
    )
    try:
        file_storage.store(str(pdf_path), destination)
    finally:
        pdf_path.unlink(missing_ok=True)

    return FileVariant.objects.create(
        book_id=job.book_id,
        format="pdf",
        mime_type=conversion.FORMAT_TO_MIME["pdf"],
        original_filename=f"{safe_name}.pdf",
        storage_location=_library_location(),
        storage_identifier=destination,
        size_bytes=output_size,
        sha256=output_sha,
        source_type=FileVariant.SourceType.CLEANED,
        uploader=job.operator,
        imported_at=timezone.now(),
        processing_state=FileVariant.ProcessingState.COMPLETE,
        converter_tool="scan-tailor",
        converter_version="pipeline-v1",
        converter_options={
            "scan_job_id": str(job.id),
            "source_kind": "scan_output_or_explicit_raw_override",
            "allow_raw_pages": allow_raw_pages,
            "lineage": lineage,
            "request_name": request_name or "cleaned",
        },
    )


def finalization_job_payload(job: ProcessingJob) -> dict[str, Any]:
    """Serialize durable scan-finalization state for API consumers."""
    details = job.details if isinstance(job.details, dict) else {}
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "scan_job_id": details.get("scan_job_id"),
        "cleaned_variant_id": details.get("cleaned_variant_id"),
        "ocr_job_id": details.get("ocr_job_id"),
        "attempt_count": details.get("attempt_count", 0),
        "error": details.get("error", ""),
        "scheduled_at": job.scheduled_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _active_scan_states() -> set[str]:
    return {
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
    }


def _validate_finalization_request(
    job: ScanJob,
    *,
    create_cleaned_variant: bool,
    allow_raw_pages: bool,
    auto_ocr: bool,
) -> None:
    if job.state not in _active_scan_states():
        raise ScanPipelineError("scan job is not ready for finalization")
    if job.todos.filter(state__in=["open", "claimed"]).exists():
        raise ScanPipelineError("outstanding scan todos must be resolved")
    if auto_ocr and not create_cleaned_variant:
        raise ScanPipelineError("automatic OCR requires cleaned-variant creation")
    if create_cleaned_variant:
        if job.book_id is None:
            raise ScanPipelineError("scan job must be linked to a book before finalization")
        sources = _collect_cleaned_sources(job, allow_raw_pages=allow_raw_pages)
        storage = _scan_storage()
        for page, _source_kind, identifier in sources:
            try:
                exists = storage.resolve_location(identifier).is_file()
            except ValueError:
                exists = False
            if not exists:
                raise ScanPipelineError(f"missing source artifact for page {page.id}: {identifier}")


def request_scan_finalization(
    *,
    scan_job: ScanJob,
    requested_by_user_id: int | None,
    create_cleaned_variant: bool = True,
    allow_raw_pages: bool = False,
    auto_ocr: bool = False,
    target_formats: Any = None,
    request_language: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    request_options: Mapping[str, Any] | None = None,
) -> ProcessingJob:
    """Persist and dispatch final scan packaging outside the web request."""
    with transaction.atomic():
        locked_job = ScanJob.objects.select_for_update().get(pk=scan_job.pk)
        _validate_finalization_request(
            locked_job,
            create_cleaned_variant=create_cleaned_variant,
            allow_raw_pages=allow_raw_pages,
            auto_ocr=auto_ocr,
        )
        prior_state = locked_job.state
        processing_job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
            state=ProcessingJob.JobState.QUEUED,
            details={
                "scan_job_id": str(locked_job.id),
                "create_cleaned_variant": create_cleaned_variant,
                "allow_raw_pages": allow_raw_pages,
                "auto_ocr": auto_ocr,
                "target_formats": target_formats,
                "request_language": request_language,
                "engine": engine,
                "model": model,
                "request_options": dict(request_options or {}),
                "requested_by_user_id": requested_by_user_id,
                "prior_scan_state": prior_state,
                "attempt_count": 0,
                "error": "",
            },
        )
        locked_job.state = ScanJob.State.FINALIZING
        locked_job.finished_at = None
        locked_job.save(update_fields=["state", "finished_at", "updated_at"])

    try:
        _dispatch_scan_finalization_job(str(processing_job.job_id))
    except Exception as exc:
        processing_job.refresh_from_db()
        failure_details = _coerce_details(processing_job)
        if processing_job.state != ProcessingJob.JobState.FAILED:
            failure_details = {**failure_details, "error": f"queue dispatch failed: {exc}"}
            processing_job.state = ProcessingJob.JobState.FAILED
            processing_job.finished_at = timezone.now()
            processing_job.details = failure_details
            processing_job.save(update_fields=["state", "finished_at", "details"])
            locked_job = ScanJob.objects.get(pk=scan_job.pk)
            _restore_scan_after_failure(locked_job, failure_details)
        error = str(failure_details.get("error") or exc)
        raise ScanPipelineError(error) from exc
    processing_job.refresh_from_db()
    return processing_job


def _dispatch_scan_finalization_job(job_id: str) -> None:
    queue = get_queue()
    if queue is None:
        process_scan_finalization_job(job_id)
        return
    queue.enqueue(process_scan_finalization_job, job_id, raise_on_error=True)


def _coerce_details(job: ProcessingJob) -> dict[str, Any]:
    return dict(job.details) if isinstance(job.details, dict) else {}


def _restore_scan_after_failure(scan_job: ScanJob, details: Mapping[str, Any]) -> None:
    prior_state = details.get("prior_scan_state")
    if prior_state not in _active_scan_states():
        prior_state = ScanJob.State.PAUSED
    if scan_job.state == ScanJob.State.FINALIZING:
        scan_job.state = str(prior_state)
        scan_job.finished_at = None
        scan_job.save(update_fields=["state", "finished_at", "updated_at"])


def _existing_ocr_job(cleaned_variant: FileVariant) -> ProcessingJob | None:
    return (
        ProcessingJob.objects.filter(
            file_variant=cleaned_variant,
            job_type=ProcessingJob.JobType.OCR,
            state__in=[
                ProcessingJob.JobState.QUEUED,
                ProcessingJob.JobState.RUNNING,
                ProcessingJob.JobState.SUCCESS,
            ],
        )
        .order_by("-scheduled_at")
        .first()
    )


def process_scan_finalization_job(job_id: str, *, raise_on_error: bool = False) -> None:
    """Build cleaned output and schedule OCR; safe to invoke again after failure."""
    with transaction.atomic():
        try:
            processing_job = ProcessingJob.objects.select_for_update().get(job_id=job_id)
        except ProcessingJob.DoesNotExist:
            return
        if processing_job.job_type != ProcessingJob.JobType.SCAN_FINALIZATION:
            return
        if processing_job.state == ProcessingJob.JobState.SUCCESS:
            return
        if processing_job.state == ProcessingJob.JobState.RUNNING:
            if not is_stale_running(processing_job):
                return
            logger.warning(
                "stale_running_job_reentered",
                processing_job_id=str(processing_job.job_id),
            )
        details = _coerce_details(processing_job)
        attempt_count = int(details.get("attempt_count", 0)) + 1
        details = {**details, "attempt_count": attempt_count, "error": ""}
        processing_job.state = ProcessingJob.JobState.RUNNING
        processing_job.started_at = timezone.now()
        processing_job.finished_at = None
        processing_job.details = details
        processing_job.save(update_fields=["state", "started_at", "finished_at", "details"])

    scan_job_id = details.get("scan_job_id")
    try:
        scan_job = ScanJob.objects.select_related("book", "operator").get(pk=scan_job_id)
    except (ScanJob.DoesNotExist, ValueError) as exc:
        error = "scan job no longer exists"
        processing_job.state = ProcessingJob.JobState.FAILED
        processing_job.finished_at = timezone.now()
        processing_job.details = {**details, "error": error}
        processing_job.save(update_fields=["state", "finished_at", "details"])
        if raise_on_error:
            raise ScanPipelineError(error) from exc
        return

    if scan_job.state == ScanJob.State.CANCELLED:
        error = "scan job was cancelled"
        processing_job.state = ProcessingJob.JobState.FAILED
        processing_job.finished_at = timezone.now()
        processing_job.details = {**details, "error": error}
        processing_job.save(update_fields=["state", "finished_at", "details"])
        if raise_on_error:
            raise ScanPipelineError(error)
        return

    if scan_job.state != ScanJob.State.FINALIZING:
        if scan_job.state not in _active_scan_states():
            processing_job.state = ProcessingJob.JobState.FAILED
            processing_job.finished_at = timezone.now()
            processing_job.details = {
                **details,
                "error": f"scan job cannot finalize from state '{scan_job.state}'",
            }
            processing_job.save(update_fields=["state", "finished_at", "details"])
            return
        scan_job.state = ScanJob.State.FINALIZING
        scan_job.finished_at = None
        scan_job.save(update_fields=["state", "finished_at", "updated_at"])

    logger.info(
        "scan_finalization_started",
        processing_job_id=str(processing_job.job_id),
        scan_job_id=str(scan_job.id),
        attempt_count=attempt_count,
    )

    try:
        cleaned_variant: FileVariant | None = None
        if bool(details.get("create_cleaned_variant", True)):
            cleaned_variant = create_cleaned_variant_for_scan_job(
                scan_job,
                request_name="cleaned",
                allow_raw_pages=bool(details.get("allow_raw_pages", False)),
            )

        ocr_job: ProcessingJob | None = None
        if bool(details.get("auto_ocr", False)):
            if cleaned_variant is None:
                raise ScanPipelineError("automatic OCR requires a cleaned variant")
            ocr_job = _existing_ocr_job(cleaned_variant)
            if ocr_job is None:
                ocr_job = ocr.request_ocr_from_variant(
                    source_variant=cleaned_variant,
                    requested_by_user_id=details.get("requested_by_user_id"),
                    target_formats=details.get("target_formats"),
                    request_language=details.get("request_language")
                    if isinstance(details.get("request_language"), str)
                    else None,
                    engine=details.get("engine")
                    if isinstance(details.get("engine"), str)
                    else None,
                    model=details.get("model") if isinstance(details.get("model"), str) else None,
                    request_options=details.get("request_options")
                    if isinstance(details.get("request_options"), dict)
                    else None,
                )

        finished_at = timezone.now()
        details = {
            **details,
            "cleaned_variant_id": cleaned_variant.id if cleaned_variant else None,
            "ocr_job_id": str(ocr_job.job_id) if ocr_job else None,
            "error": "",
        }
        with transaction.atomic():
            processing_job.state = ProcessingJob.JobState.SUCCESS
            processing_job.finished_at = finished_at
            processing_job.details = details
            processing_job.save(update_fields=["state", "finished_at", "details"])
            scan_job.state = ScanJob.State.COMPLETE
            scan_job.finished_at = finished_at
            scan_job.save(update_fields=["state", "finished_at", "updated_at"])
        logger.info(
            "scan_finalization_succeeded",
            processing_job_id=str(processing_job.job_id),
            scan_job_id=str(scan_job.id),
            cleaned_variant_id=details["cleaned_variant_id"],
            ocr_job_id=details["ocr_job_id"],
        )
    except Exception as exc:
        error = str(exc)
        rq_job = get_current_job() if raise_on_error else None
        retry_pending = bool(
            rq_job is not None and rq_job.retries_left is not None and rq_job.retries_left > 0
        )
        processing_job.state = (
            ProcessingJob.JobState.QUEUED if retry_pending else ProcessingJob.JobState.FAILED
        )
        processing_job.finished_at = None if retry_pending else timezone.now()
        processing_job.details = {**details, "error": error}
        processing_job.save(update_fields=["state", "finished_at", "details"])
        if not retry_pending:
            _restore_scan_after_failure(scan_job, details)
        logger.exception(
            "scan_finalization_failed",
            processing_job_id=str(processing_job.job_id),
            scan_job_id=str(scan_job.id),
            attempt_count=attempt_count,
            retry_pending=retry_pending,
        )
        if raise_on_error:
            raise
