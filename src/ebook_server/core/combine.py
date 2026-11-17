"""Combined whole-book derivative of a confirmed multi-part work (#60).

A reviewer-confirmed split work keeps every original part as its own FileVariant.
On request, a background job joins the parts, in their confirmed ``part_index``
order, into one new derived FileVariant. The originals are only read: each is
re-hashed against its recorded SHA-256 before use, and the output records which
parts, in which order, with which tool, produced it.

Only formats that join mechanically are supported: PDF (via pypdf, already used
for intake metadata) and plain text (decoded with charset-normalizer, written as
UTF-8). Other formats (EPUB, MOBI, ...) have no safe concatenation and are not
offered.
"""

from __future__ import annotations

import shutil
import tempfile
import uuid
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import structlog
from charset_normalizer import from_bytes
from django.db import close_old_connections, transaction
from django.utils import timezone
from pypdf import PdfReader, PdfWriter

from ebook_server.jobs import get_queue, is_stale_running

from . import intake
from .models import Book, FileVariant, ProcessingJob
from .storage_registry import StorageConfigurationError, backend_for_location

logger = structlog.get_logger(__name__)

COMBINABLE_FORMATS = {"pdf": "application/pdf", "txt": "text/plain"}
MAX_COMBINED_INPUT_BYTES = 2 * 1024 * 1024 * 1024
MAX_TEXT_INPUT_BYTES = 256 * 1024 * 1024
OPERATION = "combine_parts"


class CombineError(RuntimeError):
    """Raised when a combined variant cannot be requested or produced."""


def _tool_version(package: str) -> str:
    try:
        return version(package)
    except PackageNotFoundError:
        return "unknown"


def part_sets(book: Book) -> dict[str, list[FileVariant]]:
    """Ordered part variants per format, for formats with at least two parts."""
    sets: dict[str, list[FileVariant]] = {}
    for variant in book.file_variants.filter(
        part_index__isnull=False,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    ).order_by("part_index", "id"):
        sets.setdefault(variant.format, []).append(variant)
    return {fmt: parts for fmt, parts in sets.items() if len(parts) >= 2}


def combine_options_for_book(book: Book) -> list[dict[str, Any]]:
    """Formats whose part set can be combined, and whether that was already done."""
    options = []
    for fmt, parts in part_sets(book).items():
        if fmt not in COMBINABLE_FORMATS:
            continue
        part_ids = [part.id for part in parts]
        existing = _existing_combined(book, fmt, part_ids)
        options.append(
            {
                "format": fmt,
                "part_count": len(parts),
                "part_variant_ids": part_ids,
                "combined_variant_id": existing.id if existing is not None else None,
            },
        )
    return options


def _existing_combined(book: Book, fmt: str, part_ids: list[int]) -> FileVariant | None:
    for variant in book.file_variants.filter(
        format=fmt,
        part_index__isnull=True,
        source_type=FileVariant.SourceType.GENERATED,
    ):
        options = variant.converter_options if isinstance(variant.converter_options, dict) else {}
        if options.get("operation") == OPERATION and options.get("part_variant_ids") == part_ids:
            return variant
    return None


def request_combined_variant(
    *,
    book: Book,
    target_format: str,
    requested_by_user_id: int | None = None,
) -> ProcessingJob:
    fmt = target_format.strip().lower()
    if fmt not in COMBINABLE_FORMATS:
        raise CombineError(f"parts in {target_format} cannot be combined")
    parts = part_sets(book).get(fmt)
    if not parts:
        raise CombineError(f"book has no {fmt} parts to combine")
    part_ids = [part.id for part in parts]
    with transaction.atomic():
        active = (
            ProcessingJob.objects.select_for_update()
            .filter(
                job_type=ProcessingJob.JobType.COMBINE_PARTS,
                state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
                details__book_id=book.id,
                details__target_format=fmt,
            )
            .first()
        )
        if active is not None:
            return active
        job = ProcessingJob.objects.create(
            job_type=ProcessingJob.JobType.COMBINE_PARTS,
            state=ProcessingJob.JobState.QUEUED,
            file_variant=parts[0],
            details={
                "book_id": book.id,
                "target_format": fmt,
                "part_variant_ids": part_ids,
                "requested_by_user_id": requested_by_user_id,
                "error": "",
            },
        )
    job_id = str(job.job_id)
    try:
        queue = get_queue()
        if queue is None:
            process_combine_job(job_id)
        else:
            queue.enqueue(process_combine_job, job_id)
    except Exception as exc:
        logger.exception("combine_dispatch_failed", processing_job_id=job_id)
        job.refresh_from_db()
        if job.state in {ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING}:
            _fail(job, f"combine dispatch failed: {exc}")
    job.refresh_from_db()
    return job


