"""Measure the Dewarping stage on synthetic book curl (#171 acceptance, #195 section 6.3).

For each case of ``tests/curvature_fixtures.py`` (flat, mild 24 px and strong 64 px
quadratic curl on a 1000x1400 page) the script runs the real stage runner
``scantailor._run_dewarping`` twice on the *text* page:

- ``auto``: the stage's automatic mode (the curves come from the detector), and
- ``manual``: the known generating curl along the top and bottom text baseline,
  entered as the editor's five control points and adjusted until the drawn curve lies
  on the baseline, as a reviewer would drag them.

The transform op each run records is then applied to a calibration raster with the same
curl (five ruled lines), and the straightness of those lines is measured before and
after: the RMS residual of the ink centre after removing a fitted straight line. With
``--tesseract`` (or ``tesseract`` on ``PATH``) the text page is also recognised before
and after (``-l eng --psm 6``) and the character error rate against the known text is
reported (only A-Z and 0-9, as ``tests/integration/test_curvature_ocr.py``).

``--memory`` additionally renders one 4000x6000 (24 MP) page through the cylindrical op
at full resolution, as the Output stage does, in a fresh child process per image mode,
and reports the child's peak resident memory and time.

These are synthetic pages, not scans of a physical book: they show that the stage
straightens a known curl and does not damage a flat page; they say nothing about real
fonts, paper, 3D page shape or pictures (see docs/measurements/dewarping-acceptance.md).

Usage::

    uv run python scripts/measure_dewarping.py
    uv run python scripts/measure_dewarping.py --tesseract /usr/bin/tesseract --memory
    uv run python scripts/measure_dewarping.py --json result.json
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

#: (name, how far the page rises towards the spine in pixels; see ``cylinder_page``)
CASES: tuple[tuple[str, float], ...] = (("flat", 0.0), ("mild", 140.0), ("strong", 340.0))
MODES: tuple[str, ...] = ("auto", "manual")
FULL_SIZE = (4000, 6000)


def _setup() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")
    for path in (ROOT / "src", ROOT):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    import django

    django.setup()


def straightness(image: Any, rows: int) -> dict[str, float]:
    """How far ``rows`` ruled lines are from straight horizontal lines, in pixels.

    Every third column from 8 % to 92 % of the width is scanned for dark runs; a column
    counts only when it holds exactly ``rows`` runs, which are then assigned to the lines
    top to bottom, so it works on curled and on corrected pages alike. Returns

    - ``level_rms``: RMS distance of the line centres from each line's mean height (a
      dewarped page should have level lines; this includes any remaining tilt);
    - ``level_ptp``: the largest peak-to-peak height change of one line;
    - ``bow_rms``: RMS residual after removing each line's fitted straight line (the
      #171 baseline-curvature measure, blind to tilt).
    """
    import numpy as np

    gray = np.asarray(image.convert("L"))
    height, width = gray.shape
    columns = range(int(width * 0.08), int(width * 0.92), 3)
    samples: list[list[tuple[float, float]]] = [[] for _ in range(rows)]
    for x in columns:
        dark = np.flatnonzero(gray[:, x] < 160)
        if not len(dark):
            continue
        runs = np.split(dark, np.flatnonzero(np.diff(dark) > 1) + 1)
        if len(runs) != rows:
            continue
        for line, run in enumerate(runs):
            weights = 255.0 - gray[run, x].astype(np.float64)
            samples[line].append((float(x), float(np.average(run, weights=weights))))
    level: list[float] = []
    bow: list[float] = []
    ptp = 0.0
    for points in samples:
        if len(points) < 0.8 * len(columns):
            raise ValueError("the calibration lines could not be traced")
        data = np.asarray(points)
        fit = np.polyfit(data[:, 0], data[:, 1], 1)
        bow.extend((data[:, 1] - np.polyval(fit, data[:, 0])).tolist())
        level.extend((data[:, 1] - data[:, 1].mean()).tolist())
        ptp = max(ptp, float(np.ptp(data[:, 1])))
    return {
        "level_rms": float(np.sqrt(np.mean(np.square(level)))),
        "level_ptp": ptp,
        "bow_rms": float(np.sqrt(np.mean(np.square(bow)))),
    }


def _fitted_spline(xs: Any, ys: Any) -> Any:
    """Five editor control points (ends tension 0, others 1, as upstream's editor) moved
    until the drawn X-spline lies on the curve ``(xs, ys)``, as a reviewer drags them.

    With tension 1 the spline does not pass through its inner control points, so the
    points are corrected by the remaining vertical error at their x until it vanishes.
    """
    import numpy as np

    from ebook_server.core import cylindrical

    control_x = np.linspace(float(xs[0]), float(xs[-1]), 5)
    control_y = np.interp(control_x, xs, ys)
    target = np.interp(control_x, xs, ys)
    spline = cylindrical.XSpline(list(zip(control_x, control_y, strict=True)))
    for _ in range(200):
        drawn = np.asarray(spline.to_polyline())
        error = np.interp(control_x, drawn[:, 0], drawn[:, 1]) - target
        if np.abs(error).max() < 0.01:
            break
        control_y = control_y - error
        spline = cylindrical.XSpline(list(zip(control_x, control_y, strict=True)))
    return spline


def _known_curves(lift: float) -> dict[str, Any]:
    """The first and last text baseline as the camera sees them, as editor control
    points fitted so that the drawn curves follow them (see :func:`_fitted_spline`)."""
    import numpy as np

    from tests import curvature_fixtures as fixtures

    curves = {}
    for key, y in zip(("top_curve", "bottom_curve"), fixtures.TEXT_BASELINES, strict=True):
        points = np.asarray(
            [
                fixtures.cylinder_projection(lift, (float(x), y))
                for x in np.linspace(0.0, float(fixtures.SIZE[0]), 201)
            ]
        )
        spline = _fitted_spline(points[:, 0], points[:, 1])
        curves[key] = [[float(px), float(py)] for px, py in spline.points]
    return curves


def _curl(lift: float) -> float:
    """Peak-to-peak vertical bow of the top ruled line in the photographed page (px)."""
    from tests import curvature_fixtures as fixtures

    y = float(fixtures.CALIBRATION_ROWS[0])
    ys = [fixtures.cylinder_projection(lift, (float(x), y))[1] for x in range(70, 931, 10)]
    return max(ys) - min(ys)


def _ocr_cer(executable: str, image: Any, directory: Path, label: str) -> float:
    from rapidfuzz.distance import Levenshtein

    from tests.curvature_fixtures import LINES

    path = directory / f"{label}.png"
    image.save(path)
    result = subprocess.run(
        [executable, str(path), "stdout", "-l", "eng", "--psm", "6"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    expected = re.sub("[^A-Z0-9]", "", " ".join(LINES))
    recognised = re.sub("[^A-Z0-9]", "", result.stdout.upper())
    return Levenshtein.distance(expected, recognised) / len(expected)


def tesseract_version(executable: str) -> str:
    result = subprocess.run(
        [executable, "--version"], capture_output=True, text=True, check=True, timeout=30
    )
    return (result.stdout or result.stderr).splitlines()[0].strip()


def measure_case(
    name: str, lift: float, mode: str, tesseract: str | None, directory: Path
) -> dict[str, Any]:
    """Run the stage on the text page and measure the recorded op on the calibration page."""
    from ebook_server.core import scan_transform, scantailor
    from ebook_server.core.models import ScanTailorStageState
    from tests.curvature_fixtures import CALIBRATION_ROWS, cylinder_page

    text = cylinder_page(lift)
    calibration = cylinder_page(lift, calibration=True)
    parameters: dict[str, Any] = {"dewarp_mode": mode}
    if mode == "manual":
        parameters.update(_known_curves(lift))
    state = ScanTailorStageState(stage="dewarping", parameters=parameters)
    stage_input = scantailor.StageInput(
        image=text, full_size=text.size, render_full=lambda: text.copy(), dpi=(300, 300)
    )
    started = time.perf_counter()
    result = scantailor._run_dewarping(state, stage_input)
    seconds = time.perf_counter() - started
    recorded = result.parameters
    op = recorded["transform"]
    corrected = scan_transform.apply(calibration, op)
    row: dict[str, Any] = {
        "case": name,
        "lift_px": lift,
        "curl_px": round(_curl(lift), 1),
        "mode": mode,
        "recorded_mode": recorded["dewarp_mode"],
        "dewarp_applied": recorded["dewarp_applied"],
        "warning": recorded["dewarp_warning"],
        "op": op["op"],
        "stage_seconds": round(seconds, 3),
    }
    for label, image in (("before", calibration), ("after", corrected)):
        for key, value in straightness(image, len(CALIBRATION_ROWS)).items():
            row[f"{key}_{label}_px"] = round(value, 3)
    if tesseract:
        row["cer_before"] = round(_ocr_cer(tesseract, text, directory, f"{name}-{mode}-in"), 4)
        row["cer_after"] = round(
            _ocr_cer(tesseract, result.image, directory, f"{name}-{mode}-out"), 4
        )
    return row


def _peak_rss_mib() -> float:
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        query = kernel32.K32GetProcessMemoryInfo
        query.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
        counters = Counters()
        counters.cb = ctypes.sizeof(counters)
        if not query(kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb):
            raise OSError("K32GetProcessMemoryInfo failed")
        return counters.PeakWorkingSetSize / 2**20
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return peak / 2**20 if sys.platform == "darwin" else peak / 2**10


def full_resolution_op(size: tuple[int, int] = FULL_SIZE) -> dict[str, Any]:
    """A strongly curved cylindrical op scaled to ``size`` (the unit-test model x10)."""
    from ebook_server.core import cylindrical, scan_transform

    kx, ky = size[0] / 400, size[1] / 600
    top = [(20, 60), (100, 80), (200, 92), (300, 80), (380, 60)]
    bottom = [(20, 540), (100, 548), (200, 552), (300, 548), (380, 540)]
    top_line = cylindrical.XSpline([(x * kx, y * ky) for x, y in top]).to_polyline()
    bottom_line = cylindrical.XSpline([(x * kx, y * ky) for x, y in bottom]).to_polyline()
    return scan_transform.cylindrical_op(top_line, bottom_line, 2.0, size)


def memory_child(mode: str) -> dict[str, Any]:
    """Render one full-resolution page through the cylindrical op (this process only)."""
    from PIL import Image, ImageDraw

    from ebook_server.core import scan_transform

    op = full_resolution_op()
    page = Image.new(mode, FULL_SIZE, 255 if mode == "L" else (255, 255, 255))
    draw = ImageDraw.Draw(page)
    for y in range(100, FULL_SIZE[1] - 100, 100):
        draw.line((50, y, FULL_SIZE[0] - 50, y), fill=0, width=3)
    del draw
    baseline = _peak_rss_mib()
    started = time.perf_counter()
    output = scan_transform.apply(page, op)
    seconds = time.perf_counter() - started
    assert output.size == FULL_SIZE
    return {
        "mode": mode,
        "pixels": FULL_SIZE[0] * FULL_SIZE[1],
        "page_mib": round(FULL_SIZE[0] * FULL_SIZE[1] * len(page.getbands()) / 2**20, 1),
        "peak_rss_before_mib": round(baseline, 1),
        "peak_rss_mib": round(_peak_rss_mib(), 1),
        "render_seconds": round(seconds, 2),
    }


def measure_memory(modes: tuple[str, ...] = ("L", "RGB")) -> list[dict[str, Any]]:
    rows = []
    for mode in modes:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--memory-child", mode],
            capture_output=True,
            text=True,
            check=True,
            timeout=600,
        )
        rows.append(json.loads(result.stdout.strip().splitlines()[-1]))
    return rows


def run(tesseract: str | None, memory: bool) -> dict[str, Any]:
    import cv2
    import numpy as np

    from ebook_server.core import scantailor

    report: dict[str, Any] = {
        "algorithm_version": scantailor.DEFAULT_STAGE_ALGORITHM_VERSION["dewarping"],
        "platform": platform.platform(),
        "python": platform.python_version(),
        "numpy": np.__version__,
        "opencv": cv2.__version__,
        "tesseract": tesseract_version(tesseract) if tesseract else None,
        "cases": [],
        "memory": [],
    }
    with tempfile.TemporaryDirectory() as directory:
        for name, lift in CASES:
            for mode in MODES:
                report["cases"].append(measure_case(name, lift, mode, tesseract, Path(directory)))
    if memory:
        report["memory"] = measure_memory()
    return report


def _table(report: dict[str, Any]) -> str:
    ocr = report["tesseract"] is not None
    head = (
        "| Case | Mode | Applied | Level RMS before -> after | Level p-p before -> after "
        "| Bow RMS before -> after |"
    )
    rule = "| --- | --- | --- | ---: | ---: | ---: |"
    if ocr:
        head += " CER before -> after |"
        rule += " ---: |"
    lines = [head, rule]
    for row in report["cases"]:
        line = (
            f"| {row['case']} ({row['curl_px']:g} px curl) | {row['mode']} "
            f"| {row['dewarp_applied']} |"
        )
        for key in ("level_rms", "level_ptp", "bow_rms"):
            line += f" {row[f'{key}_before_px']:.2f} -> {row[f'{key}_after_px']:.2f} px |"
        if ocr:
            line += f" {row['cer_before']:.4f} -> {row['cer_after']:.4f} |"
        lines.append(line)
    for row in report["memory"]:
        lines.append(
            f"memory {row['mode']} {FULL_SIZE[0]}x{FULL_SIZE[1]}: page {row['page_mib']} MiB, "
            f"peak RSS {row['peak_rss_before_mib']} -> {row['peak_rss_mib']} MiB, "
            f"{row['render_seconds']} s"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tesseract", help="tesseract executable (default: on PATH)")
    parser.add_argument("--no-ocr", action="store_true", help="skip OCR even if available")
    parser.add_argument("--memory", action="store_true", help="measure a 24 MP render")
    parser.add_argument("--json", type=Path, help="also write the report as JSON")
    parser.add_argument("--memory-child", choices=("L", "RGB"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    _setup()
    if args.memory_child:
        print(json.dumps(memory_child(args.memory_child)))
        return 0
    tesseract = None if args.no_ocr else (args.tesseract or shutil.which("tesseract"))
    report = run(tesseract, args.memory)
    print(_table(report))
    if tesseract is None:
        print("OCR not measured: no tesseract executable (pass --tesseract).")
    if args.json:
        args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
