"""Real Tesseract before/after the cylindrical Dewarping stage on synthetic curl (#171).

Synthetic pages (``cylinder_page``); this does not replace a photographed-book corpus.
"""

import os
import shutil
from pathlib import Path

import pytest

from tests.test_dewarping_acceptance import _script

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires real Tesseract English acceptance tooling",
    ),
]


@pytest.mark.parametrize("mode", ["auto", "manual"])
@pytest.mark.parametrize(("name", "lift"), [("mild", 140.0), ("strong", 340.0)])
def test_cylinder_curl_ocr_before_and_after(
    name: str, lift: float, mode: str, tmp_path: Path
) -> None:
    executable = shutil.which("tesseract")
    assert executable, "install tesseract-ocr and English traineddata"
    row = _script().measure_case(name, lift, mode, executable, tmp_path)
    assert row["dewarp_applied"] is True, row
    # Engine versions differ between hosts: bound the result, do not pin it.
    assert row["cer_after"] <= 0.01, row
    assert row["cer_after"] <= row["cer_before"] + 0.005, row
    print(f"{name} {mode}: CER {row['cer_before']:.4f} -> {row['cer_after']:.4f}")
