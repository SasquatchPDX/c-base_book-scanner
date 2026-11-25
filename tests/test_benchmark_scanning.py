"""The ScanTailor-Web benchmark (#195 section 8.3) keeps working as stages change."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

from ebook_server.core import scantailor

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_scanning.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("benchmark_scanning", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_benchmark_runs_every_stage_in_pipeline_order(tmp_path: Path) -> None:
    bench = _load()
    pages = bench._synthetic_pages(1, tmp_path, 620, 877)
    raw = bench.run(pages, tmp_path)
    assert raw["failures"] == []
    summary = bench.summarise(raw)
    assert list(summary) == ["page_image", *scantailor.stage_order()]
    for row in summary.values():
        assert row["pages"] == 1
        assert set(row["phases_mean_ms"]) == set(bench.PHASES)
        assert sum(row["phases_mean_ms"].values()) <= row["mean_ms"] + 1e-6


def test_benchmark_flags_regressions_beyond_tolerance(tmp_path: Path) -> None:
    bench = _load()
    baseline = tmp_path / "baseline.json"
    baseline.write_text(
        json.dumps({"summary": {"deskew": {"mean_ms": 100.0}, "output": {"mean_ms": 100.0}}}),
        encoding="utf-8",
    )
    current = {"deskew": {"mean_ms": 119.0}, "output": {"mean_ms": 121.0}, "margins": {}}
    regressions = bench.compare(current, baseline, 0.20)
    assert len(regressions) == 1
    assert regressions[0].startswith("output:")


def test_benchmark_flags_stages_over_their_budget() -> None:
    bench = _load()
    assert bench.STAGE_BUDGETS_MS["deskew"] == 300.0  # #195 section 8.1
    assert bench.over_budget({"deskew": {"mean_ms": 120.0}, "output": {"mean_ms": 900.0}}) == []
    failures = bench.over_budget({"deskew": {"mean_ms": 301.0}})
    assert failures == ["deskew: 301 ms > budget 300 ms"]
