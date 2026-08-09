# Sprint001 ScanTailor-Web Survey

## Stage mapping notes

### 1) Page Split

- Input: raw or near-raw photo scans.
- Practical baseline options: OpenCV contour/connected-component heuristics + manual overrides.
- Risk: split confidence varies by source noise; manual override is mandatory.

### 2) Deskew

- Input: single-page images.
- Practical options: Hough lines / projection profile heuristics plus OCR text-line hints from future OCR modules.
- Must support manual correction and reset.

### 3) Select Content

- Input: deskewed images.
- Practical options: adaptive threshold + edge-density bounding candidates.
- Requires rectangle editing UI and page-level overrides.

4) Margins

- Input: content rectangles.
- Practical options: margin math plus auto-clamp against image bounds.
- Requires visual preview overlays and per-page/batch defaults.

### 5) Dewarping

- Input: content-corrected page.
- Practical options: OpenCV grid models and/or projective transformations.
- Current implementation uses an OpenCV-backed projective transform path with NumPy geometry helpers when available, and degrades to deterministic copy-through when dependencies or detection fail.
- Keep interface modular so algorithm can be swapped once better library support is selected.

### 6) Output/Binarization

- Input: dewarped pages.
- Practical options:
  - Otsu
  - Sauvola
  - Wolf
  - Niblack-derived / local-threshold equivalents
  - picture/text split heuristics from color/connected components
- Start with proven OpenCV/skimage building blocks, avoid bespoke implementations unless necessary.
- Current implementation is configurable across `otsu`, `sauvola`, `wolf`, `fox`, `window`, and `grad`,
  with manual text/picture zone overrides and fallback behavior when optional CV dependencies are unavailable.

## Data model requirement (all stages)

Persist per (job, page, stage):
- input artifact id
- algorithm name/version
- parameters
- output artifact id
- status
- timestamp / processing worker id
- invalidation lineage

Upstream changes invalidate downstream artifacts.

## Sprint 013 implementation status

Workspace and orchestration surface:

- `GET /scan/jobs/<job_id>/workspace/` returns page list with per-stage metadata.
- `GET /scan/jobs/<job_id>/pages/<page_id>/stages/` returns ordered stage state for a page.
- `GET|POST /scan/jobs/<job_id>/pages/<page_id>/stages/<stage>/` reads or updates one stage.
  - `POST` accepts optimistic-concurrency `expected_revision` and triggers async-style stage processing.
- `GET /scan/jobs/<job_id>/pages/<page_id>/stages/<stage>/preview/` serves generated preview artifacts.
- `GET /stages/<job_id>/` polls stage-processing job status.

Current behavior in Sprint 013 is intentionally placeholder for downstream image transforms:
processing copies artifacts through stable names and computes hashes so the dependency graph,
revisioning, and state lifecycle are exercised without adding image-algorithm coupling too early.
