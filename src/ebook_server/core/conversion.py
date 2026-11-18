"""Conversion orchestration for FileVariant transformation."""

from __future__ import annotations

import errno
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from math import ceil
from pathlib import Path
from typing import Any

import structlog
from django.conf import settings as django_settings
from django.db import close_old_connections, transaction
from django.utils import timezone

from ebook_server.jobs import get_queue, is_stale_running, tunable_setting
from ebook_server.storage import StorageBackend

from . import archive_layout, intake, worker_failures
from .models import Book, FileVariant, ProcessingJob
from .referenced_files import ReferencedFileError, ensure_reference_intact
from .storage_registry import (
    StorageConfigurationError,
    backend_for_location,
    writable_location_for,
)

logger = structlog.get_logger(__name__)
_CURRENT_SCRATCH: ContextVar[Path | None] = ContextVar("converter_scratch", default=None)


@contextmanager
def _conversion_scratch(root: Path) -> Iterator[None]:
    token = _CURRENT_SCRATCH.set(root)
    try:
        yield
    finally:
        _CURRENT_SCRATCH.reset(token)


class ConversionError(RuntimeError):
    """Raised when conversion cannot complete."""


@dataclass(frozen=True)
class PreparedConversion:
    """Private, verified bytes awaiting an ownership-checked archive publication."""

    variant: FileVariant
    storage: StorageBackend
    staging_identifier: str
    namespace: str
    filename: str


@dataclass(frozen=True)
class ConversionStep:
    source_format: str
    target_format: str
    converter: str


@dataclass(frozen=True)
class ConversionCapability:
    source_format: str
    target_format: str
    converter: str


@dataclass(frozen=True)
class ConversionPlan:
    steps: tuple[ConversionStep, ...]


