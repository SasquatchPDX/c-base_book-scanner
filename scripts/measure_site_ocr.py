"""Score member-downloaded OCR text from completed, one-page staging scan jobs."""

from __future__ import annotations

import argparse
import json
import os
import sys
import unicodedata
from collections import defaultdict
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import UUID

import httpx
from pydantic import BaseModel, Field
from rapidfuzz.distance import Levenshtein


class Limits(BaseModel):
    cer: float = Field(ge=0, allow_inf_nan=False)
    wer: float = Field(ge=0, allow_inf_nan=False)


class Page(BaseModel):
    finalization_id: UUID
    image_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    page_class: Literal["flat", "curved"]
    reference: Path
    language: str = Field(min_length=1)


class Corpus(BaseModel):
    source_permission: str = Field(min_length=1)
    engine: str = Field(min_length=1)
    pages: Annotated[list[Page], Field(min_length=1)]
    # One pair of operator-agreed limits per page class, omitted for calibration.
    thresholds: dict[str, Limits] | None = None


def counts(reference: str, recognized: str) -> list[int]:
    def normalized(text: str) -> str:
        return " ".join(unicodedata.normalize("NFC", text).split())

    expected, result = normalized(reference), normalized(recognized)
    if not expected:
        raise ValueError("A transcription is empty.")
    return [
        Levenshtein.distance(expected, result),
        len(expected),
        Levenshtein.distance(expected.split(), result.split()),
        len(expected.split()),
    ]


def rates(values: list[int]) -> dict[str, float]:
    return {"cer": values[0] / values[1], "wer": values[2] / values[3]}


def downloaded_text(client: httpx.Client, page: Page, engine: str) -> tuple[str, dict[str, str]]:
    def get(path: str) -> dict[str, Any]:
        return dict(client.get(path).raise_for_status().json())

    finish = get(f"/scan-finalizations/{page.finalization_id}/")
    if finish["state"] != "success" or finish["allow_raw_pages"] is not False:
        raise ValueError("Expected a successful finalization with processed pages required.")
    scan = get(f"/scan/jobs/{finish['scan_job_id']}/")
    if (
        scan["state"] != "complete"
        or len(scan["pages"]) != 1
        or scan["pages"][0]["active_version"]["checksum_sha256"] != page.image_sha256
    ):
        raise ValueError("The completed scan does not match the labelled source image.")
    job = get(f"/ocr/{finish['ocr_job_id']}/")
    if (
        job["state"] != "success"
        or not job["remote_job_id"]
        or job["source_variant_id"] != finish["cleaned_variant_id"]
    ):
        raise ValueError("The OCR job did not process this cleaned variant.")
    reported = job.get("reported", {})
    if (
        reported.get("engine") != engine
        or not reported.get("model")
        or reported.get("language") != page.language
        or engine == "synthetic-contract-ocr"
    ):
        raise ValueError("The OCR server did not report the expected engine, model and language.")
    [variant] = [item for item in job["output_variants"] if item["format"] == "txt"]
    book = get(f"/library/books/{scan['book_id']}/")
    visible = {v["id"]: v["source_type"] for v in book["variants"]}
    if (
        visible.get(finish["cleaned_variant_id"]) != "cleaned"
        or visible.get(variant["file_variant_id"]) != "ocr"
    ):
        raise ValueError("The measured variants are no longer current member downloads.")
    download = client.get(
        f"/library/books/{scan['book_id']}/download/",
        params={"variant_id": variant["file_variant_id"]},
    ).raise_for_status()
    if len(download.content) != variant["bytes"]:
        raise ValueError("Incomplete OCR download.")
    return download.content.decode("utf-8-sig"), reported


def measure(client: httpx.Client, corpus: Corpus, root: Path) -> dict[str, Any]:
    pages = []
    groups: dict[tuple[str, str, str], list[int]] = defaultdict(lambda: [0, 0, 0, 0])
    if len({page.image_sha256 for page in corpus.pages}) != len(corpus.pages):
        raise ValueError("Each source page must occur once in the corpus.")
    if {page.page_class for page in corpus.pages} != {"flat", "curved"}:
        raise ValueError("Include flat and curved pages in the site quality corpus.")
    failed = False
    for page in corpus.pages:
        text, provenance = downloaded_text(client, page, corpus.engine)
        observed = counts((root / page.reference).read_text("utf-8-sig"), text)
        scores = rates(observed)
        key = (page.page_class, page.language, provenance["model"])
        groups[key] = [a + b for a, b in zip(groups[key], observed, strict=True)]
        if corpus.thresholds is not None:
            if page.page_class not in corpus.thresholds:
                raise ValueError("Supply agreed limits for each page class.")
            limit = corpus.thresholds[page.page_class]
            failed |= scores["cer"] > limit.cer or scores["wer"] > limit.wer
        pages.append(
            {**page.model_dump(mode="json", exclude={"reference"}), **provenance, **scores}
        )
    return {
        "status": "failed" if failed else "calibration" if corpus.thresholds is None else "passed",
        "pages": pages,
        "groups": [
            {
                "page_class": kind,
                "language": language,
                "model": model,
                "engine": corpus.engine,
                **rates(values),
            }
            for (kind, language, model), values in sorted(groups.items())
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    args = parser.parse_args()
    target, session = os.getenv("OCR_ACCEPTANCE_ORIGIN"), os.getenv("OCR_ACCEPTANCE_SESSION")
    if not target or not session:
        print(
            json.dumps(
                {"status": "unavailable", "reason": "staging origin or member session absent"}
            )
        )
        return 77
    try:
        url = httpx.URL(target)
        if (
            url.scheme != "https"
            or not url.host
            or url.userinfo
            or url.path != "/"
            or url.query
            or url.fragment
        ):
            raise ValueError("Specify an HTTPS staging origin.")
        corpus = Corpus.model_validate_json(args.manifest.read_text("utf-8"))
        with httpx.Client(base_url=url, cookies={"sessionid": session}, timeout=30) as client:
            report = measure(client, corpus, args.manifest.parent)
    except httpx.TransportError as exc:
        print(json.dumps({"status": "unavailable", "reason": type(exc).__name__}))
        return 77
    except (KeyError, OSError, ValueError, httpx.HTTPStatusError) as exc:
        print(json.dumps({"status": "failed", "reason": str(exc)}))
        return 1
    print(json.dumps(report, indent=2))
    return {"passed": 0, "failed": 1, "calibration": 3}[report["status"]]


if __name__ == "__main__":
    sys.exit(main())
