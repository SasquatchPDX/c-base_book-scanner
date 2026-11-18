"""A real OCR/detail non-regression check on synthetic illustrated content (#172)."""

import os
import re
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest
from rapidfuzz.distance import Levenshtein

from tests.output_zone_fixtures import PICTURE_BOXES, TRANSCRIPT, illustrated_page
from tests.test_output_zones import run_output

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the real Tesseract acceptance tool",
    ),
]


def test_real_ocr_and_picture_detail_against_whole_page(tmp_path: Path) -> None:
    executable = shutil.which("tesseract")
    assert executable, "install tesseract-ocr and English traineddata for this lane"
    baseline, _ = run_output(tmp_path, {"zone_mode": "off"})
    zoned, result = run_output(tmp_path, {})
    assert not result["output_warning"]
    expected = re.sub("[^A-Z0-9]", "", TRANSCRIPT.upper())
    rates = []
    for label, image in (("whole", baseline), ("zoned", zoned)):
        path = tmp_path / f"{label}-text.png"
        image.crop((55, 55, 490, 255)).save(path)
        completed = subprocess.run(
            [executable, str(path), "stdout", "-l", "eng", "--psm", "6"],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        actual = re.sub("[^A-Z0-9]", "", completed.stdout.upper())
        rates.append(Levenshtein.distance(expected, actual) / len(expected))
    assert rates[1] <= 0.05, rates
    assert rates[1] <= rates[0] + 0.01, rates
    source = illustrated_page()
    for box in PICTURE_BOXES:
        original = np.asarray(source.crop(box), dtype=np.int16)
        whole_error = np.abs(
            np.asarray(baseline.convert("RGB").crop(box), dtype=np.int16) - original
        ).mean()
        zoned_error = np.abs(
            np.asarray(zoned.convert("RGB").crop(box), dtype=np.int16) - original
        ).mean()
        assert zoned_error < whole_error * 0.15
    # Also ensure the fixture really contains continuous tones, not a binary drawing.
    assert len(source.crop(PICTURE_BOXES[0]).getcolors(maxcolors=1_000_000) or []) > 100
    print(f"Synthetic OCR CER whole={rates[0]:.4f}, zoned={rates[1]:.4f}; picture detail retained")
