# Scan Tailor port register

ScanTailor-Web reproduces the processing stages of
[Scan Tailor](https://github.com/scantailor/scantailor) (copyright Joseph Artsimovich and
contributors, licensed "GPL version 3, or (at your option) any later version"). Issue
[#195](https://github.com/SasquatchPDX/c-base_book-scanner/issues/195) requires porting the
upstream algorithms rather than reinterpreting a description of them. This project is
therefore licensed `GPL-3.0-or-later` (see [`LICENSE`](../../LICENSE)).

## Rules

1. Read the upstream sources listed for a stage before writing code for it.
2. Every ported or derived module starts with a header that names the upstream files it
   derives from, the upstream copyright line and the GPL notice:

   ```python
   # Derived from Scan Tailor <upstream path(s)>
   # Copyright (C) Joseph Artsimovich <joseph.artsimovich@gmail.com> and contributors
   # Copyright (C) c-base_book-scanner contributors
   #
   # This program is free software: you can redistribute it and/or modify it under the
   # terms of the GNU General Public License as published by the Free Software
   # Foundation, either version 3 of the License, or (at your option) any later version.
   # See the LICENSE file at the repository root.
   ```

3. Add a row to the register below in the same PR: upstream file, new file, `direct port`
   or `reimplemented`, and every divergence with its reason. "Simpler" is not a reason.
4. Code taken from a fork (Scan Tailor Advanced, Enhanced, ...) follows the same rules and
   names the fork and its commit.
5. A new dependency needs a licence check against GPL-3.0 (table at the end).

## Register

Upstream paths are relative to `scantailor/scantailor` (master).

**Status:** Deskew (#195 step 4), Select Content (step 5) and the Dewarping model (step 6a) are ported. The other stages' code in
`src/ebook_server/core/scantailor.py` was written from a *description* of Scan Tailor
and is listed as `pre-port` until its PR replaces the row with the real port.

| Web stage | Upstream files | Current module | Status | Known divergences |
|---|---|---|---|---|
| 1 Deskew | `imageproc/SkewFinder.{h,cpp}`, `imageproc/Shear.cpp` (`vShearFromTo`), `imageproc/ReduceThreshold.cpp`, `imageproc/BinaryThreshold.cpp` (`otsuThreshold`), `imageproc/Morphology.cpp` (`openBrick`), `imageproc/SeedFill.cpp`, `imageproc/UpscaleIntegerTimes.cpp`, `filters/deskew/Task.cpp` (`process`, `cleanup`) | `core/deskew.py`, `core/binary_image.py`, `core/scantailor.py` (`_run_deskew`) | direct port | (1) Detection runs on the ~150 dpi working image, not the full-resolution original (#195 step 2). The reductions are chosen from its dpi so the coarse search still runs at ~75 dpi and the fine search at ~150 dpi, as upstream's defaults do for 300 dpi; upstream's first reduction (`reduce(1)` of the 300 dpi binary image) is replaced by the area-averaged working image plus Otsu. Measured on 40 synthetic 300 dpi pages with known skew in ±6.5°: mean error 0.013°, max 0.032° (pre-port code: 0.088° / 0.498°). (2) Vectorised: the sheared row histogram is built from column-block histograms (NumPy) instead of shifting packed bit columns; `tests/test_deskew.py` checks it against a literal transcription of `vShearFromTo` + `calcScore`. (3) On an exact score tie in the fine search upstream returns the lower end of the bracket, which is 0.5° off for a perfectly straight page; the port returns the middle of the bracket. (4) The confidence formula, `GOOD_CONFIDENCE` (2.0) and `LOW_SCORE` (1000) are upstream's, including that the fine score is compared with the mean coarse score. |
| 1 Deskew (UI) | `filters/deskew/ImageView.cpp`, `OptionsWidget.cpp`, `ApplyDialog.cpp` | `frontend/deskew-view.ts`, `frontend/scantailor-workspace.ts` | reimplemented | Same grid (20 px, blue centre cross), arcs and two handles, Ctrl/Ctrl+Shift wheel steps (0.1° / 0.05°), ±45° manual range, auto/manual modes, commit on release. Divergences: (1) the angle field shows **positive = clockwise** (owner requirement in #195 §6.1); upstream's spin box shows the opposite sign. Stored angles were converted by migration 0052. (2) Handles sit on a circle through the page edges (#195 §6.1) rather than on arcs fitted to the widget. (3) The grid can be limited to "only while rotating" (#195 §6.1); upstream always draws it. (4) Field and wheel changes are saved 0.6 s after the last change instead of on every spin-box step, to avoid a request per notch. (5) Apply to… adds "every other page" (from upstream's Page Layout dialog) and an option to keep pages with manual settings. |
| 2 Select Content | `filters/select_content/ContentBoxFinder.{h,cpp}`, `filters/select_content/Task.cpp`, `PhysSizeCalc.cpp`, `imageproc/{Binarize (binarizeWolf),SEDM,InfluenceMap,SlicedHistogram,ConnCompEraser,Morphology}.cpp` | `core/content_box.py`, `core/binary_image.py`, `core/scantailor.py` (`_run_select_content`, `_detect_content`, `_page_outline`) | direct port | (1) `InfluenceMap` (splitting garbage into horizontal and vertical) is replaced by "nearest seed of the same 8-connected garbage region" by straight Euclidean distance; they differ only where a region bends. (2) `inPlaceRemoveAreasTouchingBorders` seeds from all four borders; upstream's pointer arithmetic seeds row h-2 instead of the last row and misses the last two rows of the side columns. (3) Distance maps (SEDM) use OpenCV's exact Euclidean transform; same values. (4) The page outline that upstream gets as `preCropArea` is the original rectangle mapped through the recorded upstream ops. (5) The stage input is the ~150 dpi working image, rescaled to exactly 150 dpi when it differs. (6) An empty content box (no content) keeps the whole page for now with `content_found: false`; Margins (step 7) will treat such pages as upstream does. (7) Mode "disabled" (whole page) comes from Scan Tailor Advanced, as #195 §6.2 asks; upstream has only auto/manual plus "Remove content box". Measured: 0.67–0.77 s per page on a developer machine, **over the 300 ms budget** (see the PR). |
| 2 Select Content (UI) | `filters/select_content/ImageView.cpp`, `OptionsWidget.cpp`, `ApplyDialog.cpp`, `PhysSizeCalc.cpp` | `frontend/content-box-view.ts`, `frontend/scantailor-workspace.ts` | reimplemented | Same box, edge and corner drags (10 px minimum, kept inside the image), commit on release, auto/manual modes, size in mm. Divergences: no context menu ("Create/Remove Content Box"); "Disabled" replaces "Remove" and keeps the whole page; the size is shown in the options instead of a status bar. |
| Despeckle | `Despeckle.{h,cpp}` | `core/despeckle.py` | direct port | Used by Select Content now and by Output later. (1) The Voronoi diagram is the exact anisotropic Euclidean one (SciPy) instead of upstream's 4-neighbour vector propagation. (2) Upstream's second Voronoi pass is left out: it marks every pixel of the other components with a sentinel distance, so the connections it adds to them come out at about 2^31 and can never attach; it cannot change the result in practice. |
| MaxWhitespaceFinder | `imageproc/MaxWhitespaceFinder.{h,cpp}` | `core/max_whitespace.py` | direct port | Ties between equally good regions are broken by insertion order instead of `std::priority_queue` heap order. |
| 3 Dewarping (model) | `dewarping/CylindricalSurfaceDewarper.cpp`, `DistortionModel.cpp`, `Curve.cpp`, `RasterDewarper.cpp`, `math/{XSpline,ArcLengthMapper,PolylineIntersector,ToLineProjector}.cpp`, `math/HomographicTransform.h`, `filters/output/OutputGenerator.cpp` (`dewarp`, `setupTrivialDistortionModel`), `filters/output/DepthPerception.cpp` | `core/cylindrical.py`, `core/scan_transform.py` (`cylindrical` op), `core/scantailor.py` (`_run_cylindrical_dewarp`) | direct port (#195 step 6a) | (1) Upstream dewarps inside Output after page layout; here Dewarping is its own stage before Margins (owner decision). The model domain uses the stage input as the content rectangle and the output keeps the input's size, so Margins measures the dewarped content. (2) `RasterDewarper` area-maps each output pixel; the port samples bilinearly with `cv2.remap` (upstream's `INTERP_BILLINEAR` variant). (3) The search hints of `PolylineIntersector` and `ArcLengthMapper` are not kept between calls; same results except where a line crosses an S-shaped curve twice. (4) A trivial model (no text lines) records the identity op instead of resampling the page through a flat cylinder, which is the same image. |
| 3 Dewarping (UI) | `filters/output/DewarpingView.cpp`, `interaction/InteractiveXSpline.cpp`, `DewarpingMode.cpp`, `ChangeDewarpingDialog.cpp` | `frontend/dewarp-view.ts`, `frontend/scantailor-workspace.ts` | reimplemented (#195 step 6a) | Same grid, margins, red control points, end-point rotate-and-scale, click-to-insert, Del/D to delete, commit on release (auto becomes manual), off/auto/manual, depth perception slider. The model is mirrored in TypeScript for the live grid; a shared fixture checks it against the Python port. Divergences: (1) Apply to uses the editor's common scope list (it includes upstream's "this page", "all pages", "this page and the following" and "selected pages"). (2) The depth perception is a per-page setting saved with the curves, not a separate dialog. |
| 3 Dewarping (auto) | `dewarping/{TextLineTracer,TextLineRefiner,TopBottomEdgeTracer,DetectVertContentBounds,DistortionModelBuilder,TowardsLineTracer}.cpp`, `math/spfit/*`, `math/adiff/*`, `imageproc/{SavGolFilter,PolynomialLine,LocalMinMaxGeneric,GaussBlur,Sobel}.*` | `core/curvature.py` (pre-port detector, used as a bridge) | pre-port (step 6b ports it) | Auto mode takes the topmost and bottommost baselines of the pre-port detector, extends them straight to the page edges and places upstream's five-point splines on them at equal x steps instead of fitting them (`spfit`). |
| 4 Margins | `filters/page_layout/*` (`Utils.cpp`, `Settings.cpp`, `Alignment.cpp`) | `core/scantailor.py` (`_run_margins`) | pre-port | Pads each page on its own. No aggregate hard size or soft margins across the job, so with default settings it is a no-op. |
| 5 Output | `filters/output/*`, `imageproc/{Binarize,BinaryThreshold,GaussBlur,PolynomialSurface,Scale,SEDM}.cpp` | `core/scantailor.py` (`_run_output`, `_binarize_with_cv2`), `core/output_zones.py` | pre-port | OpenCV threshold modes; the "fox" and "wolf" labels do not match their algorithms. No despeckling. Picture and fill zones are only partly implemented. |
| Pipeline architecture | `ImageTransformation.h`, each filter's `Task.cpp`, `filters/output/OutputGenerator.cpp`, `ThumbnailPixmapCache.cpp` | `core/scan_transform.py`, `core/scan_images.py`, `core/scantailor.py` (`StageInput`, `_stage_input`, `_write_stage_artifacts`) | reimplemented (#195 step 2) | Same principle (stages pass geometry, Output renders the original once) but not a port: ops are a JSON list composed in order rather than one `ImageTransformation` with a pre-crop area and a 2x2 matrix; Dewarping is an op of its own before Margins. Working images are stored files (one per page version) rather than an in-memory thumbnail cache. |

### Removed upstream stages

| Upstream filter | Decision |
|---|---|
| `filters/fix_orientation` | Not implemented: the scanner delivers upright pages (#195). |
| `filters/page_split` | Removed in #195 together with `ScanJob.capture_layout`: the scanner delivers single pages. The scanner API still accepts `capture_layout: single_page` for compatibility and rejects `spread`. |

## Dependency licence check

Checked against the installed package metadata when this file was created. All are
compatible with distribution under GPL-3.0-or-later.

| Dependency | Licence | Note |
|---|---|---|
| Django, djangorestframework, dj-database-url, pypdf, uvicorn, numpy, scipy, scikit-image, httpx | BSD family | compatible (scipy declared directly since #195 step 5) |
| rq | BSD-2-Clause | compatible |
| Pillow | MIT-CMU (HPND) | compatible |
| opencv-python-headless | Apache-2.0 | compatible with GPL-3.0 (not with GPL-2.0-only) |
| pypdfium2 | Apache-2.0 or BSD-3-Clause, plus PDFium third-party notices | compatible |
| prometheus-client | Apache-2.0 and BSD-2-Clause | compatible |
| rapidfuzz, redis, pydantic, pydantic-settings, beautifulsoup4, charset-normalizer, nh3, django-stubs-ext | MIT | compatible |
| structlog | MIT or Apache-2.0 | compatible |
| rarfile | ISC | compatible |
| defusedxml | PSF-2.0 | compatible |
| isbnlib, ldap3, img2pdf | LGPL-3.0 | compatible |
| psycopg | LGPL-3.0-only | compatible |
| pycountry | LGPL-2.1-only | compatible: LGPL-2.1 §3 allows applying GPL v2 or later, so GPL-3 works |
| interactjs (frontend) | MIT | compatible |
| pdfjs-dist (frontend) | Apache-2.0 | compatible |

Candidates raised in #195, not yet adopted: `pyvips` (LGPL-2.1-or-later, compatible;
packaging still to be checked) and Numba (BSD-2-Clause, compatible).

GPL-3.0 is not the AGPL. Serving the application over a network does not by itself
require publishing source. This repository is public anyway.

*This is a compliance checklist, not legal advice.*
