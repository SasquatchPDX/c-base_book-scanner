"""Deterministic external-service fixtures used by browser acceptance tests."""

from __future__ import annotations

import base64
import hashlib
import json
from io import BytesIO
from typing import Any

from pypdf import PdfWriter
from starlette.applications import Starlette
from starlette.datastructures import UploadFile
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route


class BrowserMetadataProvider:
    """Stable provider boundary for the full physical-book browser scenario."""

    provider_id = "browser-acceptance"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        del isbn, folder_hint
        resolved_title = (title or "Browser Acceptance Book").strip()
        resolved_author = (author or "Acceptance Researcher").strip()
        provider_ref = hashlib.sha256(resolved_title.encode()).hexdigest()[:16]
        return [
            {
                "key": f"/works/{provider_ref}",
                "title": resolved_title,
                "author_name": [resolved_author],
                "publisher": ["Hackerspace Acceptance Press"],
                "first_publish_year": 2026,
                "language": ["eng"],
            },
        ]


def _searchable_pdf_fixture() -> bytes:
    output = BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_metadata(
        {
            "/Title": "Browser Physical Pipeline OCR",
            "/Producer": "book-scanner browser OCR fixture",
        },
    )
    writer.write(output)
    return output.getvalue()


_OCR_JOBS: set[str] = set()
_OCR_TEXT = b"Browser physical pipeline OCR text\n"
_OCR_PDF = _searchable_pdf_fixture()


async def submit_ocr_job(request: Request) -> JSONResponse:
    form = await request.form()
    upload = form.get("file")
    raw_payload = form.get("payload")
    if not isinstance(upload, UploadFile) or not isinstance(raw_payload, str):
        return JSONResponse({"error": "file and payload are required"}, status_code=400)

    source = await upload.read()
    try:
        payload = json.loads(raw_payload)
    except json.JSONDecodeError:
        return JSONResponse({"error": "payload must be JSON"}, status_code=400)
    if not source or not isinstance(payload, dict):
        return JSONResponse({"error": "source and payload are required"}, status_code=400)
    if payload.get("source_format") != "pdf":
        return JSONResponse({"error": "expected a cleaned PDF source"}, status_code=400)
    if payload.get("target_formats") != ["txt", "pdf"]:
        return JSONResponse({"error": "expected TXT and PDF targets"}, status_code=400)

    job_id = f"browser-{hashlib.sha256(source).hexdigest()[:20]}"
    _OCR_JOBS.add(job_id)
    return JSONResponse({"job_id": job_id}, status_code=202)


async def get_ocr_job(request: Request) -> JSONResponse:
    job_id = request.path_params["job_id"]
    if job_id not in _OCR_JOBS:
        return JSONResponse({"error": "job not found"}, status_code=404)
    return JSONResponse(
        {
            "status": "succeeded",
            "engine": "synthetic-contract-ocr",
            "model": "browser-fixture-v1",
            "language": "eng",
            "confidence": 0.99,
            "artifacts": [
                {
                    "format": "txt",
                    "filename": "browser-physical-pipeline-ocr.txt",
                    "mime_type": "text/plain",
                    "confidence": 0.99,
                    "content_base64": base64.b64encode(_OCR_TEXT).decode("ascii"),
                },
                {
                    "format": "pdf",
                    "filename": "browser-physical-pipeline-searchable.pdf",
                    "mime_type": "application/pdf",
                    "confidence": 0.99,
                    "content_base64": base64.b64encode(_OCR_PDF).decode("ascii"),
                },
            ],
        },
    )


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


ocr_app = Starlette(
    routes=[
        Route("/healthz", health, methods=["GET"]),
        Route("/v1/ocr/jobs", submit_ocr_job, methods=["POST"]),
        Route("/v1/ocr/jobs/{job_id}", get_ocr_job, methods=["GET"]),
    ],
)
