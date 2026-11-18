# Opt-in baseline curvature acceptance (#171)

These pages are **synthetic**, not scanned books. No physical-book quality corpus
or site-connected scans were supplied. Two public book photographs are being
evaluated separately; their acceptance measurements are not yet established.
This does not close the real-page
acceptance gate or enable curvature by default.

## Reproducible geometry and OCR

`tests/curvature_fixtures.py` generates calibration text using Pillow's bundled
font, then applies known quadratic vertical curl. Source size is 1000×1400;
mild/strong curl have 24/64-pixel peak displacement. The estimator sees only
text, not generating geometry. Its geometry is applied to a separate curved
calibration raster; straightness is pixel RMS residual after removing linear tilt.

| Synthetic curl | Before RMS | Corrected RMS | Tesseract CER before | CER after |
| --- | ---: | ---: | ---: | ---: |
| Mild, 24px | 5.062px | 0.084px | 0.0000 | 0.0000 |
| Strong, 64px | 13.470px | 0.500px | 0.0288 | 0.0000 |

Measured with OpenCV 4.14.0, NumPy 2.5.1, Pillow 13.0.0 and real Tesseract 5.5.2
English. CI uses installed Tesseract, allowing engine variation: after CER ≤3%
and no worse than before by more than one percentage point. Geometry gates
require residual <1.5px and at least 70% improvement. This is not evidence of
quality on real book fonts, languages, paper, pictures or a 3D surface.

```bash
uv run pytest tests/test_curvature.py -q -s
BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS=true \
  uv run pytest tests/integration/test_curvature_ocr.py -q -s
```

The second command needs `tesseract-ocr` and English traineddata. Tests also cover
flat-page passthrough, unsafe-geometry fallback, four-corner retention, manual
replay, model switching and worker provenance.

Automatic traces must preserve text-row order across their entire width. A bounded
longest-chain filter discards isolated crossing tracks, using observation coverage
and fit residual only to break ties. The retained geometry still passes the same
strict crossing/displacement checks as manual edits; invalid manual curves are
never silently filtered.

## Full-resolution memory

The 4000×6000 remap test checks every OpenCV map/crop: coordinate arrays are
≤512×512. Stage tests decode 24MP grayscale/RGB PNGs, render to 4000×6104, write
PNG output and ≤960px previews, and verify unchanged source files.

Fresh-process Windows stage-runner measurements include Django imports, decoding,
remap, output and preview—not just Python-traced heap allocations:

| Synthetic 24MP input | Peak working set | Stage time |
| --- | ---: | ---: |
| Grayscale | 170.89 MiB | 0.678s |
| RGB | 315.39 MiB | 1.108s |

These host-specific runs use three manual quadratic profiles with 100px endpoint
rise. They are not deployment memory limits. Source/output memory remains
proportional to pixel count. Decoder/allocator and mode affect peak memory.
Linux deployments can independently measure with `/usr/bin/time -v` around:

```bash
uv run pytest tests/test_curvature.py -k full_resolution -q
```

## Open real-page/site gate

- Supply rights-cleared photographed pages with transcription, mild/strong binding
  curl, different fonts/languages and mixed content to the quality corpus.
- Compare baseline residual and OCR against perspective and uncorrected images;
  inspect pictures, margins and page edges.
- Re-run memory checks under intended deployment worker limits.
- Keep perspective as default until that evidence is reviewed.
