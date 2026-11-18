from pathlib import Path
from typing import Literal
from uuid import UUID

import httpx
import pytest

from scripts.measure_site_ocr import Corpus, Limits, Page, counts, measure


def test_downloaded_quality_reports_degradation_and_calibration(tmp_path: Path) -> None:
    classes: tuple[Literal["flat", "curved"], ...] = ("flat", "curved")
    pages = [
        Page(
            finalization_id=UUID(int=i),
            image_sha256=str(i) * 64,
            page_class=kind,
            reference=Path(f"{i}.txt"),
            language="deu",
        )
        for i, kind in enumerate(classes, start=1)
    ]
    for page in pages:
        (tmp_path / page.reference).write_text("Eine Seite", encoding="utf-8")
    corpus = Corpus(source_permission="test fixtures", engine="test-engine", pages=pages)

    def fixture(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/scan-finalizations/"):
            index = int(UUID(path.split("/")[2]))
            payload = {
                "state": "success",
                "scan_job_id": index,
                "cleaned_variant_id": 5,
                "ocr_job_id": index,
                "allow_raw_pages": False,
            }
        elif path.startswith("/scan/jobs/"):
            index = int(path.split("/")[3])
            payload = {
                "state": "complete",
                "book_id": index,
                "pages": [{"active_version": {"checksum_sha256": str(index) * 64}}],
            }
        elif path.startswith("/ocr/"):
            index = int(path.split("/")[2])
            payload = {
                "state": "success",
                "remote_job_id": "done",
                "source_variant_id": 5,
                "reported": {"engine": "test-engine", "model": "v1", "language": "deu"},
                "output_variants": [{"format": "txt", "file_variant_id": 7, "bytes": 10}],
            }
        elif path.endswith("/download/"):
            assert request.url.params["variant_id"] == "7"
            return httpx.Response(200, content=b"Eine Seite" if "/1/" in path else b"xxxxxxxxxx")
        else:
            payload = {
                "variants": [{"id": 5, "source_type": "cleaned"}, {"id": 7, "source_type": "ocr"}]
            }
        return httpx.Response(200, json=payload)

    with httpx.Client(
        base_url="https://stage.example", transport=httpx.MockTransport(fixture)
    ) as client:
        assert measure(client, corpus, tmp_path)["status"] == "calibration"
        corpus.thresholds = {kind: Limits(cer=0.1, wer=0.1) for kind in ["flat", "curved"]}
        report = measure(client, corpus, tmp_path)
        assert report["status"] == "failed"
        assert [page["wer"] for page in report["pages"]] == [0, 1]
        assert len(report["groups"]) == 2
        corpus.engine = "another-engine"
        with pytest.raises(ValueError, match="expected engine"):
            measure(client, corpus, tmp_path)


def test_quality_normalizes_unicode_and_spacing_but_preserves_case() -> None:
    assert counts("Café\n  noir", "Cafe\u0301 noir") == [0, 9, 0, 2]
    assert counts("Buch", "buch") == [1, 4, 1, 1]
