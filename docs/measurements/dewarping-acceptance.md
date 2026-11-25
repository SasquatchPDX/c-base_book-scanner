# Cylindrical dewarping acceptance (#171, carried into #195 §6.3)

Stage `dewarping`, algorithm `cylindrical_dewarp`, version `cylindrical-v5` (#195 step 6a:
the port of Scan Tailor's `CylindricalSurfaceDewarper`). Auto mode still uses the
pre-port baseline detector as a bridge until step 6b ports upstream's detection.

All numbers below come from **synthetic pages**. They are not scans of a physical book.
No site-connected scanner corpus or rights-cleared photographed pages were available. The
real-page gate at the end is still open.

## Reproduce

```bash
uv run python scripts/measure_dewarping.py --memory            # geometry + memory
uv run python scripts/measure_dewarping.py --tesseract "$(command -v tesseract)" --json run.json
uv run pytest tests/test_dewarping_acceptance.py -q
BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS=true uv run pytest tests/integration/test_dewarping_ocr.py -q -s
```

CI runs the unit tests in `test` and the OCR test in `infrastructure`, which installs
`tesseract-ocr` and `tesseract-ocr-eng`.

## 1. Straightness and OCR on mild and strong curl

The fixture is `tests/curvature_fixtures.cylinder_page(lift)`. It takes a 1000×1400 page
(18 text lines, or 5 ruled calibration lines), bends it into a surface that rises by
`lift` px towards the spine (`lift·(1−x/W)²`, horizontal positions follow arc length) and
photographs it with a pinhole camera 3000 px above the page centre. Lines above the camera
axis bow one way and lines below it bow the other, which is the geometry the cylindrical
model assumes. Mild is `lift=140`, and the top ruled line bows 21 px peak to peak. Strong
is `lift=340`, with a 54.7 px bow.

The stage runner `_run_dewarping` runs on the text page in two modes:

- **auto**: the curves come from the detector.
- **manual**: the known baselines are entered as the editor's five control points and
  adjusted until the drawn X-spline lies on them, as a reviewer would drag them.

The recorded transform op is then applied to the calibration page with the same curl.
The script reports three line measures:

- *Level*: the distance of the line centres from each line's mean height. A dewarped page
  should have level lines, so this measure includes any tilt that remains.
- *p-p*: the largest height change along one line.
- *Bow*: the residual after removing a fitted straight line. This is the measure used for
  #191.

OCR is Tesseract `-l eng --psm 6` on the text page before and after. CER counts only A–Z
and 0–9.

Measured on 2026-11-25: Windows 11, Python 3.13.9, NumPy 2.5.1, OpenCV 4.14.0,
Tesseract 5.5.4 (conda-forge).

| Case | Mode | Level RMS before → after | Line p-p before → after | Bow RMS before → after | CER before → after |
| --- | --- | ---: | ---: | ---: | ---: |
| flat (0 px) | auto | 0.00 → 0.13 px | 0.00 → 0.98 px | 0.00 → 0.09 px | 0.0000 → 0.0000 |
| flat (0 px) | manual | 0.00 → 0.00 px | 0.00 → 0.00 px | 0.00 → 0.00 px | 0.0000 → 0.0000 |
| mild (21 px) | auto | 4.14 → 0.13 px | 19.96 → 0.86 px | 0.86 → 0.11 px | 0.0000 → 0.0000 |
| mild (21 px) | manual | 4.14 → 0.17 px | 19.96 → 0.88 px | 0.86 → 0.16 px | 0.0000 → 0.0000 |
| strong (54.7 px) | auto | 9.90 → 0.18 px | 47.46 → 0.96 px | 1.78 → 0.13 px | 0.0288 → 0.0000 |
| strong (54.7 px) | manual | 9.90 → 0.16 px | 47.46 → 0.90 px | 1.78 → 0.15 px | 0.0288 → 0.0000 |

Stage time on the 1000×1400 page was 0.11–0.21 s.

The test gates allow for engine and host variation:

- Geometry (`tests/test_dewarping_acceptance.py`): after correction, level RMS must be
  under 0.5 px and under 10 % of the value before. Line p-p must be under 1.5 px. A flat
  page must stay under 0.3 px RMS.
- OCR (`tests/integration/test_dewarping_ocr.py`): CER after correction must be at most
  1 %, and no worse than before by more than 0.5 percentage points.

### Findings

- **A flat page is resampled in auto mode.** The detector finds baselines, and the model
  is applied even though the page is flat. The result stays level within 1 px and OCR is
  unchanged, but the page goes through one bilinear resampling. The pre-port model had a
  `none_needed` pass-through below 2 px. Step 6b should decide whether to bring that back.
- **The #191 fixture does not fit this model.** `curled_page` shifts every row by the same
  quadratic, so the top and bottom curves are identical. Upstream's
  `calcPlnStraightLineY` then falls back to the plane midline as "the line that stays
  straight", and the lines between the curves bow the other way. Measured with this
  script's bow RMS in auto mode: 5.07→3.41 px (24 px) and 13.47→9.55 px (64 px). A real
  photographed page is not shaped like that fixture. Use `cylinder_page` for this model.
- The table does not show how a page is stretched horizontally (arc-length unrolling) or
  how depth perception affects it.

## 2. Persisted manual geometry, replay, bypass and fallback

Covered by `tests/test_dewarping_acceptance.py`. Model-level tests are in
`tests/test_cylindrical.py`, and the PostgreSQL replay is in
`tests/integration/test_curvature_postgres.py`.

- **Replay.** Manual `top_curve`, `bottom_curve` and `depth_perception` are stored in the
  stage parameters. Replaying them gives the same op and identical pixels, both in-process
  and through the worker (same `output_sha256`).
- **Batch apply.** Applying auto to all pages with "preserve exceptions" skips the manual
  page. Its revision and curves stay unchanged.
- **Upstream edits.** An upstream Deskew edit invalidates the stage. Re-running it keeps
  the manual model.
- **Bypass.** `off` and the legacy `bypass` return the input pixels unchanged, with the
  identity op, and drop the curves.
- **Safe fallback.** In each of the following cases the page stays unchanged, the op is
  the identity, `dewarp_applied=false`, and `dewarp_warning` explains why:
  - no text lines are found;
  - the curves do not form a valid model (upstream's convexity check);
  - the model cannot be rendered (`DewarpError`).
- **Bounded manual geometry (new).** A manual curve may have at most 64 control points,
  and every point must lie within one page size of the page. Before this limit, one point
  at `y=1e300` kept the worker sampling the spline for minutes, which is a runaway job.
  Points outside the limits are refused at once, with a warning, and kept so the editor can
  show them. Over 3000 random manual curve pairs, no stage run raised an exception.

## 3. Worker memory on full-resolution pages

Output renders the original once through the composed ops, including the cylindrical op.
`RasterDewarper` used to build two float32 coordinate maps of the full output size, which
is 2×92 MiB for a 4000×6000 page. `cylindrical.dewarp_image` now computes each output
column's generatrix once and builds the maps in strips of `MAP_STRIP_ROWS = 64` rows, with
reused scratch buffers. The pixels are identical to the single-remap version
(`test_strips_give_the_same_pixels_as_one_remap`).

The script's `--memory` option measures the peak working set in a fresh process. The
"before" column is after imports and page creation. The test page is 4000×6000 with a
strongly curved model.

| Mode | Before the strips | With strips (this change) |
| --- | ---: | ---: |
| L (22.9 MiB page) | 132 → 366 MiB, 3.3 s | 132 → 188 MiB, 0.8–1.8 s |
| RGB (68.7 MiB page) | 201 → 526 MiB, 4.8 s | 201 → 364 MiB, 1.2–1.7 s |

`test_full_resolution_render_builds_maps_in_bounded_strips` checks two things on a 24 MP
page. No map strip is taller than `MAP_STRIP_ROWS`. The traced NumPy peak stays below the
output size plus 16 MiB. It was measured at 29.2 MiB for a 22.9 MiB output, against more
than 200 MiB before.

These figures are host-specific. They are not deployment limits. Source and output memory
still grow with the pixel count. Re-measure on the 2-vCPU / 3 GB VM with the intended
worker count, for example with `/usr/bin/time -v uv run python scripts/measure_dewarping.py --memory`.

## Still open (site or human evaluation)

- Real photographed or scanned pages with transcriptions, at mild and strong binding
  curl, in different fonts and languages, including mixed content. Compare uncorrected,
  auto and manual results, and inspect pictures, margins and page edges.
- Memory under the deployment worker limits, on the test VM.
- **The automatic mode is already the default** (`dewarp_mode` defaults to `auto`).
  #171 and #195 ask for the results to be inspected before that happens. The owner should
  confirm that default or switch it to `off` until step 6b and the real-page review are
  done.
- **Perspective option.** #171 asked to keep the existing perspective model. Step 6a
  (`9b9b993`) removed the `dewarp_model=perspective` choice from the stage, which now
  runs only off/auto/manual, as upstream does. The `perspective` transform op is still
  available in `scan_transform`. Whether to bring the option back is an owner decision.
