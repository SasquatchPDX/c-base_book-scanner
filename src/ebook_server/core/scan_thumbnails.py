"""Bounded JPEG thumbnails of scan artifacts (workspace and scanner API, #131).

One renderer for the workspace page thumbnails (``/scan/pages/{id}/image/``) and the
scanner's rescan thumbnails (``GET /api/scanner/v1/jobs/{id}/pages/{n}/thumbnail``).
Rendering is bounded:

* a JPEG is decoded at a reduced scale (``Image.draft``), so a 3840x2160 page never
  needs its full-size raster for an 800 px thumbnail;
* Pillow's decompression-bomb limit applies, and it is an error here, not a warning;
* at most :data:`MAX_CONCURRENT_RENDERS` renders run at once per process; the rest
  wait for a slot.

Results are cached as files keyed by the artifact's identity and the edge length; the
write is atomic (temporary file, then rename), so concurrent renders of the same key
are harmless.
"""

from __future__ import annotations

import threading
import warnings
from pathlib import Path
from typing import Final
from uuid import uuid4

from ebook_server.storage import StorageBackend

MAX_CONCURRENT_RENDERS: Final = 2
JPEG_QUALITY: Final = 85

_render_slots = threading.BoundedSemaphore(MAX_CONCURRENT_RENDERS)


def render_jpeg_thumbnail(
    storage: StorageBackend, identifier: str, cache_path: Path, max_edge: int
) -> None:
    """Write a JPEG of ``identifier`` with its longest edge at most ``max_edge``.

    Raises ``FileNotFoundError`` for a missing artifact and Pillow's errors for data
    that does not decode.
    """
    from PIL import Image, ImageOps

    cache_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = cache_path.with_name(f"{cache_path.name}.{uuid4().hex}.tmp")
    with _render_slots:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with storage.open(identifier) as stream, Image.open(stream) as source:
                    if source.format == "JPEG":
                        # Decode at the smallest DCT scale that still covers max_edge.
                        source.draft("RGB", (max_edge, max_edge))
                    image = ImageOps.exif_transpose(source) or source
                    if image.mode in {"I;16", "I;16L", "I;16B", "I"}:
                        image = (
                            image.convert("I").point(lambda value: value * (1 / 256)).convert("L")
                        )
                    elif image.mode == "F":
                        image = image.point(lambda value: value * 255).convert("L")
                    elif image.mode not in {"RGB", "L"}:
                        image = image.convert("RGB")
                    image.thumbnail((max_edge, max_edge))
                    image.convert("RGB").save(temp_path, format="JPEG", quality=JPEG_QUALITY)
            temp_path.replace(cache_path)
        finally:
            temp_path.unlink(missing_ok=True)
