"""Benchmark the ScanTailor-Web stage pipeline (#195, section 8).

Runs every stage runner of ``core.scantailor`` in pipeline order (Deskew, Select
Content, Dewarping, Margins, Output) on a set of page images, without a database or a
queue. Each stage reads the previous stage's output file, as the worker does. The per
stage time is split into phases by wrapping the shared helpers:

- ``decode``: ``_load_image`` (full decode, EXIF transpose, mode normalisation);
- ``encode``: ``_save_png`` of the full-resolution intermediate;
- ``preview``: ``_save_preview`` (copy, resize and a second PNG);
- ``hash``: SHA-256 of the output, as ``process_stage_job`` does before publishing;
- ``compute``: the rest (detection and pixel work).

Usage::

    uv run python scripts/benchmark_scanning.py --pages /path/to/weizenbaum/*.tif
    uv run python scripts/benchmark_scanning.py --synthetic 10 --json run.json
    uv run python scripts/benchmark_scanning.py --synthetic 10 --baseline run.json

``--baseline`` compares the mean per-page time of each stage with an earlier ``--json``
result and exits with status 1 if any stage is more than ``--tolerance`` (default 20 %)
slower. Record numbers on the 2-vCPU / 3 GB test VM; numbers from a developer machine
are only useful as relative comparisons.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

PHASES = ("decode", "encode", "preview", "hash", "compute")


def _setup_django() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")
    src = Path(__file__).resolve().parents[1] / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    import django

    django.setup()


def _synthetic_pages(count: int, directory: Path, width: int, height: int) -> list[Path]:
    """Text-like A4 pages at 300 dpi, slightly rotated, saved as LZW TIFF like the scanner."""
    import numpy as np
    from PIL import Image, ImageDraw

    rng = np.random.default_rng(195)
    pages = []
    for index in range(count):
        image = Image.new("L", (width, height), 236)
        draw = ImageDraw.Draw(image)
        left, right = int(width * 0.12), int(width * 0.88)
        y = int(height * 0.1)
        while y < height * 0.9:
            x = left
            while x < right:
                word = int(rng.integers(width // 60, width // 14))
                draw.rectangle((x, y, min(x + word, right), y + height // 160), fill=30)
                x += word + width // 90
            y += height // 55
        angle = float(rng.uniform(-2.5, 2.5))
        image = image.rotate(angle, expand=False, fillcolor=236)
        path = directory / f"page-{index + 1:04d}.tif"
        image.save(path, compression="tiff_lzw", dpi=(300, 300))
        pages.append(path)
    return pages


@contextmanager
def _phase_timers(scantailor: Any, totals: dict[str, float]) -> Iterator[None]:
    """Wrap the shared I/O helpers so their time is attributed to a phase."""

    def timed(name: str, function: Callable[..., Any]) -> Callable[..., Any]:
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return function(*args, **kwargs)
            finally:
                totals[name] += time.perf_counter() - started

        return wrapper

    originals = {
        "_load_image": scantailor._load_image,
        "_save_png": scantailor._save_png,
        "_save_preview": scantailor._save_preview,
    }
    scantailor._load_image = timed("decode", originals["_load_image"])
    scantailor._save_png = timed("encode", originals["_save_png"])
    scantailor._save_preview = timed("preview", originals["_save_preview"])
    try:
        yield
    finally:
        for name, function in originals.items():
            setattr(scantailor, name, function)


def _sha256(path: Path) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)


def run(pages: list[Path], workdir: Path) -> dict[str, Any]:
    from ebook_server.core import scantailor
    from ebook_server.core.models import ScanTailorStageState

    stages = scantailor.stage_order()
    per_stage: dict[str, list[dict[str, float]]] = defaultdict(list)
    failures: list[str] = []
    for page_index, page in enumerate(pages):
        source = page
        for stage in stages:
            algorithm = scantailor.DEFAULT_STAGE_ALGORITHM[stage]
            runner = scantailor._STAGE_RUNNERS[stage][algorithm]
            state = ScanTailorStageState(stage=stage, parameters={}, algorithm=algorithm)
            output = workdir / f"{page_index:04d}-{stage}.png"
            preview = workdir / f"{page_index:04d}-{stage}-preview.png"
            totals: dict[str, float] = defaultdict(float)
            started = time.perf_counter()
            try:
                with _phase_timers(scantailor, totals):
                    runner(state, source, output, preview)
                hash_started = time.perf_counter()
                _sha256(output)
                totals["hash"] = time.perf_counter() - hash_started
            except Exception as exc:  # report and continue with the next page
                failures.append(f"{page.name} {stage}: {exc}")
                break
            total = time.perf_counter() - started
            totals["compute"] = max(0.0, total - sum(totals[p] for p in PHASES if p != "compute"))
            per_stage[stage].append({"total": total, **{p: totals[p] for p in PHASES}})
            source = output
    return {"stages": per_stage, "failures": failures}


def summarise(raw: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {}
    for stage, samples in raw["stages"].items():
        totals = sorted(sample["total"] for sample in samples)
        p95_index = max(0, round(0.95 * len(totals)) - 1)
        summary[stage] = {
            "pages": len(samples),
            "mean_ms": statistics.fmean(totals) * 1000,
            "p95_ms": totals[p95_index] * 1000,
            "sum_s": sum(totals),
            "phases_mean_ms": {
                phase: statistics.fmean(sample[phase] for sample in samples) * 1000
                for phase in PHASES
            },
        }
    return summary


def print_table(summary: dict[str, Any], page_count: int) -> None:
    header = f"{'stage':<15}{'mean ms':>9}{'p95 ms':>9}" + "".join(f"{p:>9}" for p in PHASES)
    print(header)
    print("-" * len(header))
    for stage, row in summary.items():
        phases = "".join(f"{row['phases_mean_ms'][p]:>9.0f}" for p in PHASES)
        print(f"{stage:<15}{row['mean_ms']:>9.0f}{row['p95_ms']:>9.0f}{phases}")
    print()
    for stage, row in summary.items():
        projected = row["mean_ms"] / 1000 * 365
        print(
            f"{stage:<15} {page_count} pages: {row['sum_s']:.1f} s; 365 pages ~ {projected:.0f} s"
        )


def compare(summary: dict[str, Any], baseline_path: Path, tolerance: float) -> list[str]:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["summary"]
    regressions = []
    for stage, row in summary.items():
        before = baseline.get(stage)
        if before is None:
            continue
        limit = before["mean_ms"] * (1 + tolerance)
        if row["mean_ms"] > limit:
            regressions.append(
                f"{stage}: {row['mean_ms']:.0f} ms > {before['mean_ms']:.0f} ms + {tolerance:.0%}"
            )
    return regressions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--pages", nargs="+", type=Path, help="page images to process")
    source.add_argument("--synthetic", type=int, help="generate this many synthetic A4 pages")
    parser.add_argument("--size", default="2480x3508", help="synthetic page size, WxH")
    parser.add_argument("--json", type=Path, help="write raw samples and summary here")
    parser.add_argument("--baseline", type=Path, help="earlier --json result to compare with")
    parser.add_argument("--tolerance", type=float, default=0.20)
    args = parser.parse_args()

    _setup_django()
    with tempfile.TemporaryDirectory(prefix="scan-bench-") as tempdir:
        workdir = Path(tempdir)
        if args.synthetic is not None:
            width, height = (int(part) for part in args.size.lower().split("x"))
            pages = _synthetic_pages(args.synthetic, workdir, width, height)
        else:
            pages = sorted(args.pages)
        raw = run(pages, workdir)
    summary = summarise(raw)
    print_table(summary, len(pages))
    for failure in raw["failures"]:
        print(f"FAILED {failure}", file=sys.stderr)
    if args.json:
        args.json.write_text(
            json.dumps(
                {
                    "machine": {
                        "platform": platform.platform(),
                        "python": platform.python_version(),
                        "cpus": os.cpu_count(),
                    },
                    "pages": [str(page) for page in pages] if args.pages else args.synthetic,
                    "summary": summary,
                    "samples": raw["stages"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
    if args.baseline:
        regressions = compare(summary, args.baseline, args.tolerance)
        for regression in regressions:
            print(f"REGRESSION {regression}", file=sys.stderr)
        if regressions:
            return 1
    return 1 if raw["failures"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