def _fail(job: ProcessingJob, message: str) -> None:
    details = job.details if isinstance(job.details, dict) else {}
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = {**details, "error": message}
    job.save(update_fields=["state", "finished_at", "details"])
    logger.error("combine_failed", processing_job_id=str(job.job_id), error_recorded=True)


def process_combine_job(job_id: str) -> None:
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
    except ProcessingJob.DoesNotExist:
        return
    if job.job_type != ProcessingJob.JobType.COMBINE_PARTS:
        return
    if job.state in {ProcessingJob.JobState.SUCCESS, ProcessingJob.JobState.FAILED}:
        return
    if job.state == ProcessingJob.JobState.RUNNING and not is_stale_running(job):
        return
    details = job.details if isinstance(job.details, dict) else {}
    job.state = ProcessingJob.JobState.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["state", "started_at"])
    try:
        output = _combine(job, details)
    except Exception as exc:
        close_old_connections()
        _fail(job, str(exc))
        return
    job.state = ProcessingJob.JobState.SUCCESS
    job.finished_at = timezone.now()
    job.details = {**details, "output_variant_id": output.id, "error": ""}
    job.save(update_fields=["state", "finished_at", "details"])
    logger.info(
        "combine_succeeded",
        processing_job_id=str(job.job_id),
        output_variant_id=output.id,
        part_count=len(details.get("part_variant_ids", [])),
    )


def _load_parts(details: dict[str, Any]) -> tuple[Book, str, list[FileVariant]]:
    fmt = str(details.get("target_format", ""))
    raw_ids = details.get("part_variant_ids")
    if fmt not in COMBINABLE_FORMATS or not isinstance(raw_ids, list) or len(raw_ids) < 2:
        raise CombineError("invalid combine request")
    by_id = {
        variant.id: variant
        for variant in FileVariant.objects.select_related("book", "storage_location").filter(
            pk__in=raw_ids
        )
    }
    parts = [by_id[part_id] for part_id in raw_ids if part_id in by_id]
    if len(parts) != len(raw_ids):
        raise CombineError("a part variant no longer exists")
    book = parts[0].book
    for part in parts:
        if part.book_id != book.id or part.part_index is None or part.format != fmt:
            raise CombineError("part set changed since the request")
    if [part.part_index for part in parts] != sorted(part.part_index or 0 for part in parts):
        raise CombineError("part order changed since the request")
    return book, fmt, parts


def _stage_parts(parts: list[FileVariant], workdir: Path) -> list[Path]:
    """Copy each original out of storage and verify it against its recorded hash."""
    total = sum(part.size_bytes for part in parts)
    if total > MAX_COMBINED_INPUT_BYTES:
        raise CombineError("parts exceed the combine size limit")
    staged = []
    for position, part in enumerate(parts, start=1):
        try:
            storage = backend_for_location(part.storage_location)
        except StorageConfigurationError as exc:
            raise CombineError("part storage is unavailable") from exc
        path = workdir / f"part-{position:04d}.{part.format}"
        try:
            with storage.open(part.storage_identifier) as source, path.open("wb") as target:
                shutil.copyfileobj(source, target, length=1 << 20)
        except FileNotFoundError as exc:
            raise CombineError(f"part {part.id} is missing from storage") from exc
        if intake.compute_sha256(str(path)) != part.sha256:
            raise CombineError(f"part {part.id} does not match its recorded checksum")
        staged.append(path)
    return staged


