"""OCR orchestration and external OCR service integration."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import time
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx
from django.conf import settings as django_settings
from django.utils import timezone
from pydantic import BaseModel, Field

from ebook_server.jobs import get_queue
from ebook_server.storage import LocalFilesystemStorage

from . import conversion, intake
from .models import Book, FileVariant, ProcessingJob, ScanJob


class OCRClientError(RuntimeError):
    """Raised for OCR service transport or protocol failures."""


class OCRMalformedResponseError(RuntimeError):
    """Raised when OCR service responses cannot be interpreted."""


class OCRJobError(RuntimeError):
    """Raised when OCR job payload or orchestration is invalid."""


class OCRJobRequest(BaseModel):
    """Envelope for a single OCR submission."""

    language: str | None = None
    engine: str | None = None
    model: str | None = None
    target_formats: list[str] = Field(default_factory=lambda: ["txt", "pdf"])
    options: dict[str, Any] = Field(default_factory=dict)


@dataclass(frozen=True)
class OCRArtifactDescriptor:
    """One OCR output artifact descriptor returned by the provider."""

    format: str
    filename: str | None = None
    mime_type: str | None = None
    confidence: float | None = None
    content: str | None = None
    content_url: str | None = None


@dataclass(frozen=True)
class OCRPollResult:
    """OCR status + optional output artifacts."""

    status: str
    artifacts: list[OCRArtifactDescriptor]
    engine: str | None = None
    model: str | None = None
    language: str | None = None
    confidence: float | None = None
    error: str | None = None


class OCRClientProtocol(Protocol):
    """Pluggable OCR provider contract."""

    name: str = "base"

    def submit_job(
        self,
        source_path: Path,
        source_format: str,
        source_mime: str,
        request: OCRJobRequest,
    ) -> str:
        """Submit source artifact and return remote job id."""

    def poll_job(self, remote_job_id: str) -> OCRPollResult:
        """Get current status for remote job id."""

    def download_artifact(self, remote_job_id: str, artifact: OCRArtifactDescriptor) -> bytes:
        """Download artifact bytes."""


class HttpxOCRClient:
    """HTTP OCR client abstraction backed by a remote OCR service."""

    name = "httpx"
    submit_endpoint = "/v1/ocr/jobs"
    status_endpoint = "/v1/ocr/jobs/{job_id}"

    def __init__(self, base_url: str | None = None) -> None:
        self.base_url = (base_url or _setting("BOOK_SCANNER_OCR_SERVICE_URL", "http://localhost:8020")).rstrip("/")
        self.timeout_seconds = float(_setting("BOOK_SCANNER_OCR_REQUEST_TIMEOUT", 30))
        self._client = httpx.Client(timeout=self.timeout_seconds)

    def submit_job(
        self,
        source_path: Path,
        source_format: str,
        source_mime: str,
        request: OCRJobRequest,
    ) -> str:
        payload = {
            "source_format": source_format,
            "target_formats": request.target_formats,
            "language": request.language,
            "engine": request.engine,
            "model": request.model,
            "options": request.options,
        }
        payload = _compact_payload(payload)
        files = {
            "file": (
                source_path.name,
                source_path.read_bytes(),
                source_mime or "application/octet-stream",
            ),
        }
        try:
            response = self._client.post(
                f"{self.base_url}{self.submit_endpoint}",
                files=files,
                data={"payload": json.dumps(payload)},
            )
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, OSError) as exc:
            raise OCRClientError(f"OCR submit failed: {exc}") from exc
        return _coerce_remote_job_id(raw)

    def poll_job(self, remote_job_id: str) -> OCRPollResult:
        try:
            response = self._client.get(
                f"{self.base_url}{self.status_endpoint.format(job_id=remote_job_id)}",
            )
            response.raise_for_status()
            raw = response.json()
        except (httpx.HTTPError, OSError) as exc:
            raise OCRClientError(f"OCR poll failed: {exc}") from exc
        return _coerce_poll_payload(raw)

    def download_artifact(self, remote_job_id: str, artifact: OCRArtifactDescriptor) -> bytes:
        if artifact.content:
            try:
                return base64.b64decode(artifact.content)
            except Exception as exc:
                raise OCRClientError("artifact content_base64 invalid") from exc
        if artifact.content_url:
            url = artifact.content_url
            if url.startswith("/"):
                url = f"{self.base_url}{url}"
            try:
                response = self._client.get(url)
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, OSError) as exc:
                raise OCRClientError(f"OCR artifact fetch failed: {exc}") from exc
        if remote_job_id and artifact.format:
            try:
                response = self._client.get(
                    f"{self.base_url}/v1/ocr/jobs/{remote_job_id}/artifacts/{artifact.format}",
                )
                response.raise_for_status()
                return response.content
            except (httpx.HTTPError, OSError) as exc:
                raise OCRClientError(f"OCR artifact fetch failed: {exc}") from exc
        raise OCRClientError("artifact missing content reference")


_OCR_CLIENT: OCRClientProtocol | None = None


def _setting(name: str, default: Any) -> Any:
    return getattr(django_settings, name, default)


def _default_ocr_storage() -> LocalFilesystemStorage:
    return intake._default_storage()


def _default_ocr_storage_location():
    return intake._default_storage_location()


def _get_ocr_client() -> OCRClientProtocol:
    global _OCR_CLIENT
    if _OCR_CLIENT is None:
        _OCR_CLIENT = HttpxOCRClient()
    return _OCR_CLIENT


def _compact_payload(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in raw.items()
        if value not in (None, "", [], {}, b"")
    }


def _coerce_target_formats(raw: Any) -> list[str]:
    if raw is None:
        return ["txt", "pdf"]
    if isinstance(raw, str):
        raw_values = [entry.strip() for entry in raw.split(",")]
    elif isinstance(raw, Sequence) and not isinstance(raw, (bytes, bytearray, str)):
        raw_values = [str(entry).strip() for entry in raw]
    else:
        raise OCRJobError("target_formats must be string or list-like")
    normalized: list[str] = []
    seen: set[str] = set()
    for raw_value in raw_values:
        if not raw_value:
            continue
        normalized_value = _normalize_output_format(raw_value)
        if normalized_value not in seen:
            seen.add(normalized_value)
            normalized.append(normalized_value)
    if not normalized:
        raise OCRJobError("no valid OCR target formats")
    return normalized


def _coerce_remote_job_id(payload: object) -> str:
    if not isinstance(payload, Mapping):
        raise OCRMalformedResponseError("submit payload is not a json object")
    remote = payload.get("job_id") or payload.get("id") or payload.get("task_id")
    if isinstance(remote, str) and remote.strip():
        return remote.strip()
    raise OCRMalformedResponseError("submit payload missing remote job id")


def _coerce_optional_float(raw: Any) -> float | None:
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


_POLL_ACCEPTED = {
    "accepted",
    "queued",
    "pending",
    "running",
    "processing",
    "in_progress",
    "started",
}
_POLL_SUCCEEDED = {
    "succeeded",
    "success",
    "completed",
    "done",
    "finished",
}
_POLL_FAILED = {
    "failed",
    "error",
    "cancelled",
    "canceled",
    "timeout",
    "rejected",
}


def _normalize_remote_status(raw: str) -> str:
    value = raw.strip().lower()
    if value in _POLL_ACCEPTED:
        return "running"
    if value in _POLL_SUCCEEDED:
        return "succeeded"
    if value in _POLL_FAILED:
        return "failed"
    return "malformed"


def _coerce_poll_payload(payload: object) -> OCRPollResult:
    if not isinstance(payload, Mapping):
        raise OCRMalformedResponseError("poll payload is not a json object")
    raw_status = payload.get("status") or payload.get("state") or payload.get("job_status")
    if not isinstance(raw_status, str):
        raise OCRMalformedResponseError("poll payload missing status")

    raw_artifacts = (
        payload.get("artifacts")
        or payload.get("outputs")
        or payload.get("results")
        or []
    )
    if isinstance(payload.get("result"), Mapping):
        nested = payload["result"]
        if isinstance(nested, Mapping):
            nested_artifacts = (
                nested.get("artifacts")
                or nested.get("outputs")
                or nested.get("results")
                or []
            )
            if nested_artifacts:
                raw_artifacts = nested_artifacts

    if not isinstance(raw_artifacts, list):
        raise OCRMalformedResponseError("poll payload artifacts malformed")

    artifacts: list[OCRArtifactDescriptor] = []
    for raw_artifact in raw_artifacts:
        if not isinstance(raw_artifact, Mapping):
            continue
        raw_format = raw_artifact.get("format") or raw_artifact.get("kind")
        if not isinstance(raw_format, str) or not raw_format.strip():
            continue
        artifacts.append(
            OCRArtifactDescriptor(
                format=raw_format,
                filename=raw_artifact.get("filename")
                if isinstance(raw_artifact.get("filename"), str)
                else None,
                mime_type=raw_artifact.get("mime_type")
                if isinstance(raw_artifact.get("mime_type"), str)
                else None,
                confidence=_coerce_optional_float(raw_artifact.get("confidence")),
                content=(
                    raw_artifact.get("content_base64")
                    if isinstance(raw_artifact.get("content_base64"), str)
                    else (
                        raw_artifact.get("content")
                        if isinstance(raw_artifact.get("content"), str)
                        else None
                    )
                ),
                content_url=(
                    raw_artifact.get("content_url")
                    if isinstance(raw_artifact.get("content_url"), str)
                    else (
                        raw_artifact.get("download_url")
                        if isinstance(raw_artifact.get("download_url"), str)
                        else None
                    )
                ),
            ),
        )

    return OCRPollResult(
        status=str(raw_status).lower(),
        artifacts=artifacts,
        engine=payload.get("engine") if isinstance(payload.get("engine"), str) else None,
        model=payload.get("model") if isinstance(payload.get("model"), str) else None,
        language=payload.get("language") if isinstance(payload.get("language"), str) else None,
        confidence=_coerce_optional_float(payload.get("confidence")),
        error=payload.get("error") if isinstance(payload.get("error"), str) else None,
    )


def _normalize_output_format(value: str) -> str:
    candidate = value.strip().lower()
    alias = {
        "text": "txt",
        "plain_text": "txt",
        "plain": "txt",
        "structured_text": "txt",
        "searchable_pdf": "pdf",
        "ocr_pdf": "pdf",
        "hocr": "html",
        "alto": "html",
    }
    canonical = alias.get(candidate, candidate)
    if canonical in {
        "txt",
        "pdf",
        "html",
        "epub",
        "mobi",
        "fb2",
        "djvu",
        "cbz",
        "cbr",
        "png",
        "tiff",
    }:
        return canonical
    if canonical == "json":
        return "txt"
    raise OCRJobError(f"unsupported OCR target format: {value}")


def _derive_source_extension(*, mime_type: str, fallback: str = ".bin") -> str:
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/tiff": ".tiff",
        "image/tif": ".tif",
        "application/pdf": ".pdf",
    }
    return mapping.get(mime_type.lower(), fallback)


def _poll_limit() -> int:
    return max(1, int(_setting("BOOK_SCANNER_OCR_MAX_ATTEMPTS", 40)))


def _poll_interval() -> float:
    return float(_setting("BOOK_SCANNER_OCR_POLL_INTERVAL_SECONDS", 0.2))


def request_ocr_from_variant(
    *,
    source_variant: FileVariant,
    requested_by_user_id: int | None = None,
    target_formats: Any = None,
    request_language: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    request_options: Mapping[str, Any] | None = None,
) -> ProcessingJob:
    request = OCRJobRequest(
        language=request_language,
        engine=engine,
        model=model,
        target_formats=_coerce_target_formats(target_formats),
        options=dict(request_options or {}),
    )
    if source_variant.book_id is None:
        raise OCRJobError("source variant is not linked to a book")

    details = {
        "source_type": "variant",
        "source_variant_id": source_variant.id,
        "book_id": source_variant.book_id,
        "target_formats": request.target_formats,
        "request_language": request_language,
        "engine": engine,
        "model": model,
        "request_options": dict(request.options),
        "requested_by_user_id": requested_by_user_id,
    }
    job = ProcessingJob.objects.create(
        file_variant=source_variant,
        job_type=ProcessingJob.JobType.OCR,
        state=ProcessingJob.JobState.QUEUED,
        details=details,
    )
    _dispatch_ocr_job(str(job.job_id))
    job.refresh_from_db()
    return job


def request_ocr_from_scan_job(
    *,
    scan_job: ScanJob,
    requested_by_user_id: int | None = None,
    target_formats: Any = None,
    request_language: str | None = None,
    engine: str | None = None,
    model: str | None = None,
    request_options: Mapping[str, Any] | None = None,
) -> ProcessingJob:
    request = OCRJobRequest(
        language=request_language,
        engine=engine,
        model=model,
        target_formats=_coerce_target_formats(target_formats),
        options=dict(request_options or {}),
    )
    if scan_job.book_id is None:
        raise OCRJobError("scan job is not linked to a book")

    details = {
        "source_type": "scan_job",
        "scan_job_id": str(scan_job.id),
        "book_id": scan_job.book_id,
        "target_formats": request.target_formats,
        "request_language": request_language,
        "engine": engine,
        "model": model,
        "request_options": dict(request.options),
        "requested_by_user_id": requested_by_user_id,
    }
    job = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.OCR,
        state=ProcessingJob.JobState.QUEUED,
        details=details,
    )
    _dispatch_ocr_job(str(job.job_id))
    job.refresh_from_db()
    return job


def _dispatch_ocr_job(job_id: str) -> None:
    queue = get_queue()
    if queue is None:
        process_ocr_job(job_id)
        return
    queue.enqueue(process_ocr_job, job_id)


def process_ocr_job(job_id: str) -> None:
    try:
        ocr_job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except ProcessingJob.DoesNotExist:
        return
    if ocr_job.job_type != ProcessingJob.JobType.OCR:
        return
    if ocr_job.state == ProcessingJob.JobState.SUCCESS:
        return

    details = _ensure_dict(ocr_job.details)
    ocr_job.state = ProcessingJob.JobState.RUNNING
    ocr_job.started_at = timezone.now()
    ocr_job.save(update_fields=["state", "started_at"])

    cleanup_paths: list[Path] = []
    try:
        source_path, source_format, source_mime, book, source_variant = _resolve_source_artifact(
            ocr_job,
            details=details,
        )
        if details.get("source_type") == "scan_job":
            cleanup_paths.append(source_path)
        request = OCRJobRequest(
            language=_coerce_str(details.get("request_language")),
            engine=_coerce_str(details.get("engine")),
            model=_coerce_str(details.get("model")),
            target_formats=_coerce_target_formats(details.get("target_formats")),
            options=_coerce_mapping(details.get("request_options", {})),
        )
        remote_job_id, request_artifacts = _submit_and_poll(
            source_path=source_path,
            source_format=source_format,
            source_mime=source_mime,
            request=request,
        )

        output_variants: list[dict[str, Any]] = []
        if request_artifacts:
            for artifact in request_artifacts:
                output_variants.extend(
                    _materialize_ocr_artifact(
                        source_book=book,
                        source_variant=source_variant,
                        artifact=artifact,
                        remote_job_id=remote_job_id,
                        request=request,
                    ),
                )

        details = {
            **details,
            "remote_job_id": remote_job_id,
            "remote_status": "succeeded",
            "output_variants": output_variants,
            "error": "",
        }
        ocr_job.state = ProcessingJob.JobState.SUCCESS
        ocr_job.finished_at = timezone.now()
        ocr_job.details = details
        ocr_job.save(update_fields=["state", "finished_at", "details"])
    except Exception as exc:  # pragma: no cover - defensive
        _fail_ocr_job(ocr_job, str(exc))
    finally:
        for cleanup_path in cleanup_paths:
            cleanup_path.unlink(missing_ok=True)


def _submit_and_poll(
    *,
    source_path: Path,
    source_format: str,
    source_mime: str,
    request: OCRJobRequest,
) -> tuple[str, list[OCRArtifactDescriptor] | None]:
    client = _get_ocr_client()
    remote_job_id = client.submit_job(
        source_path=source_path,
        source_format=source_format,
        source_mime=source_mime,
        request=request,
    )
    for _ in range(_poll_limit()):
        poll = client.poll_job(remote_job_id)
        remote_status = _normalize_remote_status(poll.status)
        if remote_status == "running":
            time.sleep(_poll_interval())
            continue
        if remote_status == "failed":
            raise OCRClientError(poll.error or "ocr failed")
        if remote_status == "succeeded":
            return remote_job_id, poll.artifacts
        raise OCRClientError(f"unexpected OCR status: {poll.status}")
    raise OCRClientError("OCR polling timed out")


def _resolve_source_artifact(
    ocr_job: ProcessingJob,
    *,
    details: dict[str, Any],
) -> tuple[Path, str, str, Book, FileVariant | None]:
    source_type = str(details.get("source_type", "variant"))
    book_id_raw = details.get("book_id")
    if not isinstance(book_id_raw, int):
        raise OCRJobError("book_id missing")
    book = Book.objects.get(pk=book_id_raw)

    if source_type == "scan_job":
        scan_job_id = details.get("scan_job_id")
        if not isinstance(scan_job_id, str):
            raise OCRJobError("scan_job_id missing")
        scan_job = ScanJob.objects.prefetch_related("pages", "pages__versions").get(pk=scan_job_id)
        artifact = _zip_scan_job_pages(scan_job)
        return artifact, "zip", "application/zip", book, None

    source_variant_id = details.get("source_variant_id")
    if not isinstance(source_variant_id, int):
        raise OCRJobError("source_variant_id missing")
    if ocr_job.file_variant_id is None:
        source_variant = FileVariant.objects.select_related("book").get(pk=source_variant_id)
    else:
        source_variant = ocr_job.file_variant
    if source_variant is None:
        raise OCRJobError("source variant missing")
    if source_variant.storage_identifier is None:
        raise OCRJobError("source artifact storage missing")
    source_path = _default_ocr_storage().resolve_location(source_variant.storage_identifier)
    if not source_path.exists():
        raise OCRJobError("source artifact missing")
    if source_variant.book is not None:
        book = source_variant.book
    return source_path, source_variant.format, source_variant.mime_type, book, source_variant


def _zip_scan_job_pages(scan_job: ScanJob) -> Path:
    pages = list(
        scan_job.pages.prefetch_related("versions").order_by("logical_sequence_number", "side"),
    )
    if not pages:
        raise OCRJobError("scan job has no pages")

    with tempfile.NamedTemporaryFile(
        prefix="ocr-scan-pages-",
        suffix=".zip",
        delete=False,
    ) as archive:
        archive_path = Path(archive.name)
    try:
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
            written = 0
            for page in pages:
                version = page.active_version()
                if version is None:
                    continue
                storage = _default_ocr_storage()
                try:
                    with storage.open(version.storage_identifier) as stream:
                        content = stream.read()
                except FileNotFoundError:
                    continue
                extension = _derive_source_extension(
                    mime_type=version.mime_type,
                    fallback=Path(
                        version.original_filename or "page.bin",
                    ).suffix.lower()
                    or ".bin",
                )
                filename = f"{page.logical_sequence_number:04d}-{page.side}{extension}"
                zipped.writestr(filename, content)
                written += 1
        if written == 0:
            archive_path.unlink(missing_ok=True)
            raise OCRJobError("scan job has no active page versions")
        return archive_path
    except Exception:
        archive_path.unlink(missing_ok=True)
        raise


def _materialize_ocr_artifact(
    *,
    source_book: Book,
    source_variant: FileVariant | None,
    artifact: OCRArtifactDescriptor,
    remote_job_id: str,
    request: OCRJobRequest,
) -> list[dict[str, Any]]:
    request_format = _normalize_output_format(artifact.format)
    if request_format not in request.target_formats:
        return []

    content = _get_ocr_client().download_artifact(remote_job_id, artifact)
    if not content:
        return []
    output_sha = hashlib.sha256(content).hexdigest()

    variant_query = FileVariant.objects.filter(
        book=source_book,
        format=request_format,
        source_type=FileVariant.SourceType.OCR,
        sha256=output_sha,
    )
    if source_variant is not None:
        variant_query = variant_query.filter(source_variant=source_variant)
    duplicate = variant_query.first()
    if duplicate is not None:
        return [
            {
                "format": request_format,
                "file_variant_id": duplicate.id,
                "filename": duplicate.original_filename,
                "bytes": duplicate.size_bytes,
            },
        ]

    safe_name = Path(
        artifact.filename or f"{source_book.id}-ocr.{request_format}",
    ).name
    with tempfile.NamedTemporaryFile(
        prefix="ocr-artifact-",
        suffix=f".{request_format}",
        delete=False,
    ) as temp:
        temp.write(content)
        temp.flush()
        temp_path = Path(temp.name)

    storage = _default_ocr_storage()
    storage_identifier = storage.generate_destination(
        f"books/{source_book.id}/ocr",
        safe_name,
    )
    try:
        storage.store(str(temp_path), storage_identifier)
    finally:
        temp_path.unlink(missing_ok=True)

    destination = storage.resolve_location(storage_identifier)
    variant = FileVariant.objects.create(
        book=source_book,
        format=request_format,
        mime_type=conversion.FORMAT_TO_MIME.get(request_format, "application/octet-stream"),
        original_filename=safe_name,
        storage_location=_default_ocr_storage_location(),
        storage_identifier=storage_identifier,
        size_bytes=destination.stat().st_size,
        sha256=output_sha,
        source_type=FileVariant.SourceType.OCR,
        uploader=source_variant.uploader if source_variant is not None else None,
        imported_at=timezone.now(),
        source_variant=source_variant,
        converter_tool=request.engine or "ocr",
        converter_version=request.model or "unknown",
        converter_options={
            "engine": request.engine,
            "model": request.model,
            "language": request.language,
            "options": request.options,
            "targets": request.target_formats,
            "remote_job_id": remote_job_id,
            "provenance": {
                "ocr_target_formats": request.target_formats,
                "confidence": artifact.confidence,
            },
        },
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )

    return [
        {
            "format": request_format,
            "file_variant_id": variant.id,
            "filename": variant.original_filename,
            "bytes": variant.size_bytes,
        },
    ]


def _coerce_str(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _coerce_mapping(raw: object) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    return {}


def _ensure_dict(raw: object) -> dict[str, Any]:
    return dict(raw) if isinstance(raw, Mapping) else {}


def _fail_ocr_job(job: ProcessingJob, message: str) -> None:
    details = _ensure_dict(job.details)
    details["error"] = message
    details["remote_status"] = "failed"
    job.state = ProcessingJob.JobState.FAILED
    job.finished_at = timezone.now()
    job.details = details
    job.save(update_fields=["state", "finished_at", "details"])


def ocr_job_payload(job: ProcessingJob) -> dict[str, Any]:
    details = _ensure_dict(job.details)
    return {
        "job_id": str(job.job_id),
        "state": job.state,
        "source_variant_id": details.get("source_variant_id"),
        "scan_job_id": details.get("scan_job_id"),
        "remote_job_id": details.get("remote_job_id"),
        "remote_status": details.get("remote_status"),
        "target_formats": details.get("target_formats", []),
        "error": details.get("error", ""),
        "output_variants": details.get("output_variants", []),
    }
