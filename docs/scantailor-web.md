# ScanTailor-Web

ScanTailor-Web is a multi-user subsystem within the Django modular monolith. Its
database state is authoritative; web clients do not coordinate directly with
workers or with one another. Originals and prior scan-page versions are retained,
while every processing stage writes a new derived artifact and provenance.

## Processing pipeline

Each `(scan job, page, stage)` has a persistent `ScanTailorStageState` containing
its input lineage, parameters, algorithm/version, revision, worker job, output and
preview identifiers, checksums, timestamps, and status. The ordered stages are
(#195):

1. Deskew — a port of upstream `SkewFinder` (#195 step 4, `core/deskew.py`): Otsu
   binarisation, removal of horizontal shadows, a coarse search in 1° steps over
   ±7° and a binary search to 0.1°. Below upstream's confidence threshold (2.0) the
   page is left unrotated and the editor says so. Manual mode sets the angle
   directly (±45°). Angles are degrees, **positive = clockwise**. It reads the
   page's active raw version directly.
2. Select Content — a port of upstream `ContentBoxFinder` (#195 step 5,
   `core/content_box.py`): at 150 dpi, Wolf binarisation, removal of page-edge
   shadows, despeckling, content blocks from the largest whitespace rectangles, a
   text mask, and side-by-side trimming of strips that lie closer to the shadows than
   to the text. Modes: auto, manual (the box as drawn) and disabled (the whole page).
   A page without content keeps the whole page and says so. The content size is also
   recorded in millimetres (upstream `PhysSizeCalc`).
3. Dewarping — upstream Scan Tailor's cylindrical-surface model (#195 step 6,
   `core/cylindrical.py`): a top and a bottom X-spline follow the text block, and
   the page between them is unrolled as a cylinder (`CylindricalSurfaceDewarper`,
   with upstream's depth perception, 1 to 3, default 2). Modes are off, auto and
   manual. The output keeps the input's size. Auto detection is not ported yet
   (step 6b): until then it takes the top and bottom baselines found by the earlier
   baseline detector, and a page without text lines stays unchanged and says so.
4. Margins — per-side/canvas padding and horizontal/vertical alignment.
5. Output/Binarization — Otsu, Sauvola, Wolf, Fox, Window, Grad, and copy modes,
   plus draggable text/picture zones with independent algorithms.

There is no Fix Orientation and no Page Split stage: the scanner delivers upright
single pages (owner decision in #195, superseding #55). Dewarping runs before
Margins, unlike upstream Scan Tailor where it is part of Output; Margins therefore
sees the dewarped content.

Issue #195 replaces these stages with ports of the upstream Scan Tailor algorithms,
one stage per PR. Until a stage is ported it keeps the behaviour described here;
[`third-party/scantailor-port.md`](third-party/scantailor-port.md) records the
status and divergences of each stage.

The default dewarping model corrects perspective on flat pages; it does not
correct book-spine curvature. An opt-in baseline curvature model is described below.
The four perspective control points only define the correction
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
`dewarp_warning`.

### Opt-in baseline curvature (`baseline-curvature-v1`)

Perspective remains the default. Selecting **Dewarp model: curvature** opts one
page into `baseline_curvature`; a batch applies it only when explicitly selected.
This model straightens curved text lines with a vertical displacement field. It
does not reconstruct a 3D page surface or correct horizontal foreshortening.
Inspect the Result preview before finishing. Real photographed-book acceptance
remains open; [the recorded measurements](measurements/curvature-acceptance.md)
are synthetic.

Automatic estimation tracks text bands in 25 vertical strips of an image reduced
to at most 1200 pixels on its longest edge, robustly fits quadratic curves, and
records nine source-coordinate control points per baseline in `curvature_curves`.
Between baselines, the renderer interpolates vertical displacement using OpenCV
`remap`. Output retains the input width and adds paper-colour padding above/below
to retain the input, including content outside the baselines. Corrections below
two pixels pass through without resampling.

**Edit detected baselines** copies geometry into authored `dewarp_curves` and
switches to manual mode. Select a baseline to drag its points or edit numeric y
coordinates; add/remove baselines as needed. Only authored geometry travels back
to the server. Manual geometry survives batch application with Preserve manual
exceptions and produces identical output when replayed. Changing to perspective
uses the existing quad editor and algorithm again.

Manual input is bounded to 2–32 non-crossing baselines with 3–17 finite points
each, ordered in x and inside the image. Baselines must stay at least two pixels
apart. Displacement is limited to the smaller of 512 pixels and 15% of the input
height. Unsafe geometry or unreliable estimation keeps the normalized input and
records a visible `dewarp_warning` and `dewarp_applied="passthrough"`. Bypass keeps
the existing normalized-PNG byte-copy policy. Every run clears stale derived
geometry; selected model/version and actual applied transform are separate.

Remap coordinate arrays are at most 512×512, with bounded source crops; there are
no full-resolution float maps. Paper estimation and preview generation resize
before making additional copies. Full source/output images still consume memory
proportional to page size. Tests exercise actual decoding, remapping, PNG output
and previews on 24-megapixel grayscale/RGB pages.

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

The editor's **Input** and **Result** buttons distinguish the editable source from
the processed image. A successful run selects Result automatically, with geometry
handles hidden; switch to Input to adjust geometry. Worker fallback warnings appear
beside the preview even when the stage completes successfully. Updates work through
both live events and compatibility polling.

Margins record computed dimensions as `resolved_canvas_width` and
`resolved_canvas_height`; `canvas_width` and `canvas_height` are reserved for explicit
fixed-canvas requests. Reducing ordinary padding therefore shrinks the result again.
The editor also recognizes legacy computed canvases equal to the input plus recorded
padding, rather than silently resubmitting them as fixed dimensions.

## Pipeline architecture: transforms, not images (#195 step 2)

Upstream Scan Tailor passes an `ImageTransformation` and parameters from filter to
filter and renders full-resolution pixels only in Output. ScanTailor-Web now does the
same:

- **Working image, made once.** When a page version is committed, a background job
  (`scan_images.generate_page_image_job`) decodes the scan once and stores a
  `ScanPageImage`: a lossless working image at about 150 dpi (long edge at least
  1600 px and at most 2400 px, never upscaled) and a 400 px JPEG thumbnail. Versions
  are immutable, so these files never change. A stage that runs before the job
  finished builds the image itself. The editor's Deskew input preview never decodes
  the scan in the request: when the image is missing it queues the job (once; a
  short-lived Redis marker deduplicates repeated requests), looks again (an inline
  queue or a fast worker may already be done) and otherwise answers
  `503 input preview not ready` with `Retry-After: 2`. The editor then reloads the
  image with backoff (1, 2, 4, then 8 s; about a minute in total) and shows it as
  soon as it is ready. If its diagnostic request finds the image already available,
  it reloads it at once.
- **Stages 1-4 record a transform op.** Deskew, Select Content, Dewarping and Margins
  run detection on their working-resolution input and store one op in
  `parameters["transform"]`, in the full-resolution coordinates of their input
  (`rotate`, `crop`, `perspective`, `curvature`, `pad` or `identity`; see
  `core/scan_transform.py`). All other parameters (content box, quad, curves,
  margins) stay in full-resolution coordinates too, and every run records
  `input_width/height` and `output_width/height` at full resolution, computed from
  the ops without rendering anything.
- **Working-resolution artifacts.** Each of these stages stores its output at working
  resolution (`work.png`, PNG compression level 1), which is the next stage's input,
  and a JPEG preview (`preview.jpg`). They never decode or write the full scan.
- **Output renders once.** The Output stage composes every upstream op and renders
  the untouched original once at full resolution, then binarises it
  (`output.png`, with the scan's DPI) and writes a working-resolution `preview.png`.
  Auto-detected zones are found on the working image and stored at full resolution.
- **Scales agree.** An op applied at working resolution previews what Output renders
  (`tests/test_scan_transform.py` checks every op against a downscaled full render).
  Scaling uses pixel-centre coordinates.

Migration 0050 invalidates every processed stage of jobs that can still be
processed (open, capturing, paused, raw scan), because stages processed before this
change have no recorded op; they re-run once. Finalizing, complete and cancelled
jobs reject stage re-runs, so their results are kept as they are. The migration
does not create `ScanPageImage` rows (that would decode every scan); existing pages
get them on first open, or ahead of time with
`manage.py backfill_scan_page_images` (see `docs/operations.md`). `scripts/benchmark_scanning.py`
times the `page_image` step and each stage on this pipeline.

## Serving and scheduling (#195 step 2b)

- **Immutable artifact URLs.** Page thumbnails and stage previews carry a content
  token (`?v=`): the stage revision and output checksum, or `page-<version>` for the
  working image. A URL with the current token is answered with
  `Cache-Control: private, max-age=31536000, immutable`; any other request
  revalidates by ETag (`private, max-age=0, must-revalidate`). The workspace computes thumbnail tokens from rows it already
  loads. With `BOOK_SCANNER_SCAN_ARTIFACT_ACCEL_PREFIX` set, Django only checks the
  session and nginx sends the file (`deploy/nginx/book-scanner.conf`).
- **Delta state for polling.** Without `EventSource` the workspace polls
  `GET /scan/jobs/{id}/workspace/state/?since=<cursor>` every 5 s instead of the whole
  workspace. It returns only stage rows updated since the cursor (minus a 30 s
  overlap, so a late commit is not missed), never writes, and uses the
  `(scan_job, updated_at)` index. The SSE stream was already incremental.
- **Lazy thumbnails.** Page thumbnails load lazily; the virtualised strip follows in
  the workspace redesign (step 3).
- **Invalidation** is one `UPDATE` per edit instead of one save per stage row.
- **Priority.** A single-page edit is queued at the front of the RQ queue; "apply to
  pages" is dispatched as jobs of eight pages at the back, so the page being edited
  never waits behind a batch (upstream `ProcessingTaskQueue`). Every page keeps its
  own `ProcessingJob` and revision check.
- **Worker pool.** Workers keep OpenCV and BLAS single-threaded; run one per core
  (`deploy/systemd/README.md`).
- **No in-worker image cache.** RQ forks a fresh work horse per job, so an
  in-process LRU (upstream `ThumbnailPixmapCache`) would always be empty. The
  working images make a stage's decode cheap instead (about 10 ms in
  `scripts/benchmark_scanning.py`).

## Page Split removed (#195)

Page Split and `ScanJob.capture_layout` (#55) were removed in #195. Migration 0049
deletes existing `page_split` stage rows and relinks the remaining stages in the
new order. On jobs that still accept processing (open, capturing, paused, raw
scan) it marks every stage that had been queued, run or failed as invalidated
with a new revision, so those jobs re-run once. Finalizing, complete and
cancelled jobs keep their stage statuses, revisions and outputs: the lifecycle
refuses stage re-runs in those states, and the outputs are the history behind
the published book. When a replacement page reopens a complete job (raw scan,
re-finish needed), that page is invalidated as usual and the re-finish reuses
the other pages' recorded outputs. Raw page versions are untouched. The
migration rehearsal's integrity check expects exactly the deleted `page_split`
rows to disappear.
Job creation (`POST /scan/jobs/` and `PUT /api/scanner/v1/jobs/{id}`) still accepts
`capture_layout: single_page` and ignores it, and rejects `spread`.

## Browser workspace

Two pages (#195 step 3):

- **Job overview** (`/scan/jobs/{id}/workspace/`): metadata, identification,
  uploads, rescans, finishing, and one card per page with server-rendered stage
  statuses and Run forms, so it works without JavaScript. "Open ScanTailor editor"
  and each stage's "Edit" link open the editor (`?page=<id>&stage=<stage>`).
- **Editor** (`/scan/jobs/{id}/editor/`): upstream Scan Tailor's main window. It
  fills the browser viewport (no site header, no page scrolling) with three columns:
  - *left*: the numbered stage list (1 Deskew … 5 Output) with a ▶ button per stage
    that runs it for every page with each page's own settings (upstream "Launch
    batch processing"), the options of the selected stage with "Save and run" and
    "Apply to…" (selected pages, all pages, this page only, this page and the
    following ones, or every other page counted from this one, optionally keeping
    pages with manual settings), and a
    status bar with the stage's hint (upstream strings where they exist) and the
    live-update state;
  - *middle*: the preview canvas. The wheel zooms around the cursor, dragging the
    background pans, Fit and 1:1 reset the view. Zoom sets the image's layout size
    and pan only translates it, so overlays and their handles keep working at any
    zoom; handles capture their own pointer events. Ctrl+Wheel is reserved for
    stage gestures (deskew rotation). The image is the stage's working-resolution
    preview (about 150 dpi); full-resolution tiles are not part of this step;
  - *right*: the page strip. It is virtualised (only the rows in view exist in the
    DOM) and its thumbnails load lazily from tokenised, immutable URLs: the current
    stage's output (each stage run writes a 400 px `thumbnail.jpg`), else the last
    completed stage before it, else the page's working-image thumbnail. Under each:
    the page number and file name, and one small numbered icon per stage: grey (not
    started or needs re-run), amber and pulsing (queued or running), green (done),
    blue (done with manual settings), red (failed); the tooltip names the stage and
    the error or invalidation reason. A sort selector offers "Natural order",
    "Order by increasing width" and "Order by increasing height" (content box for
    Select Content, output size otherwise). Click selects a page, Ctrl/Cmd-click
    toggles, Shift-click selects a range; Home, End, PgUp (or Q), PgDown (or W) and
    the arrow keys move between pages, and "Keep current page in view" scrolls the
    strip along (upstream ThumbnailSequence and MainWindow behaviour).

**Deskew** (upstream `deskew::ImageView` and `OptionsWidget`): the input preview turns
live in the browser (a CSS rotation about the page centre) while the angle changes;
nothing is sent per frame. A semi-transparent 20 px grid with a blue cross through the
centre stays fixed while the page turns ("Keep the grid visible" switches it to
"only while rotating"; the choice is remembered per browser). Two round handles on
arcs at the page edges rotate the page by dragging; Ctrl+Wheel turns by 0.1° and
Ctrl+Shift+Wheel by 0.05° per notch; the angle field accepts typing and the mouse
wheel. A change switches the page to Manual and is saved 0.6 s after the last
movement, or when a handle is released, as upstream commits on release. Choosing
Auto discards the manual angle and detects again. The options show what the stage
did, for example "Angle 1.30° clockwise (auto, confidence 12.4)" or "Angle 0.00°,
no change: detection confidence 1.3 is below 2.0". The server applies the same
rotation the browser previews (`scan_transform.rotate(-angle)`); a fixture shared
by `tests/test_deskew.py` and `frontend/editor-shell.test.ts` checks both.

**Select Content** (upstream `select_content::ImageView`): the content box is drawn in
auto and manual mode alike, as a translucent blue rectangle over the stage input.
Dragging an edge moves only that edge and dragging a corner its two edges, by exactly
the pointer movement in page coordinates, whatever the zoom or pan; the box keeps a
minimum of 10 screen pixels and stays on the page. Dragging inside the box pans the
page. Releasing a handle makes the box manual and saves it, as upstream commits on
release. Auto, Manual and Disabled are radio buttons; the options show the content
size, e.g. "Content 151.0 × 233.2 mm (892 × 1377 px, auto)". The page strip can be
sorted by content width or height.

**Dewarping** (upstream `DewarpingView`): the input view shows the model as upstream
draws it, a 30 × 30 blue grid that follows the cylinder, translucent white beyond its
left and right edges, and the red control points of the top and bottom curves. Drag a
middle point to move it; drag an end point to turn and scale its whole curve about the
other end; click a curve to add a point there (and drag it); press Del or D over a
middle point to remove it. The grid follows every movement in the browser (a
TypeScript twin of the model, checked against the server with
`tests/fixtures/dewarp_model_cases.json`); releasing saves the curves and makes an
auto model manual, as upstream does. Off, Auto and Manual are radio buttons; the
depth-perception slider redraws the grid live and saves on release.

The editor also keeps:

- direct content-box, dewarp-point, and output-zone manipulation;
- typed controls for deskew, margins, output algorithms, and threshold settings;
- revision-conflict detection that reloads current values instead of overwriting;
- live stage status, error, revision, and preview availability updates over SSE,
  with delta polling when `EventSource` is unavailable.

Interact.js supplies drag/resize behavior. TypeScript, Vite, Vitest, and jsdom
provide strict compilation, production bundling, and coordinate/parameter tests.
The browser sends only editable inputs back to Django; dimensions, resolved
geometry, hashes, and other worker-generated provenance are never echoed as user
parameters.

## HTTP contracts

- `GET /scan/jobs/<job_id>/workspace/` — HTML workspace or JSON page/stage state.
- `GET /scan/jobs/<job_id>/workspace/events/` — authenticated SSE state stream.
  The first event (including reconnects) contains all stages; later events contain
  changed stages only. Unchanged ticks send periodic keep-alive comments without
  reloading full stage models or serializing JSON. See the [300-page event polling
  measurements](measurements/scantailor-workspace-events.md) for the PostgreSQL
  fingerprint path, portable token fallback, and remaining database scan cost.
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