def _combine_pdf(paths: list[Path], labels: list[str], output: Path) -> str:
    writer = PdfWriter()
    for path, label in zip(paths, labels, strict=True):
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            raise CombineError("an encrypted PDF part cannot be combined")
        start = len(writer.pages)
        writer.append(reader)
        if label and len(writer.pages) > start:
            writer.add_outline_item(label, start)
    with output.open("wb") as handle:
        writer.write(handle)
    return f"pypdf {_tool_version('pypdf')}"


def _combine_text(paths: list[Path], output: Path) -> str:
    if sum(path.stat().st_size for path in paths) > MAX_TEXT_INPUT_BYTES:
        raise CombineError("text parts exceed the combine size limit")
    with output.open("w", encoding="utf-8", newline="") as handle:
        for position, path in enumerate(paths):
            match = from_bytes(path.read_bytes()).best()
            if match is None:
                raise CombineError("a text part has an undetectable encoding")
            text = str(match).lstrip("﻿")
            if position:
                handle.write("\n\n")
            handle.write(text.rstrip("\n"))
        handle.write("\n")
    return f"charset-normalizer {_tool_version('charset-normalizer')}"


def _combine(job: ProcessingJob, details: dict[str, Any]) -> FileVariant:
    book, fmt, parts = _load_parts(details)
    part_ids = [part.id for part in parts]
    existing = _existing_combined(book, fmt, part_ids)
    if existing is not None:
        return existing
    with tempfile.TemporaryDirectory(prefix="ebook-combine-") as raw_workdir:
        workdir = Path(raw_workdir)
        staged = _stage_parts(parts, workdir)
        output = workdir / f"combined.{fmt}"
        if fmt == "pdf":
            tool_version = _combine_pdf(staged, [part.part_label for part in parts], output)
            tool = "pypdf"
        else:
            tool_version = _combine_text(staged, output)
            tool = "text-concat"
        sha = intake.compute_sha256(str(output))
        size = output.stat().st_size
        duplicate = FileVariant.objects.filter(sha256=sha).first()
        if duplicate is not None:
            return duplicate
        storage = backend_for_location(parts[0].storage_location)
        filename = intake.normalize_filename(
            f"{intake._sanitize_title_for_filename(book.title)} (combined).{fmt}"
        )
        storage_identifier = storage.generate_destination(
            f"books/{book.id}/combined/{uuid.uuid4()}", filename
        )
        storage.ensure_free_space(size)
        storage.store(str(output), storage_identifier)
        if storage.hash(storage_identifier) != sha:
            raise CombineError("stored combined file failed verification")
    return FileVariant.objects.create(
        book=book,
        format=fmt,
        mime_type=COMBINABLE_FORMATS[fmt],
        original_filename=filename,
        storage_location=parts[0].storage_location,
        storage_identifier=storage_identifier,
        size_bytes=size,
        sha256=sha,
        source_type=FileVariant.SourceType.GENERATED,
        uploader=None,
        source_variant=parts[0],
        imported_at=timezone.now(),
        converter_tool=tool,
        converter_version=tool_version[:64],
        converter_options={
            "operation": OPERATION,
            "part_variant_ids": part_ids,
            "part_sha256": [part.sha256 for part in parts],
            "part_labels": [part.part_label for part in parts],
        },
        job_id=job.job_id,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )


def combine_job_payload(job: ProcessingJob) -> dict[str, Any]:
    details = job.details if isinstance(job.details, dict) else {}
    output_variant_id = details.get("output_variant_id")
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "book_id": details.get("book_id"),
        "target_format": details.get("target_format"),
        "part_variant_ids": details.get("part_variant_ids", []),
        "output_variant_id": str(output_variant_id) if output_variant_id else None,
        "error": details.get("error", ""),
    }
