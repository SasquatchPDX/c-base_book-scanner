"""Run a ScanTailor stage runner directly on an image (#195 step 2 API)."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from PIL import Image

from ebook_server.core import scantailor
from ebook_server.core.models import ScanTailorStageState


def stage_input(source: Image.Image | Path) -> scantailor.StageInput:
    image = scantailor._load_image(source) if isinstance(source, Path) else source
    return scantailor.StageInput(
        image=image,
        full_size=image.size,
        render_full=lambda: image.copy(),
        dpi=scantailor._dpi_of(image),
    )


def run_stage(
    runner: Callable[[ScanTailorStageState, scantailor.StageInput], scantailor.StageResult],
    state: Any,
    source: Image.Image | Path,
) -> scantailor.StageResult:
    return runner(state, stage_input(source))
