"""Conversion orchestration for FileVariant transformation."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import uuid
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.utils import timezone

from ebook_server.jobs import get_queue
from ebook_server.storage import LocalFilesystemStorage

from . import intake
from .models import Book, FileVariant, ProcessingJob


class ConversionError(RuntimeError):
    """Raised when conversion cannot complete."""


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
        )
        text = output.stdout.strip() if isinstance(output.stdout, str) else ""
        return text or "calibre-unknown"

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
        )


class PandocConverter(Converter):
    name = "pandoc"
    binary = "pandoc"

    def version(self) -> str:
        output = _run_conversion_command(
            [self.binary, "--version"],
            check=True,
            capture_output=True,
        )
        text = output.stdout.strip() if isinstance(output.stdout, str) else ""
        return text or "pandoc-unknown"

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
        )


class CopierConverter(Converter):
    """Internal fallback to explicitly model copy-based transformations."""

    name = "copy"
    binary = "copy"

    def is_available(self) -> bool:
        return True

    def version(self) -> str:
        return "copy-only"

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
        destination_path.write_bytes(source_path.read_bytes())


CONVERTER_REGISTRY: dict[str, Converter] = {
    "calibre": CalibreConverter(),
    "pandoc": PandocConverter(),
}

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
    "pdf": "application/pdf",
    "txt": "text/plain",
    "html": "text/html",
    "fb2": "application/fb2",
    "djvu": "image/vnd.djvu",
    "cbz": "application/x-cbz",
    "cbr": "application/x-cbr",
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
        converter = CONVERTER_REGISTRY.get(capability.converter, CopierConverter())
        if not converter.is_available():
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
    for variant in book.file_variants.all():
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


def _default_storage() -> LocalFilesystemStorage:
    return intake._default_storage()


def request_conversion(
    *,
    source_variant: FileVariant,
    target_format: str,
    requested_by_user_id: int | None = None,
    request_options: Mapping[str, Any] | None = None,
) -> ProcessingJob:
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
    _dispatch_conversion_job(str(job.job_id))
    job.refresh_from_db()
    return job


def _dispatch_conversion_job(job_id: str) -> None:
    queue = get_queue()
    if queue is None:
        process_conversion_job(job_id)
        return
    queue.enqueue(process_conversion_job, job_id)


def process_conversion_job(job_id: str) -> None:
    try:
        conversion_job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except ProcessingJob.DoesNotExist:
        return
    if conversion_job.state == ProcessingJob.JobState.SUCCESS:
        return

    details = conversion_job.details if isinstance(conversion_job.details, dict) else {}
    source_variant = conversion_job.file_variant
    if source_variant is None:
        _fail_conversion_job(conversion_job, "missing source variant")
        return

    conversion_job.state = ProcessingJob.JobState.RUNNING
    conversion_job.started_at = timezone.now()
    conversion_job.save(update_fields=["state", "started_at"])

    try:
        route_payload = details.get("route")
        if not isinstance(route_payload, list) or not route_payload:
            raise ConversionError("conversion route missing")
        target_format = _normalize_format(details.get("target_format", ""))
        route = _deserialize_route(route_payload)
        output_variant = _execute_plan(
            source_variant=source_variant,
            target_format=target_format,
            route=route,
            request_options=details.get("request_options"),
        )
        conversion_job.state = ProcessingJob.JobState.SUCCESS
        conversion_job.finished_at = timezone.now()
        conversion_job.details = {
            **details,
            "output_variant_id": output_variant.id,
            "output_format": target_format,
            "error": "",
        }
        conversion_job.save(update_fields=["state", "finished_at", "details"])
    except Exception as exc:  # pragma: no cover - defensive path
        _fail_conversion_job(conversion_job, str(exc))


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
) -> FileVariant:
    storage = _default_storage()
    source_path = storage.resolve_location(source_variant.storage_identifier)
    if not source_path.exists():
        raise ConversionError("source artifact missing")

    request_options_map: dict[str, Any] = {}
    if isinstance(request_options, dict):
        request_options_map = dict(request_options)

    with tempfile.TemporaryDirectory(prefix="ebook-convert-") as workdir:
        current_path = source_path
        converter_chain: list[str] = []
        converter_versions: list[str] = []
        for index, step in enumerate(route, start=1):
            destination = Path(workdir) / f"step-{index}.{step.target_format}"
            converter = CONVERTER_REGISTRY.get(step.converter, CopierConverter())
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
            current_path = destination

        output_sha = intake.compute_sha256(str(current_path))
        output_size = current_path.stat().st_size
        existing = (
            FileVariant.objects.filter(
                book=source_variant.book,
                source_variant=source_variant,
                format=target_format,
                source_type=FileVariant.SourceType.CONVERTED,
                sha256=output_sha,
            )
            .select_related("book")
            .first()
        )
        if existing is not None:
            return existing

        output_name = f"{uuid.uuid4()}.{target_format}"
        storage_identifier = storage.generate_destination(
            f"books/{source_variant.book_id}/converted",
            output_name,
        )
        storage.store(str(current_path), storage_identifier)

        return FileVariant.objects.create(
            book=source_variant.book,
            format=target_format,
            mime_type=FORMAT_TO_MIME.get(target_format, "application/octet-stream"),
            original_filename=Path(storage_identifier).name,
            storage_location=source_variant.storage_location,
            storage_identifier=storage_identifier,
            size_bytes=output_size,
            sha256=output_sha,
            source_type=FileVariant.SourceType.CONVERTED,
            uploader=source_variant.uploader,
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
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )


def _fail_conversion_job(job: ProcessingJob, message: str) -> None:
    details = job.details if isinstance(job.details, dict) else {}
    if not isinstance(details, dict):
        details = {}
    details = {**details, "error": message}
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = details
    job.save(update_fields=["state", "finished_at", "details"])


def _run_conversion_command(
    command: list[str],
    check: bool = False,
    capture_output: bool = False,
) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"check": check}
    if capture_output:
        kwargs.update(
            {
                "stdout": subprocess.PIPE,
                "stderr": subprocess.PIPE,
                "text": True,
            },
        )
    else:
        kwargs.update(
            {
                "stdout": subprocess.DEVNULL,
                "stderr": subprocess.DEVNULL,
            },
        )
    return subprocess.run(command, **kwargs)


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
