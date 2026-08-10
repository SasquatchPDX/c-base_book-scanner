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
   single-page pass-through.
2. Deskew — projection-profile estimation with configurable search bounds or a
   manual angle.
3. Select Content — automatic content detection or a draggable/resizable content
   rectangle.
4. Margins — per-side/canvas padding and horizontal/vertical alignment.
5. Dewarping — OpenCV projective correction using detected or draggable control
   points, with an explicit bypass and deterministic failure behavior.
6. Output/Binarization — Otsu, Sauvola, Wolf, Fox, Window, Grad, and copy modes,
   plus draggable text/picture zones with independent algorithms.

Pillow, NumPy, OpenCV, and scikit-image provide the image operations. Processing
runs as durable worker jobs. Successful stages record input/output dimensions so
browser geometry can be mapped between full-resolution source coordinates and a
scaled display. Stage previews are bounded to 960 pixels; the first stage still
uses the preserved capture as its input because it has no predecessor preview.

Changing a stage increments its optimistic-concurrency revision, clears its prior
error, queues processing, and invalidates every downstream stage. A replacement
scan page similarly invalidates dependent artifacts without deleting the old page
version.

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
Uvicorn/PostgreSQL deployment boundary. Browser-driven multi-user and full
scan-to-library acceptance remain the next production-like E2E layer.
