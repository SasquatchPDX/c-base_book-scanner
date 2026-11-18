# Opt-in baseline curvature acceptance (#171)

The generated fixtures below are **synthetic**, not scanned books. Two public
photographs were also measured independently, as described separately below. No
site-connected scanner corpus was supplied. This does not close the broader
real-page acceptance gate or enable curvature by default.

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

## Two real photographed pages: limited evidence

The Boston Cooking-School Cook Book photographs come from
[`mzucker/page_dewarp` example_input at d3257d7](https://github.com/mzucker/page_dewarp/tree/d3257d7b564bff994187a872d00537bc2fda7d3d/example_input).
They are real book photographs, not generated fixtures or this project's scanner
output. Both original JPEGs are 2448×3264. Apply EXIF orientation before cropping:

| Photograph | SHA-256 of original | Body crop `(left, top, right, bottom)` |
| --- | --- | --- |
| A, pp248–249 source A | `b7e3aac880412b7967c49b0b3271fb58a333a614fab0abf58e2bd137de3482f3` | `(420, 250, 2150, 3060)` |
| B, source B | `5a518c607a701c26b5e60c366df06e64c1b8124d4452d1a3d06c427c975fce38` | `(305, 205, 2050, 3120)` |

The initial detector safely rejected both pages because isolated false tracks
crossed genuine text rows. Ordered automatic-track selection retains 29/28 curves,
with maximum displacements 153.13/203.19px and vertical padding 156/206px.

An independent review measured these outputs with actual Tesseract 5.5.2 English,
`--psm 6`, against manually transcribed passages. OCR was restricted to the same
source pixels before and after correction using the rendered passage mask. Raw
page passage y ranges are A `[1035,1897)` and B `[1030,1690)`; relative to the body
crops these are `[785,1647)` and `[825,1485)`. Transcripts and OCR are normalized
with NFC, uppercase, then only A–Z and 0–9, before Levenshtein CER:

| Passage | Reference characters | OCR errors before → after | CER before → after |
| --- | ---: | ---: | ---: |
| A, Braised Chicken | 503 | 5 → 0 | 0.994% → 0% |
| B, Fried Chicken (Southern Style) | 330 | 5 → 1 | 1.515% → 0.303% |

Geometry is a separate gate. Median detrended strip-center RMS on selected text
lines was A **2.71→2.45px** (9 lines), B **2.58→2.88px** (5 lines). The second page
slightly regresses despite improved OCR. Strip centers measure ink bands rather
than annotated ground-truth baselines: binarize with adaptive Gaussian threshold
31/C12, use 17 horizontal positions from 13.5% to 86.5% of body width, 52px-wide
strips, and median ink y; remove a fitted linear tilt before RMS. Corresponding
source masks are remapped to select the same lines after correction.

These two samples support inspecting an opt-in implementation, not a general
quality claim. Automatic traces, page-edge extrapolation, mixed content, languages
and severe binding curvature still need a larger annotated scanner corpus.

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
