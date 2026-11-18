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

**Status in this PR (#195 step 1):** nothing has been ported yet. The current stage code
in `src/ebook_server/core/scantailor.py` was written from a *description* of Scan Tailor.
It is listed here as `pre-port` so each later PR can replace a row with the real port.

| Web stage | Upstream files | Current module | Status | Known divergences |
|---|---|---|---|---|
| 1 Deskew | `filters/deskew/*`, `imageproc/SkewFinder.cpp`, `imageproc/Transform.cpp` | `core/scantailor.py` (`_estimate_deskew_angle`, `_run_deskew`) | pre-port | Grey 900 px thumbnail and row-sum score in 1° steps, not SkewFinder's binarised coarse/fine search with 0.1° accuracy. Small angles are discarded. |
| 2 Select Content | `filters/select_content/*`, `ContentBoxFinder.cpp`, `PhysSizeCalc.cpp`, `imageproc/{SeedFill,ConnCompEraser,Morphology,MaxWhitespaceFinder}.cpp` | `core/scantailor.py` (`_detect_content_box`, `_run_select_content`) | pre-port | Otsu ink mask, dilation and speck filter. No shadow detection, whitespace finding or text/garbage separation, and no 150 dpi working scale. |
| 3 Dewarping | `dewarping/*`, `filters/output/DewarpingView.cpp`, `DewarpingMode.cpp`, `imageproc/{HoughLineDetector,RastLineFinder,PolynomialLine,SavGolFilter,LeastSquaresFit}.cpp` | `core/scantailor.py` (`_run_perspective_dewarp`, `_run_curvature_dewarp`), `core/curvature.py` | pre-port | 4-point perspective crop plus a baseline-curvature model. Not the cylindrical-surface dewarper. Upstream runs dewarping inside Output; here it is its own stage before Margins (owner decision in #195). |
| 4 Margins | `filters/page_layout/*` (`Utils.cpp`, `Settings.cpp`, `Alignment.cpp`) | `core/scantailor.py` (`_run_margins`) | pre-port | Pads each page on its own. No aggregate hard size or soft margins across the job, so with default settings it is a no-op. |
| 5 Output | `filters/output/*`, `imageproc/{Binarize,BinaryThreshold,GaussBlur,PolynomialSurface,Scale,SEDM}.cpp` | `core/scantailor.py` (`_run_output`, `_binarize_with_cv2`), `core/output_zones.py` | pre-port | OpenCV threshold modes; the "fox" and "wolf" labels do not match their algorithms. No despeckling. Picture and fill zones are only partly implemented. |

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
| Django, djangorestframework, dj-database-url, pypdf, uvicorn, numpy, scikit-image, httpx | BSD family | compatible |
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
