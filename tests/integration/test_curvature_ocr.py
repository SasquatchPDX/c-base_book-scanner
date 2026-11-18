"""Real OCR on synthetic curl; does not replace photographed-book/site acceptance."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from rapidfuzz.distance import Levenshtein

from ebook_server.core import curvature
from tests.curvature_fixtures import LINES, curled_page

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires real Tesseract English acceptance tooling",
    ),
]


@pytest.mark.parametrize("amplitude", [24, 64], ids=["synthetic-mild", "synthetic-strong"])
def test_synthetic_curl_ocr_before_and_after(amplitude: int, tmp_path: Path) -> None:
    executable = shutil.which("tesseract")
    assert executable, "install tesseract-ocr and English traineddata"
    source = curled_page(amplitude)
    curves = curvature.detect_curves(source)
    corrected, _geometry = curvature.render(source, curves)
    expected = re.sub("[^A-Z0-9]", "", " ".join(LINES))
    rates = []
    for label, image in (("before", source), ("after", corrected)):
        path = tmp_path / f"{label}.png"
        image.save(path)
        result = subprocess.run(
            [executable, str(path), "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        text = re.sub("[^A-Z0-9]", "", result.stdout.upper())
        rates.append(Levenshtein.distance(expected, text) / len(expected))
    assert rates[1] <= 0.03, rates
    assert rates[1] <= rates[0] + 0.01, rates
    print(f"Synthetic curl {amplitude}px CER before={rates[0]:.4f}, after={rates[1]:.4f}")
