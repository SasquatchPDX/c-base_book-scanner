import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import httpx
import pytest
from pydantic import ValidationError

from scripts.measure_site_ocr import (
    Corpus,
    Limits,
    Page,
    SiteUnavailable,
    counts,
    main,
    measure,
)

ORIGIN = "https://stage.example/"
Handler = Callable[[httpx.Request], httpx.Response]


def _corpus(tmp_path: Path, **extra: Any) -> Corpus:
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
    return Corpus(source_permission="test fixtures", engine="test-engine", pages=pages, **extra)


def _staging(
    *,
    degraded: bool = True,
    ocr_job: dict[str, Any] | None = None,
    finalization_state: str = "success",
) -> Handler:
    """Portal double: page 1 OCRs perfectly, page 2 returns garbage when ``degraded``."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/scan-finalizations/"):
            index = int(UUID(path.split("/")[2]))
            payload: dict[str, Any] = {
                "state": finalization_state,
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
            payload = ocr_job or {
                "state": "success",
                "remote_job_id": "done",
                "source_variant_id": 5,
                "reported": {"engine": "test-engine", "model": "v1", "language": "deu"},
                "output_variants": [{"format": "txt", "file_variant_id": 7, "bytes": 10}],
            }
        elif path.endswith("/download/"):
            assert request.url.params["variant_id"] == "7"
            good = "/1/" in path or not degraded
            return httpx.Response(200, content=b"Eine Seite" if good else b"xxxxxxxxxx")
        else:
            payload = {
                "variants": [{"id": 5, "source_type": "cleaned"}, {"id": 7, "source_type": "ocr"}]
            }
        return httpx.Response(200, json=payload)

    return handler


def _client(handler: Handler) -> httpx.Client:
    return httpx.Client(base_url=ORIGIN, transport=httpx.MockTransport(handler))


def test_downloaded_quality_reports_degradation_and_calibration(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    with _client(_staging()) as client:
        assert measure(client, corpus, tmp_path)["status"] == "calibration"
        corpus.thresholds = {kind: Limits(cer=0.1, wer=0.1) for kind in ["flat", "curved"]}
        report = measure(client, corpus, tmp_path)
        assert report["status"] == "failed"
        assert [page["wer"] for page in report["pages"]] == [0, 1]
        assert [page["cer"] for page in report["pages"]] == [0, 1]
        assert [
            (g["page_class"], g["engine"], g["model"], g["language"]) for g in report["groups"]
        ] == [
            ("curved", "test-engine", "v1", "deu"),
            ("flat", "test-engine", "v1", "deu"),
        ]
        corpus.engine = "another-engine"
        with pytest.raises(ValueError, match="expected engine"):
            measure(client, corpus, tmp_path)


def test_clean_downloads_pass_configured_thresholds(tmp_path: Path) -> None:
    corpus = _corpus(
        tmp_path, thresholds={"flat": Limits(cer=0, wer=0), "curved": Limits(cer=0, wer=0)}
    )
    with _client(_staging(degraded=False)) as client:
        assert measure(client, corpus, tmp_path)["status"] == "passed"


def test_each_page_class_needs_its_own_threshold(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path, thresholds={"flat": Limits(cer=0.1, wer=0.1)})
    with _client(_staging()) as client, pytest.raises(ValueError, match="each page class"):
        measure(client, corpus, tmp_path)


@pytest.mark.parametrize("bad", [-0.1, float("inf"), float("nan")])
def test_threshold_limits_reject_nonsense(bad: float) -> None:
    with pytest.raises(ValidationError):
        Limits(cer=bad, wer=0.1)


def test_contract_fixture_engine_is_never_scored(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path)
    corpus.engine = "synthetic-contract-ocr"
    job = {
        "state": "success",
        "remote_job_id": "done",
        "source_variant_id": 5,
        "reported": {"engine": "synthetic-contract-ocr", "model": "v1", "language": "deu"},
        "output_variants": [{"format": "txt", "file_variant_id": 7, "bytes": 10}],
    }
    with _client(_staging(ocr_job=job)) as client, pytest.raises(ValueError, match="engine"):
        measure(client, corpus, tmp_path)


@pytest.mark.parametrize(
    ("ocr_job", "finalization_state"),
    [
        ({"state": "queued"}, "success"),
        ({"state": "running"}, "success"),
        (
            {"state": "failed", "service_unavailable": True, "error": "OCR submit failed: refused"},
            "success",
        ),
        (None, "running"),
    ],
)
def test_unfinished_or_unreachable_ocr_is_unavailable(
    tmp_path: Path, ocr_job: dict[str, Any] | None, finalization_state: str
) -> None:
    with (
        _client(_staging(ocr_job=ocr_job, finalization_state=finalization_state)) as client,
        pytest.raises(SiteUnavailable),
    ):
        measure(client, _corpus(tmp_path), tmp_path)


def test_ocr_server_error_result_is_a_failure_not_unavailable(tmp_path: Path) -> None:
    job = {"state": "failed", "service_unavailable": False, "error": "model crashed"}
    with _client(_staging(ocr_job=job)) as client, pytest.raises(ValueError, match="cleaned"):
        measure(client, _corpus(tmp_path), tmp_path)


def test_quality_normalizes_unicode_and_spacing_but_preserves_case() -> None:
    assert counts("Café\n  noir", "Café noir") == [0, 9, 0, 2]
    assert counts("Buch", "buch") == [1, 4, 1, 1]


def test_error_counts_are_character_and_word_edit_distances() -> None:
    # One substituted character in one of two words; one dropped word of three.
    assert counts("Das Buch", "Das Bach") == [1, 8, 1, 2]
    assert counts("ein kleines Buch", "ein Buch") == [8, 16, 1, 3]
    with pytest.raises(ValueError, match="empty"):
        counts("  \n", "anything")


def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    corpus: Corpus | None,
    transport: httpx.BaseTransport | None,
    origin: str = ORIGIN,
) -> tuple[int, dict[str, Any]]:
    manifest = tmp_path / "corpus.json"
    manifest.write_text(
        (corpus or _corpus(tmp_path)).model_dump_json(),
        encoding="utf-8",
    )
    monkeypatch.setenv("OCR_ACCEPTANCE_ORIGIN", origin)
    monkeypatch.setenv("OCR_ACCEPTANCE_SESSION", "session")
    code = main([str(manifest)], transport=transport)
    return code, json.loads(capsys.readouterr().out)


def test_cli_reports_absent_staging_as_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("OCR_ACCEPTANCE_ORIGIN", raising=False)
    monkeypatch.delenv("OCR_ACCEPTANCE_SESSION", raising=False)
    assert main([str(tmp_path / "corpus.json")]) == 77
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def _refused(request: httpx.Request) -> httpx.Response:
    raise httpx.ConnectError("connection refused", request=request)


@pytest.mark.parametrize(
    ("handler", "reason"),
    [
        (_refused, "ConnectError"),
        (lambda request: httpx.Response(503), "HTTP 503"),
        (_staging(ocr_job={"state": "running"}), "has not finished"),
        (
            _staging(ocr_job={"state": "failed", "service_unavailable": True, "error": "refused"}),
            "OCR server unreachable",
        ),
    ],
)
def test_cli_reports_unreachable_site_hardware_as_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    handler: Handler,
    reason: str,
) -> None:
    code, report = _run(tmp_path, monkeypatch, capsys, None, httpx.MockTransport(handler))
    assert (code, report["status"]) == (77, "unavailable")
    assert reason in report["reason"]


def test_cli_exit_codes_for_graded_and_calibration_runs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    limits = {"flat": Limits(cer=0.05, wer=0.05), "curved": Limits(cer=0.05, wer=0.05)}
    degraded = httpx.MockTransport(_staging())
    clean = httpx.MockTransport(_staging(degraded=False))

    code, report = _run(tmp_path, monkeypatch, capsys, None, degraded)
    assert (code, report["status"]) == (3, "calibration")
    code, report = _run(
        tmp_path, monkeypatch, capsys, _corpus(tmp_path, thresholds=limits), degraded
    )
    assert (code, report["status"]) == (1, "failed")
    code, report = _run(tmp_path, monkeypatch, capsys, _corpus(tmp_path, thresholds=limits), clean)
    assert (code, report["status"]) == (0, "passed")


@pytest.mark.parametrize(
    "origin",
    ["http://stage.example/", "https://stage.example/api/", "https://user:pw@stage.example/"],
)
def test_cli_requires_a_bare_https_origin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    origin: str,
) -> None:
    transport = httpx.MockTransport(_staging(degraded=False))
    code, report = _run(tmp_path, monkeypatch, capsys, None, transport, origin=origin)
    assert (code, report["status"]) == (1, "failed")


def test_cli_portal_errors_fail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(403))
    code, report = _run(tmp_path, monkeypatch, capsys, None, transport)
    assert (code, report["status"]) == (1, "failed")