class Converter:
    name = ""
    binary = ""

    def is_available(self) -> bool:
        return bool(shutil.which(self.binary))

    def version(self) -> str:
        return "unknown"

    def convert(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        source_format: str,
        target_format: str,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        raise NotImplementedError


class CalibreConverter(Converter):
    name = "calibre"
    binary = "ebook-convert"

    def version(self) -> str:
        output = _run_conversion_command(
            [self.binary, "--version"],
            check=True,
            capture_output=True,
            timeout=min(VERSION_PROBE_TIMEOUT_SECONDS, _converter_timeout_seconds()),
        )
        return _version_summary(output.stdout, fallback="calibre-unknown")

    def convert(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        source_format: str,
        target_format: str,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        _ = (source_format, target_format, options)
        _run_conversion_command(
            [self.binary, str(source_path), str(destination_path)],
            check=True,
            scratch_dir=destination_path.parent,
            output_path=destination_path,
        )


class PandocConverter(Converter):
    name = "pandoc"
    binary = "pandoc"

    def version(self) -> str:
        output = _run_conversion_command(
            [self.binary, "--version"],
            check=True,
            capture_output=True,
            timeout=min(VERSION_PROBE_TIMEOUT_SECONDS, _converter_timeout_seconds()),
        )
        return _version_summary(output.stdout, fallback="pandoc-unknown")

    def convert(
        self,
        *,
        source_path: Path,
        destination_path: Path,
        source_format: str,
        target_format: str,
        options: Mapping[str, Any] | None = None,
    ) -> None:
        _ = (source_format, target_format, options)
        _run_conversion_command(
            [self.binary, str(source_path), "-o", str(destination_path)],
            check=True,
            scratch_dir=destination_path.parent,
            output_path=destination_path,
        )


CONVERTER_REGISTRY: dict[str, Converter] = {
    "calibre": CalibreConverter(),
    "pandoc": PandocConverter(),
}

# Only routes with binary-verified acceptance evidence are advertised (Sprint020). AZW,
# AZW3, PRC and RTF are intake formats (#23) but stay without conversion routes until
# real ebook-convert acceptance coverage exists; see docs/conversion-matrix.md.
CAPABILITIES: tuple[ConversionCapability, ...] = (
    ConversionCapability("epub", "pdf", "calibre"),
    ConversionCapability("pdf", "epub", "calibre"),
    ConversionCapability("epub", "mobi", "calibre"),
    ConversionCapability("mobi", "epub", "calibre"),
    ConversionCapability("epub", "txt", "calibre"),
    ConversionCapability("pdf", "txt", "calibre"),
    ConversionCapability("html", "epub", "pandoc"),
    ConversionCapability("txt", "epub", "pandoc"),
)

FORMAT_TO_MIME: dict[str, str] = {
    "epub": "application/epub+zip",
    "mobi": "application/x-mobipocket-ebook",
    "azw": "application/vnd.amazon.ebook",
    "azw3": "application/x-mobi8-ebook",
    "prc": "application/x-mobipocket-ebook",
    "rtf": "application/rtf",
    "pdf": "application/pdf",
    "txt": "text/plain",
    "html": "text/html",
    "fb2": "application/x-fictionbook+xml",
    "djvu": "image/vnd.djvu",
    "cbz": "application/vnd.comicbook+zip",
    "cbr": "application/vnd.comicbook-rar",
    "png": "image/png",
    "tiff": "image/tiff",
}


def _normalize_format(format_name: str) -> str:
    return format_name.strip().lower()


def _is_known_format(format_name: str) -> bool:
    return _normalize_format(format_name) in FORMAT_TO_MIME


def _available_graph() -> dict[str, tuple[ConversionStep, ...]]:
    available: list[ConversionStep] = []
    for capability in CAPABILITIES:
        converter = CONVERTER_REGISTRY.get(capability.converter)
        if converter is None or not converter.is_available():
            continue
        available.append(
            ConversionStep(
                source_format=capability.source_format,
                target_format=capability.target_format,
                converter=capability.converter,
            ),
        )
    by_source: dict[str, list[ConversionStep]] = {}
    for step in available:
        by_source.setdefault(step.source_format, []).append(step)
    return {source: tuple(steps) for source, steps in by_source.items()}


def find_conversion_plan(source_format: str, target_format: str) -> ConversionPlan | None:
    source = _normalize_format(source_format)
    target = _normalize_format(target_format)
    if source == target:
        return None
    if not _is_known_format(source) or not _is_known_format(target):
        return None

    graph = _available_graph()
    queue: deque[tuple[str, tuple[ConversionStep, ...]]] = deque([(source, ())])
    seen = {source}
    while queue:
        current, path = queue.popleft()
        for edge in graph.get(current, ()):
            new_path = path + (edge,)
            if edge.target_format == target:
                return ConversionPlan(steps=new_path)
            if edge.target_format in seen:
                continue
            seen.add(edge.target_format)
            queue.append((edge.target_format, new_path))
    return None


def list_conversion_targets(source_format: str) -> dict[str, ConversionPlan]:
    source = _normalize_format(source_format)
    if not _is_known_format(source):
        return {}

    graph = _available_graph()
    queue: deque[tuple[str, tuple[ConversionStep, ...]]] = deque([(source, ())])
    seen = {source}
    targets: dict[str, ConversionPlan] = {}
    while queue:
        current, path = queue.popleft()
        for edge in graph.get(current, ()):
            new_path = path + (edge,)
            if edge.target_format != source and edge.target_format not in targets:
                targets[edge.target_format] = ConversionPlan(steps=new_path)
            if edge.target_format in seen:
                continue
            seen.add(edge.target_format)
            queue.append((edge.target_format, new_path))
    return targets


def describe_plan(plan: ConversionPlan) -> list[dict[str, Any]]:
    return [
        {
            "source_format": step.source_format,
            "target_format": step.target_format,
            "converter": step.converter,
        }
        for step in plan.steps
    ]


def conversion_options_for_book(book: Book) -> list[dict[str, Any]]:
    options: list[dict[str, Any]] = []
    # A part of a split work (#60) is not the whole book: converting one chapter would
    # yield a "complete" variant that is really a fragment.
    for variant in book.file_variants.filter(
        processing_state=FileVariant.ProcessingState.COMPLETE,
        part_index__isnull=True,
    ):
        for target_format, route in list_conversion_targets(variant.format).items():
            options.append(
                {
                    "source_variant_id": variant.id,
                    "source_format": variant.format,
                    "target_format": target_format,
                    "hops": len(route.steps),
                    "route": describe_plan(route),
                },
            )
    return options


def request_conversion(
    *,
    source_variant: FileVariant,
    target_format: str,
    requested_by_user_id: int | None = None,
    request_options: Mapping[str, Any] | None = None,
) -> ProcessingJob:
    if source_variant.part_index is not None:
        raise ConversionError("a single part of a multi-part work cannot be converted alone")
    normalized_target = _normalize_format(target_format)
    plan = find_conversion_plan(source_variant.format, normalized_target)
    if plan is None or not plan.steps:
        raise ConversionError(
            f"no conversion route from {source_variant.format} to {target_format}",
        )

    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.CONVERSION,
        state=ProcessingJob.JobState.QUEUED,
        file_variant=source_variant,
        details={
            "book_id": source_variant.book_id,
            "source_variant_id": source_variant.id,
            "target_format": normalized_target,
            "request_options": dict(request_options or {}),
            "route": [step.__dict__ for step in plan.steps],
            "requested_by_user_id": requested_by_user_id,
        },
    )
    _dispatch_conversion_job(job)
    job.refresh_from_db()
    return job


def _dispatch_conversion_job(job: ProcessingJob) -> None:
    job_id = str(job.job_id)
    try:
        queue = get_queue()
        if queue is None:
            process_conversion_job(job_id)
            return
        queue.enqueue(process_conversion_job, job_id)
    except Exception as exc:
        logger.exception("conversion_dispatch_failed", processing_job_id=job_id)
        _fail_conversion_job(job, f"conversion dispatch failed: {exc}")


def _claim_conversion_job(job_id: str) -> tuple[ProcessingJob, str] | None:
    """Claim one delivery, or replace a stale attempt, in a short transaction."""
    with transaction.atomic():
        job = (
            ProcessingJob.objects.select_for_update()
            .filter(job_id=job_id, job_type=ProcessingJob.JobType.CONVERSION)
            .first()
        )
        if job is None or not (job.state == ProcessingJob.JobState.QUEUED or is_stale_running(job)):
            return None
        token = str(uuid.uuid4())
        details = dict(job.details) if isinstance(job.details, dict) else {}
        job.details = {**details, "conversion_attempt": token}
        job.state = ProcessingJob.JobState.RUNNING
        job.started_at = timezone.now()
        job.finished_at = None
        job.save(update_fields=["state", "started_at", "finished_at", "details"])
        return job, token


def _owns_conversion(job: ProcessingJob, token: str) -> bool:
    return (
        job.state == ProcessingJob.JobState.RUNNING
        and isinstance(job.details, dict)
        and job.details.get("conversion_attempt") == token
    )


def process_conversion_job(job_id: str) -> None:
    claim = _claim_conversion_job(job_id)
    if claim is None:
        return
    conversion_job, token = claim
    details = conversion_job.details if isinstance(conversion_job.details, dict) else {}
    source_variant = conversion_job.file_variant
    if source_variant is None:
        _fail_conversion_job(conversion_job, "missing source variant", token=token)
        if worker_failures.retry_pending(conversion_job):
            raise ConversionError("missing source variant")
        return
    logger.info(
        "conversion_started",
        processing_job_id=str(conversion_job.job_id),
        source_variant_id=source_variant.id,
        target_format=details.get("target_format"),
    )

    prepared: PreparedConversion | None = None
    registered = False
    try:
        route_payload = details.get("route")
        if not isinstance(route_payload, list) or not route_payload:
            raise ConversionError("conversion route missing")
        target_format = _normalize_format(details.get("target_format", ""))
        route = _deserialize_route(route_payload)
        prepared = _execute_plan(
            source_variant=source_variant,
            target_format=target_format,
            route=route,
            request_options=details.get("request_options"),
            processing_job_id=conversion_job.job_id,
        )
        output_variant = _complete_conversion_job(conversion_job, token, prepared)
        if output_variant is None:
            return
        registered = output_variant is prepared.variant
        logger.info(
            "conversion_succeeded",
            processing_job_id=str(conversion_job.job_id),
            output_variant_id=output_variant.id,
            target_format=target_format,
        )
    except Exception as exc:  # pragma: no cover - defensive path
        close_old_connections()
        failed = _fail_conversion_job(conversion_job, str(exc), token=token)
        if failed and worker_failures.retry_pending(conversion_job):
            raise
    finally:
        if prepared is not None:
            _discard_unpublished_conversion(prepared, registered=registered)


def _complete_conversion_job(
    snapshot: ProcessingJob, token: str, prepared: PreparedConversion
) -> FileVariant | None:
    """Fence exclusive publication and metadata; prefer promotion without copying."""
    variant = prepared.variant
    with transaction.atomic():
        job = ProcessingJob.objects.select_for_update().filter(pk=snapshot.pk).first()
        if job is None or not _owns_conversion(job, token):
            return None
        # Serialize reuse across distinct conversions of the same source as well.
        FileVariant.objects.select_for_update().get(pk=variant.source_variant_id)
        output = FileVariant.objects.filter(
            book_id=variant.book_id,
            source_variant_id=variant.source_variant_id,
            format=variant.format,
            source_type=FileVariant.SourceType.CONVERTED,
            sha256=variant.sha256,
        ).first()
        if output is None:
            try:
                variant.storage_identifier = prepared.storage.publish_unique(
                    prepared.staging_identifier, prepared.namespace, prepared.filename, mode="link"
                )
            except OSError as exc:
                if exc.errno not in {
                    errno.EXDEV,
                    errno.EPERM,
                    errno.EACCES,
                    errno.ENOSYS,
                    errno.EOPNOTSUPP,
                }:
                    raise
                # Some SMB mounts cannot hard-link. Preserve their existing safe
                # exclusive-copy path, keeping ownership locked until it finishes.
                variant.storage_identifier = prepared.storage.publish_unique(
                    prepared.staging_identifier, prepared.namespace, prepared.filename, mode="copy"
                )
            variant.original_filename = Path(variant.storage_identifier).name
            variant.save(force_insert=True)
            output = variant
        job.state = ProcessingJob.JobState.SUCCESS
        job.finished_at = timezone.now()
        job.details = {
            **job.details,
            "output_variant_id": output.pk,
            "output_format": variant.format,
            "error": "",
        }
        job.save(update_fields=["state", "finished_at", "details"])
        return output


def _discard_unpublished_conversion(prepared: PreparedConversion, *, registered: bool) -> None:
    """Remove only this attempt's exclusive output, never an adopted artifact.

    If the database cannot confirm that the file is unreferenced, leave it for
    operator reconciliation instead of risking a committed variant's contents.
    """
    identifiers = {prepared.staging_identifier}
    if not registered:
        identifiers.add(prepared.variant.storage_identifier)
    for identifier in identifiers:
        try:
            if FileVariant.objects.filter(
                storage_location_id=prepared.variant.storage_location_id,
                storage_identifier=identifier,
            ).exists():
                continue
            prepared.storage.delete(identifier)
        except Exception:
            logger.warning(
                "conversion_unpublished_cleanup_failed", job_id=str(prepared.variant.job_id)
            )


def _deserialize_route(raw: list[Any]) -> tuple[ConversionStep, ...]:
    steps: list[ConversionStep] = []
    for raw_step in raw:
        if not isinstance(raw_step, dict):
            continue
        source_format = raw_step.get("source_format")
        target_format = raw_step.get("target_format")
        converter = raw_step.get("converter")
        if not (
            isinstance(source_format, str)
            and isinstance(target_format, str)
            and isinstance(converter, str)
        ):
            continue
        steps.append(
            ConversionStep(
                source_format=_normalize_format(source_format),
                target_format=_normalize_format(target_format),
                converter=converter,
            ),
        )
    return tuple(steps)


def _execute_plan(
    *,
    source_variant: FileVariant,
    target_format: str,
    route: tuple[ConversionStep, ...],
    request_options: Any | None,
    processing_job_id: uuid.UUID,
) -> PreparedConversion:
    _validate_route(
        source_format=source_variant.format,
        target_format=target_format,
        route=route,
    )
    try:
        ensure_reference_intact(source_variant)
    except ReferencedFileError as exc:
        raise ConversionError(str(exc)) from exc
    # Converted output never goes onto a read-only (indexed-in-place) location.
    output_location = writable_location_for(source_variant.storage_location)
    try:
        storage = backend_for_location(source_variant.storage_location)
        output_storage = backend_for_location(output_location)
    except StorageConfigurationError as exc:
        raise ConversionError("source storage is unavailable") from exc

    request_options_map: dict[str, Any] = {}
    if isinstance(request_options, dict):
        request_options_map = dict(request_options)

    scratch_root = tunable_setting("BOOK_SCANNER_CONVERTER_SCRATCH_ROOT", "") or None
    with (
        tempfile.TemporaryDirectory(prefix="ebook-convert-", dir=scratch_root) as workdir,
        _conversion_scratch(Path(workdir)),
    ):
        source_suffix = Path(source_variant.original_filename).suffix or f".{source_variant.format}"
        source_path = Path(workdir) / f"source{source_suffix}"
        try:
            with storage.open(source_variant.storage_identifier) as source_stream:
                with source_path.open("wb") as staged_source:
                    copied = 0
                    while chunk := source_stream.read(1 << 20):
                        copied += len(chunk)
                        if copied > _resource_limit("MAX_SCRATCH_BYTES", 4 * 1024**3):
                            raise ConversionError("converter scratch limit exceeded by source")
                        staged_source.write(chunk)
        except FileNotFoundError as exc:
            raise ConversionError("source artifact missing") from exc

        current_path = source_path
        converter_chain: list[str] = []
        converter_versions: list[str] = []
        for index, step in enumerate(route, start=1):
            destination = Path(workdir) / f"step-{index}.{step.target_format}"
            converter = CONVERTER_REGISTRY.get(step.converter)
            if converter is None:
                raise ConversionError(f"unknown converter: {step.converter}")
            if not converter.is_available():
                raise ConversionError(f"converter unavailable: {step.converter}")
            converter_chain.append(converter.name)
            converter_versions.append(converter.version())
            converter.convert(
                source_path=current_path,
                destination_path=destination,
                source_format=step.source_format,
                target_format=step.target_format,
                options=request_options_map,
            )
            if not destination.exists():
                raise ConversionError(f"converter produced no output: {converter.name}")
            _check_conversion_files(Path(workdir), destination)
            current_path = destination

        output_size = current_path.stat().st_size
        output_sha = intake.compute_sha256(str(current_path))
        namespace = (
            f"books/{source_variant.book_id}/converted"
            if archive_layout.configured_layout().job_scoped
            else archive_layout.book_folder(source_variant.book, output_storage)
        )
        filename = (
            f"{uuid.uuid4()}.{target_format}"
            if archive_layout.configured_layout().job_scoped
            else f"{archive_layout.book_file_stem(source_variant.book)}.{target_format}"
        )
        staging_identifier = output_storage.generate_destination(
            f"_conversion_staging/{processing_job_id}", f"{uuid.uuid4()}.{target_format}"
        )
        try:
            output_storage.store_atomic(str(current_path), staging_identifier)
            if output_storage.hash(staging_identifier) != output_sha:
                raise ConversionError("staged conversion checksum mismatch")
        except BaseException:
            output_storage.delete(staging_identifier)
            raise
        variant = FileVariant(
            book=source_variant.book,
            format=target_format,
            mime_type=FORMAT_TO_MIME.get(target_format, "application/octet-stream"),
            original_filename=filename,
            storage_location=output_location,
            storage_identifier=staging_identifier,
            size_bytes=output_size,
            sha256=output_sha,
            source_type=FileVariant.SourceType.CONVERTED,
            uploader_id=source_variant.uploader_id,
            source_variant=source_variant,
            imported_at=timezone.now(),
            converter_tool=" -> ".join(converter_chain),
            converter_version=" -> ".join(converter_versions),
            converter_options={
                "route": [
                    {
                        "source_format": step.source_format,
                        "target_format": step.target_format,
                        "converter": step.converter,
                    }
                    for step in route
                ],
                "request_options": request_options_map,
            },
            job_id=processing_job_id,
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )
        return PreparedConversion(variant, output_storage, staging_identifier, namespace, filename)


def _validate_route(
    *,
    source_format: str,
    target_format: str,
    route: tuple[ConversionStep, ...],
) -> None:
    if not route:
        raise ConversionError("conversion route is empty")

    current_format = _normalize_format(source_format)
    allowed_capabilities = {
        (capability.source_format, capability.target_format, capability.converter)
        for capability in CAPABILITIES
    }
    for step in route:
        if step.source_format != current_format:
            raise ConversionError("conversion route is disconnected")
        if (step.source_format, step.target_format, step.converter) not in allowed_capabilities:
            raise ConversionError("conversion route contains an unsupported step")
        if step.converter not in CONVERTER_REGISTRY:
            raise ConversionError(f"unknown converter: {step.converter}")
        current_format = step.target_format

    if current_format != _normalize_format(target_format):
        raise ConversionError("conversion route does not reach requested target")


def _fail_conversion_job(job: ProcessingJob, message: str, *, token: str | None = None) -> bool:
    """An enqueue error owns only QUEUED; worker errors own only their attempt."""
    with transaction.atomic():
        current = ProcessingJob.objects.select_for_update().filter(pk=job.pk).first()
        if current is None or current.job_type != ProcessingJob.JobType.CONVERSION:
            return False
        if token is None:
            if (
                current.state != ProcessingJob.JobState.QUEUED
                or current.started_at != job.started_at
                or current.details != job.details
            ):
                return False
        elif not _owns_conversion(current, token):
            return False
        details = dict(current.details) if isinstance(current.details, dict) else {}
        pending = worker_failures.retry_pending(current)
        current.state = ProcessingJob.JobState.QUEUED if pending else ProcessingJob.JobState.FAILED
        current.finished_at = None if pending else timezone.now()
        current.details = {**details, "error": message}
        current.save(update_fields=["state", "finished_at", "details"])
    logger.error(
        "conversion_failed",
        processing_job_id=str(job.job_id),
        error_recorded=bool(message),
    )
    return True


def recover_stale_conversion_job(snapshot: ProcessingJob, message: str) -> bool:
    """Recover only the exact state inspected by the operator, not a newer attempt."""
    with transaction.atomic():
        current = ProcessingJob.objects.select_for_update().filter(pk=snapshot.pk).first()
        if (
            current is None
            or current.job_type != ProcessingJob.JobType.CONVERSION
            or current.state != snapshot.state
            or current.started_at != snapshot.started_at
            or current.details != snapshot.details
            or current.state not in {ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING}
            or (current.state == ProcessingJob.JobState.RUNNING and not is_stale_running(current))
        ):
            return False
        current.state = ProcessingJob.JobState.FAILED
        current.finished_at = timezone.now()
        details = dict(current.details) if isinstance(current.details, dict) else {}
        current.details = {**details, "error": message}
        current.save(update_fields=["state", "finished_at", "details"])
        return True


STDERR_TAIL_BYTES = 2000
DEFAULT_CONVERTER_TIMEOUT_SECONDS = 1800
VERSION_PROBE_TIMEOUT_SECONDS = 30


def _converter_timeout_seconds() -> float:
    """Per-command timeout, kept below the RQ job timeout so we fail with context."""
    queue_timeout = float(getattr(django_settings, "BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS", 3600))
    configured = tunable_setting(
        "BOOK_SCANNER_CONVERTER_TIMEOUT_SECONDS", DEFAULT_CONVERTER_TIMEOUT_SECONDS
    )
    return max(1.0, min(float(configured), queue_timeout - 60.0))


def _stderr_tail(raw: bytes | str | None) -> str:
    if not raw:
        return ""
    data = raw.encode("utf-8", "replace") if isinstance(raw, str) else raw
    return data[-STDERR_TAIL_BYTES:].decode("utf-8", "replace").strip()


def _resource_limit(suffix: str, default: int) -> int:
    value = int(tunable_setting(f"BOOK_SCANNER_CONVERTER_{suffix}", default))
    if value <= 0:
        raise ConversionError(f"converter {suffix.lower()} must be positive")
    return value


def _check_conversion_files(root: Path, output: Path | None = None) -> None:
    """Bound logical scratch bytes/entries without following tool-created symlinks."""
    if output is not None and output.exists():
        info = output.lstat()
        if not stat.S_ISREG(info.st_mode):
            raise ConversionError("converter output is not a regular file")
        if info.st_size > _resource_limit("MAX_OUTPUT_BYTES", 1024**3):
            raise ConversionError("converter output limit exceeded")
    maximum = _resource_limit("MAX_SCRATCH_BYTES", 4 * 1024**3)
    total = 0
    entries = 0
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            iterator = os.scandir(directory)
        except FileNotFoundError:
            continue
        with iterator as items:
            for item in items:
                entries += 1
                if entries > 10000:
                    raise ConversionError("converter scratch file-count limit exceeded")
                try:
                    info = item.stat(follow_symlinks=False)
                except FileNotFoundError:  # a tool removed a temporary file during sampling
                    continue
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(item.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                else:
                    raise ConversionError("converter scratch contains a non-regular file")
                if total > maximum:
                    raise ConversionError("converter scratch limit exceeded")


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    try:
        if sys.platform == "win32":
            # Windows deployment needs an external job-object/container boundary
            # for descendant limits; production workers are the Linux systemd units.
            if process.poll() is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        if process.poll() is None:
            process.kill()


def _run_conversion_command(
    command: list[str],
    check: bool = False,
    capture_output: bool = False,
    timeout: float | None = None,
    *,
    scratch_dir: Path | None = None,
    output_path: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Bound diagnostics in RAM, monitor scratch, and reap the converter group.

    A private cwd and TMPDIR keep normal converter scratch in the monitored tree.
    POSIX exec limits bound each file and CPU (optionally address space) before
    the tool starts; the sampled aggregate limit complements a filesystem quota.
    """
    if scratch_dir is None:
        scratch_dir = _CURRENT_SCRATCH.get()
    if scratch_dir is None:
        scratch_root = tunable_setting("BOOK_SCANNER_CONVERTER_SCRATCH_ROOT", "") or None
        with tempfile.TemporaryDirectory(prefix="ebook-probe-", dir=scratch_root) as workdir:
            return _run_conversion_command(
                command,
                check,
                capture_output,
                timeout,
                scratch_dir=Path(workdir),
                output_path=output_path,
            )
    scratch_dir = scratch_dir.resolve()
    _check_conversion_files(scratch_dir, output_path)
    effective_timeout = timeout if timeout is not None else _converter_timeout_seconds()
    diagnostic_limit = _resource_limit("MAX_DIAGNOSTIC_BYTES", 1024 * 1024)
    address_limit = tunable_setting("BOOK_SCANNER_CONVERTER_ADDRESS_SPACE_BYTES", None)
    memory_limit = 0 if address_limit is None else _resource_limit("ADDRESS_SPACE_BYTES", 0)
    file_limit = min(
        _resource_limit("MAX_OUTPUT_BYTES", 1024**3),
        _resource_limit("MAX_SCRATCH_BYTES", 4 * 1024**3),
    )
    executable = command
    if os.name == "posix":
        executable = [
            sys.executable,
            "-I",
            str(Path(__file__).parents[1] / "converter_limits.py"),
            str(file_limit),
            str(memory_limit),
            str(max(1, ceil(effective_timeout))),
            *command,
        ]
    environment = {
        **os.environ,
        "TMPDIR": str(scratch_dir),
        "TEMP": str(scratch_dir),
        "TMP": str(scratch_dir),
        "CALIBRE_TEMP_DIR": str(scratch_dir),
    }
    process = subprocess.Popen(
        executable,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture_output else subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        start_new_session=True,
        cwd=scratch_dir,
        env=environment,
    )
    stdout = bytearray()
    stderr = bytearray()
    diagnostic_bytes = 0
    exceeded = threading.Event()
    capture_lock = threading.Lock()

    def drain(stream: Any, *, is_stderr: bool) -> None:
        nonlocal diagnostic_bytes
        try:
            while chunk := stream.read(8192):
                with capture_lock:
                    diagnostic_bytes += len(chunk)
                    if diagnostic_bytes > diagnostic_limit:
                        exceeded.set()
                    if is_stderr:
                        stderr.extend(chunk)
                        del stderr[:-STDERR_TAIL_BYTES]
                    else:
                        stdout.extend(chunk[: max(0, min(diagnostic_limit, 65536) - len(stdout))])
        finally:
            stream.close()

    readers = [
        threading.Thread(
            target=drain, args=(process.stderr,), kwargs={"is_stderr": True}, daemon=True
        )
    ]
    if process.stdout is not None:
        readers.append(
            threading.Thread(
                target=drain, args=(process.stdout,), kwargs={"is_stderr": False}, daemon=True
            )
        )
    deadline = time.monotonic() + effective_timeout
    failure: str | None = None
    try:
        for reader in readers:
            reader.start()
        while True:
            _check_conversion_files(scratch_dir, output_path)
            if exceeded.is_set():
                raise ConversionError("converter diagnostic limit exceeded")
            if process.poll() is not None and all(not reader.is_alive() for reader in readers):
                break
            if time.monotonic() >= deadline:
                raise ConversionError(
                    f"{Path(command[0]).name} timed out after {effective_timeout:g}s"
                )
            time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    except ConversionError as exc:
        failure = str(exc)
    finally:
        # Also reap children left behind by a wrapper that exited before its tool.
        _kill_process_group(process)
        process.wait()
        for reader in readers:
            if reader.ident is not None:
                reader.join(timeout=5)
    _check_conversion_files(scratch_dir, output_path)
    if exceeded.is_set() and failure is None:
        failure = "converter diagnostic limit exceeded"
    tail = _stderr_tail(bytes(stderr))
    if failure is not None:
        raise ConversionError(failure + (f": {tail}" if tail else ""))
    if check and process.returncode != 0:
        raise ConversionError(
            f"{Path(command[0]).name} exited with status {process.returncode}"
            + (f": {tail}" if tail else ""),
        )
    return subprocess.CompletedProcess(
        command,
        process.returncode,
        bytes(stdout).decode("utf-8", "replace") if capture_output else None,
        tail,
    )


def _version_summary(raw_output: object, *, fallback: str) -> str:
    if not isinstance(raw_output, str):
        return fallback
    first_line = next((line.strip() for line in raw_output.splitlines() if line.strip()), "")
    return first_line[:64] or fallback


def conversion_job_payload(job: ProcessingJob) -> dict[str, Any]:
    details = job.details if isinstance(job.details, dict) else {}
    output_variant_id = details.get("output_variant_id")
    route = details.get("route")
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "source_variant_id": details.get("source_variant_id"),
        "target_format": details.get("target_format"),
        "route": route if isinstance(route, list) else [],
        "output_variant_id": str(output_variant_id) if output_variant_id else None,
        "error": details.get("error", ""),
    }
