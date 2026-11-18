# ScanTailor-Web

ScanTailor-Web is a multi-user subsystem within the Django modular monolith. Its
database state is authoritative; web clients do not coordinate directly with
workers or with one another. Originals and prior scan-page versions are retained,
while every processing stage writes a new derived artifact and provenance.

## Processing pipeline

Each `(scan job, page, stage)` has a persistent `ScanTailorStageState` containing
its input lineage, parameters, algorithm/version, revision, worker job, output and
preview identifiers, checksums, timestamps, and status. The ordered stages are:

1. Page Split — automatic gutter detection, manual draggable split line, or a
   single-page pass-through. In a `single_page` job (the default, see below) it
   completes on its own as a recorded "no split" pass-through.
2. Deskew — projection-profile estimation with configurable search bounds or a
   manual angle.
3. Select Content — automatic content detection or a draggable/resizable content
   rectangle.
4. Margins — per-side/canvas padding and horizontal/vertical alignment.
5. Dewarping — OpenCV projective correction using detected or draggable control
   points, with an explicit bypass and deterministic failure behavior.
6. Output/Binarization — Otsu, Sauvola, Wolf, Fox, Window, Grad, and copy modes,
   plus draggable text/picture zones with independent algorithms.

The current dewarping stage corrects perspective on flat pages; it does not
correct book-spine curvature. The four control points only define the correction
(which quadrilateral becomes an upright rectangle); the output canvas holds the
whole transformed input, so content outside the points and the Margins padding
are kept (`opencv-v2`). A near-degenerate quad (a corner at or beyond the
vanishing line, a canvas reaching more than one input size past the quad, or a
canvas over twice the input's pixel count) is not rendered: the page is passed
through unchanged with `dewarp_correction="rejected_degenerate_quad"` and the
reason in `dewarp_warning`. Every run records `dewarp_applied`
(`perspective` or `passthrough`) and resets the warp-geometry parameters it did
not produce, so a passthrough never leaves an earlier warp looking current. In `auto` mode (`opencv-v3`) the quad comes from the
text: the fitted topmost and bottommost text lines give the top/bottom edges and
robust fits through the outer line ends give the sides (a ragged side mirrors the
aligned one). A page outline contour is the fallback. An already-upright page is
passed through without resampling (`dewarp_correction="none_needed"`). When
nothing is found, the result records `dewarp_mode="auto_fallback"` with a
`dewarp_warning`. Curvature correction remains Sprint020 work.

### Output zones (`opencv-zones-v2`)

`zone_mode="auto"` detects multiple continuous-tone/halftone picture regions and
groups the remaining glyphs into text blocks using OpenCV morphology and connected
components. Detection runs on an image at most 1200 pixels on its longest edge,
then maps rectangles back to source coordinates. The detector is a heuristic,
not semantic recognition: inspect illustrated pages before finishing them.

Text regions default to Sauvola; picture regions default to `copy`. Picture copy
keeps the normalized source pixels, including RGB colours even if `keep_color`
is false. It never converts a photograph to grayscale and then merely relabels
the result RGB. `keep_color` protects picture and unclassified source pixels;
only text regions are binarized, and the composed output stays RGB. It overrides
the picture algorithm to `copy`, which is recorded in the result. Pictures win
overlapping text regions. Outside regions, the selected output algorithm runs;
`zone_mode="off"` deliberately uses that algorithm over the whole page.

The worker records `resolved_zones` (`kind`, `box`, `source`) and the detector
version. Choose **Edit detected regions** to make an explicit manual list. The
editor supports adding, deleting, changing type, dragging/resizing, and numeric
coordinates for up to 64 regions. Manual `zones` are recorded as human choices
and survive batch application with **Preserve manual exceptions** enabled.
Old `text_box` and `picture_box` parameters remain readable; a rerun records the
equivalent list. Automatic runs redetect from the current input instead of
reusing geometry after an upstream change. Derived detection metadata is never
submitted as editable parameters.

Layout detection allocates arrays only at reduced resolution. Thresholding uses
768-pixel tiles with a local-window halo capped at 253 pixels; Otsu retains a
global threshold. Full-size source/output Pillow images remain necessary, but
there are no page-sized float or component-label arrays. Large local-statistic
regions use tile-local contrast estimates. On a detection or processing error,
the normalized source is copied and the result records `output_applied="copy_fallback"`
and `output_warning`. `output_mode="copy"` bypasses zoning and thresholding.
Successful execution alone does not establish output quality.

CI runs a real Tesseract English acceptance case on a synthetic illustrated page:
text character-error rate must be at most 5%, no worse than whole-page output by
more than one percentage point, and picture pixel error must be below 15% of the
whole-page result. The local Tesseract 5.5.2 run measured 0% CER for both paths,
with picture detail retained. This is a repeatable layout regression, not proof
of OCR quality on photographed books or the hackerspace AI server. The six output
modes use OpenCV/NumPy statistics; Fox is a Niblack-style threshold with a noise
floor and Wolf uses the Wolf–Jolion local-statistics formula, not a claim of
bit-identical ScanTailor Advanced output.

Pillow, NumPy, OpenCV, and scikit-image provide the image operations. Processing
runs as durable worker jobs. Successful stages record input/output dimensions so
browser geometry can be mapped between full-resolution source coordinates and a
scaled display. Stage previews are bounded to 960 pixels; the first stage still
uses the preserved capture as its input because it has no predecessor preview.

Changing a stage increments its optimistic-concurrency revision, clears its prior
error, queues processing, and invalidates every downstream stage. A replacement
scan page similarly invalidates dependent artifacts without deleting the old page
version.

## Page Split and the capture layout (decision for #55)

**Question.** Is Page Split needed for this project's scanner, which captures
every page as its own image?

**Answer.** The two-page-spread case does not occur with the project's current
scanner. The stage stays in the pipeline, but for single-page captures it is a
recorded passthrough that needs no work from anyone.

Evidence about the hardware:

- **Real capture.** The owner manually tested a real 365-page self-scan (#55).
  Every page arrived as its own image file. None of the images contained two
  facing pages.
- **The scanner protocol.** `docs/scanner-protocol.md` takes one image per
  `(logical_sequence_number, side)`. A two-page opening is sent as two uploads,
  `side=left` and `side=right`. Each upload is one page, not one spread
  photographed twice.
- **The reference client.** `scripts/scanner_client_example.py --spreads` pairs
  separate image files as left and right. It never sends the same spread image
  twice.
- **Project documents.** No Sprint document or status note describes a camera or
  planned hardware variant that photographs both pages at once. Sprint010 keeps
  the protocol hardware-neutral. The only spread images in the repository are
  synthetic test fixtures, such as the 6000×4000 smoke-test spreads in
  `status-2026-11-10-first-hardware-pass.md`.
- **Risk of running the splitter anyway.** Running gutter detection on
  single-page input is not harmless. A landscape `left`/`right` image is cut at
  the detected gutter or, failing that, at the centre. That would halve a page
  photographed sideways without EXIF orientation.

AGENTS.md requires Page Split as the first of six stages. Spread capture is also
realistic for other setups: a single overhead camera, a flatbed, or
already-photographed spreads. So the stage is kept, and it is made automatic
where it has nothing to do:

- `ScanJob.capture_layout` is either `single_page` (one page per image) or
  `spread` (an image may hold two facing pages).
- At job creation the layout is chosen in this order:
  1. an explicit `capture_layout` field on `POST /scan/jobs/`;
  2. otherwise, `spread` if the job's `scanner_id` is listed in
     `BOOK_SCANNER_SCAN_SPREAD_SCANNER_IDS`;
  3. otherwise, `BOOK_SCANNER_SCAN_DEFAULT_CAPTURE_LAYOUT` (default
     `single_page`).
- Page `side` values do not imply spreads, because `left` and `right` are
  separate single-page images.
- Jobs that existed before migration 0022 were set to `spread`, so their Page
  Split behaviour did not change. To treat such a job as single-page, batch-apply
  Page Split with `split_mode: "none"` to all pages.

**`single_page` jobs.**

- Every new page version (an upload or a rescan replacement) queues a Page Split
  background job with `split_mode: "none"`.
- The job runs the shared passthrough helper from #54. A normalised PNG is
  byte-copied and other inputs are normalised once. No gutter detection runs.
- The stage still produces its own output artifact, checksum, preview and
  revision. Downstream inputs and invalidation therefore behave exactly as
  after a real split.
- The run records:
  - `algorithm = single_page_passthrough`;
  - `algorithm_version = splitter-v2`;
  - the parameters `resolved_split_mode: "none"`, `split_skipped: true`,
    `split_skipped_reason` and `capture_layout`.
- The workspace shows the stage as **skipped · single page**. The API
  serializes it with `auto_skipped: true`.
- The page thumbnail keeps using the full-size capture, because the passthrough
  output is the same image.
- To split one page anyway, switch its split mode to `manual` or `auto`. That
  is an ordinary stage edit: it bumps the revision, re-runs the stage and
  invalidates every downstream stage.
- A page switched this way counts as a manual exception. A later rescan does not
  reset it to "none".
- If the background job cannot be queued, the upload still succeeds and the
  stage is marked failed with the reason.

**`spread` jobs** keep the previous behaviour. Page Split runs only when someone
starts it, and its default mode is `auto` gutter detection.

## Browser workspace

The server-rendered page remains usable without JavaScript through individual Run
forms. The focused TypeScript component adds:

- a six-stage navigation surface and per-page editor selection;
- direct split-line, content-box, dewarp-point, and output-zone manipulation;
- typed controls for deskew, margins, output algorithms, and threshold settings;
- selected-page or all-page batch application;
- preservation of page-level manual exceptions by default;
- revision-conflict detection that reloads current values instead of overwriting;
- live stage status, error, revision, and preview availability updates over SSE,
  with compatibility polling when `EventSource` is unavailable.

Interact.js supplies drag/resize behavior. TypeScript, Vite, Vitest, and jsdom
provide strict compilation, production bundling, and coordinate/parameter tests.
The browser sends only editable inputs back to Django; dimensions, resolved
geometry, hashes, and other worker-generated provenance are never echoed as user
parameters.

## HTTP contracts

- `GET /scan/jobs/<job_id>/workspace/` — HTML workspace or JSON page/stage state.
- `GET /scan/jobs/<job_id>/workspace/events/` — authenticated SSE state stream.
- `GET /scan/jobs/<job_id>/pages/<page_id>/stages/` — ordered stage state.
- `GET|POST /scan/jobs/<job_id>/pages/<page_id>/stages/<stage>/` — read/update one
  stage; POST accepts `expected_revision` and returns `409` on a conflict.
- `POST /scan/jobs/<job_id>/stages/<stage>/batch/` — apply parameters to selected
  page IDs or all pages, optionally preserving manual exceptions.
- `GET /scan/jobs/<job_id>/pages/<page_id>/stages/<stage>/preview/` — stage output
  preview; `?input=true` resolves the correct predecessor/input artifact.

All routes require authentication. Mutations also require an active local profile
and an editable scan-job state.

## Frontend development

Node 22.12 or newer is required:

```bash
npm ci
npm run check
npm test
npm run build
```

The generated ES module is committed under
`src/ebook_server/core/static/core/dist/` so Python packaging and collectstatic do
not require Node at deployment time. CI rebuilds it and rejects an uncommitted
bundle difference.

## Deployment notes

Serve Django through ASGI for asynchronous SSE connections. The endpoint sends
`X-Accel-Buffering: no`; reverse proxies must also permit streaming responses and
use an idle timeout longer than the configured stream lifetime. The defaults poll
persistent state once per second and reconnect after 45 iterations. Configure
them with:

- `BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS`
- `BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_MAX_EVENTS`

The infrastructure suite verifies authentication, prompt initial and updated
events, unbuffered Nginx proxying, and client disconnect recovery against a real
Uvicorn/PostgreSQL deployment boundary. The browser suite also exercises concurrent
editors and a synthetic scan-to-library workflow against an HTTP OCR contract
fixture. Recognition quality against the hackerspace AI server remains a separate
site-connected acceptance requirement.
