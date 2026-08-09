"""Scan-tailor to library pipeline helpers."""

from __future__ import annotations

import tempfile
import uuid
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import img2pdf
from django.conf import settings as django_settings
from django.db import connection
from django.db.models import Q
from django.utils import timezone

from ebook_server.storage import LocalFilesystemStorage

from . import conversion, intake
from .models import (
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanTailorStageState,
    StorageLocation,
)


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
) -> tuple[str, str] | None:
    output_state = _active_output_state(page)
    if (
        output_state is not None
        and output_state.output_storage_identifier
        and output_state.output_storage_identifier != ""
    ):
        return ("scan_stage_output", output_state.output_storage_identifier)

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

    result: dict[str, Any] = {
        "page_count": page_count,
        "ready_pages": ready_count,
        "missing_pages": [
            entry["page_id"] for entry in resolved_sources if entry["status"] != "ready"
        ],
        "stage_counts": {"output_stage_success": ready_count},
    }

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


def _collect_cleaned_sources(job: ScanJob) -> list[tuple[ScanPage, str, str]]:
    entries: list[tuple[ScanPage, str, str]] = []
    for page in _ordered_pages(job):
        source = _select_page_source_identifier(page)
        if source is None:
            continue
        source_kind, source_identifier = source
        entries.append((page, source_kind, source_identifier))
    return entries


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

        destination.write_bytes(img2pdf.convert([str(path) for path in image_paths]))
        return destination
    except Exception:
        destination.unlink(missing_ok=True)
        raise


def create_cleaned_variant_for_scan_job(
    job: ScanJob,
    *,
    request_name: str | None = None,
) -> FileVariant:
    if job.book_id is None:
        raise ScanPipelineError("scan job is not linked to a book")

    sources = _collect_cleaned_sources(job)
    if not sources:
        raise ScanPipelineError("scan job has no resolvable page sources")

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
            "source_kind": "scan_output_or_raw",
            "lineage": lineage,
            "request_name": request_name or "cleaned",
        },
    )
