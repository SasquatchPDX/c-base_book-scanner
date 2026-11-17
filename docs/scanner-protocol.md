# Sprint001 Scanner Protocol Draft (v1)

## Core resources

- `ScanJob`: string ID (a server-generated UUID for web-created jobs, the scanner-assigned
  `YYYYMMDDTHHMMSSZ[-N]` for jobs registered over the scanner API), `source` (`scanner` or
  `web`), metadata hints, status, operator, scanner id.
- `ScanPage`: logical page identity and one or more immutable artifacts.
- `ScanTodo`: optional tasks like `RESCAN`.

## API surface (v1)

- `POST /scan/jobs/` -> create job
- `GET /scan/jobs/` -> list open jobs for scanner/operator
- `GET /scan/jobs/{job_id}/` -> detail
- `POST /scan/jobs/{job_id}/pages/` -> upload page
- `PUT /scan/pages/{page_id}/` -> replace/reupload idempotently
- `POST /scan/jobs/{job_id}/logs/` -> upload a scanner log file (202, processed by a worker)
- `GET /scan/jobs/{job_id}/logs/` -> the job's logs and their processing state
- `POST /scan/jobs/{job_id}/identify/` -> queue bibliographic lookup/review
- `GET /scan-identifications/{processing_job_id}/` -> lookup job status
- `POST /library/books/{book_id}/ocr/` -> request OCR from library variant
- `POST /scan/jobs/{job_id}/ocr/` -> request OCR from scan pages
- `GET /ocr/{job_id}/` -> OCR job status/provenance
- `POST /scan/jobs/{job_id}/finish/` -> queue identification when unlinked, otherwise finalize

## Page upload contract

- multipart form fields (`POST /scan/jobs/{job_id}/pages/`):
  - `file` (required): the image; JPEG, PNG, TIFF, BMP or WebP. Anything Pillow cannot decode
    is rejected with 400 `invalid image`. The stored `mime_type` comes from the sniffed
    format, not the client `Content-Type`.
  - `logical_sequence_number` (required, integer >= 1)
  - `side` (optional): `left`, `right` or `single`, case-insensitive, aliases `l`/`r`/`s`.
    Missing means `single`. Any other value is a 400 (so a left+right pair can never
    silently collapse onto one page).
  - `checksum_sha256` (recommended): verified against the received bytes (400 on mismatch)
  - `capture_timestamp` (optional): ISO 8601 (naive means UTC), epoch seconds or epoch
    milliseconds (values above 1e11). A provided but unparseable value is a 400.
  - `scanner_metadata` (optional): JSON object string
- the client filename is sanitised (`Path(name).name`, unsafe characters replaced) and kept
  with its extension in the stored name; if it has no image extension one is derived from
  the sniffed format.
- idempotency:
  - repeated upload for the same `(job_id, sequence_number, side, checksum)` returns the
    existing version with HTTP 200 (`created: false`); a new version returns 201.
- job state: the first successful upload moves an `open` job to `capturing`.
- `PUT /scan/pages/{page_id}/` replaces a page. The body MUST be `multipart/form-data` with
  the same `file` / `checksum_sha256` / `capture_timestamp` / `scanner_metadata` fields.
  Replacing with an already-stored checksum re-activates that version (200).
- `POST .../pause/`, `.../resume/` and `.../cancel/` are idempotent (200 with the current
  state when already there) and return 400 if the job is in a state that cannot move
  (e.g. `finalizing`).
- `GET /scan/pages/{page_id}/image/` serves a downscaled (max 1600 px) JPEG of the active
  version with `ETag`/`Cache-Control`; originals are never streamed to browsers.

## Finish and raw pages

`POST /scan/jobs/{job_id}/finish/` (JSON or form body) finalizes a job linked to a book. By
default only pages with processed ScanTailor output are accepted. Raw scanner pages are
rejected with 400:

```json
{"error": "missing processed output for 2 page(s): ...",
 "missing_pages": ["<page uuid>", "<page uuid>"],
 "hint": "run ScanTailor stages or resend with allow_raw_pages=true"}
```

Send `{"allow_raw_pages": true}` to build from the raw uploads instead. A job with no linked
book queues bibliographic identification instead (202, `requires_bibliographic_review`).

## Client recipe (non-browser clients)

1. **Log in.** `GET /accounts/login/` and keep the `csrftoken` cookie. Then
   `POST /accounts/login/` as a form with `username`, `password` and
   `csrfmiddlewaretoken=<csrftoken cookie value>`, plus the header
   `Referer: <base url>/accounts/login/`. Success is a 302 and a `sessionid` cookie; keep
   the cookie jar for all later requests. Bad credentials return 401. Do not follow the
   redirect blindly if you only need the cookies.
2. **Every unsafe request** (POST, PUT) needs `X-CSRFToken: <current csrftoken cookie>` and
   a `Referer` header. Over HTTPS the Referer is mandatory and the origin must be listed in
   `BOOK_SCANNER_CSRF_TRUSTED_ORIGINS`, otherwise Django answers 403. The csrftoken cookie
   rotates at login, so re-read it after logging in.
3. Send `Accept: application/json`. Requests that do not ask for `text/html` get
   `401 {"error": "authentication required"}` when the session is missing or expired
   (browsers still get the login redirect). On a 401, log in again (step 1) and retry.
4. **Create a job:** `POST /scan/jobs/` (form or JSON: `provisional_title`, `scanner_id`,
   `book_id`, `expected_page_count`, `capture_layout`...). The response (201) contains
   the job `id`. `capture_layout` is `single_page` (every image is one page; the
   default and what the project's scanner produces) or `spread` (an image may hold two
   facing pages). If it is left out, scanner ids listed in
   `BOOK_SCANNER_SCAN_SPREAD_SCANNER_IDS` get `spread` and every other scanner gets
   `BOOK_SCANNER_SCAN_DEFAULT_CAPTURE_LAYOUT`. Any other value is a 400. In a
   `single_page` job, ScanTailor's Page Split completes automatically as "no split"
   (see `docs/scantailor-web.md`).
5. **Upload pages** one at a time with `POST /scan/jobs/{id}/pages/` as multipart, always
   sending `checksum_sha256` of the exact bytes. Retries are safe: re-sending the same
   `(sequence, side, checksum)` returns 200 without creating a version, so retry on network
   errors, timeouts and 5xx. A 4xx (other than 401) will not succeed on retry.
   Use `side=left|right` for spreads (same sequence number, two uploads) and `single`
   otherwise.
6. **Replace** a bad page with `PUT /scan/pages/{page_id}/` using a multipart body (not
   JSON, not raw bytes).
7. **Finish** with `POST /scan/jobs/{id}/finish/`, adding `"allow_raw_pages": true` if no
   ScanTailor processing has been run.

A runnable version of this flow is `scripts/scanner_client_example.py`.

## Scanner logs and where scan output is stored

`POST /scan/jobs/{job_id}/logs/` takes a multipart `file` field holding a whole
scanner log file: plain text, at most 8 MiB. The request only writes the
bounded upload to scan staging (an empty or oversized upload is refused with 400) and
records the log with a `scan_log` processing job, which is queued once the request's
transaction commits; it answers `202` with the log (`state: queued`) and a
`status_url`. A worker then checks that the content is text, hashes it and stores it.
It hashes the stored copy again and compares it with the upload before it records the
log as stored and deletes the staged upload; a copy that does not match is removed, the
staged upload is kept and the log becomes `failed` (an admin can retry it). The log's
`state` becomes `stored`, `duplicate` (the same content is already stored for the job;
`duplicate_of` names that log, and no second file is written) or `failed` (with
`error`). `GET /scan/jobs/{job_id}/logs/` and the scan job detail (`logs`) report it.
The client filename is only kept as provenance; the server stores each upload as
`scan-upload-<first 8 hex digits of its SHA-256>.log`.

Uploaded log files never take the name `scan.jsonl`. In the book folder that name is
reserved for the scanner event log rendered from the events endpoint (#129, S5), which
is rewritten as events arrive; `/logs/` uploads are immutable originals.

Everything the library keeps from a scan goes into the book's archive folder on the
attached storage (`<Author>/<Title>/` under the default `BOOK_SCANNER_ARCHIVE_LAYOUT`):
the cleaned PDF (`<Title> - <Author>.pdf`), OCR output and conversions derived from it,
the cover, and uploaded scanner logs (`scan-upload-<sha8>.log`). A log uploaded before the
job is linked to a book waits in scan storage and is moved into the book folder during
finalization; the scan-storage copy is deleted only after the archive copy's SHA-256 is
verified. Raw page captures and ScanTailor stage outputs stay in `scan-artifacts/`,
keyed by scan job, because the job may still be unidentified and stages are re-run.

## Storage and resilience

- Scanner sends raw image artifacts; server stores immutable upload.
- Replacements create new page version and mark previous version inactive.
- Source deletion by scanner is decoupled; pipeline policy handles source cleanup.

## Rescan todo flow

- `POST /scan/pages/{page_id}/rescan/` creates a new RESCAN todo for the page (`201`), or
  returns the outstanding one (`200`, `created: false`): an `open` or `claimed` todo, as
  the scanner lists both, unchanged and with its assignee, and without a second
  `rescan_flag` line. After it is `completed` or `cancelled` a new flag creates a new
  todo. The check runs under the job's row lock. Allowed while the job is being captured or processed, and also for
  `captured` and `complete` jobs (D7). For a scanner job it appends a `rescan_flag` line
  to `scan.jsonl` in the same transaction. The scanner fetches and fulfils these todos
  through the device API (see "Rescans over the device API" below).
- `GET /scan/jobs/{job_id}/todos/` lists todos for a specific job.
- `GET /scan/todos/?state=...` lists or filters todos across jobs.
- `POST /scan/todos/{todo_id}/claim/` claims a todo.
- `POST /scan/todos/{todo_id}/complete/` marks a todo completed.
- `POST /scan/todos/{todo_id}/cancel/` cancels a todo.
- `GET /scan/jobs/?with_open_todos=true` lists jobs with open rescan todos.

## Security

- Upload endpoints require an authenticated session (401 JSON for API clients).
- Filenames are sanitized before storage.
- Parser/extractor boundaries remain server-side behind size and timeout controls.

## Scan job IDs and the scanner API (`/api/scanner/v1/`)

The libreflip scanner API is specified by the contract in
[libreflip/serif#37](https://github.com/libreflip/serif/pull/37) (`docs/sans-serif-interface.md`).
There is one scanner. It authenticates with the scanner API key (`Authorization: Bearer`,
see `docs/operations.md`).

- A `ScanJob` ID is an opaque, immutable string of at most 64 characters. Web-created jobs
  keep a server-generated UUID in canonical form (`str(uuid4())`). Jobs registered by a
  scanner use the scanner's ID, which must match `^[0-9]{8}T[0-9]{6}Z(-[1-9][0-9]{0,3})?$`.
- Every `/scan/jobs/{job_id}/...` route accepts both forms (path converter `scan_job_id`).
- `PUT /api/scanner/v1/jobs/{job_id}` with `{"scan_started_at": "<ISO 8601>",
  "capture_layout": "single_page"}` (layout optional) registers a job:
  - `201`: created in state `open` with `source=scanner` (`operator` is empty).
  - `200`: the same `scan_started_at` and layout are registered again; nothing changes.
  - `409 job_id_conflict`: the ID exists with another `scan_started_at` or layout, or
    as a web-created job. Nothing is overwritten.
  - `400 invalid_job_id`: the ID does not match the format above (UUIDs included).
  - `400 invalid_json` / `invalid_request`: the body is not a JSON object, or
    `scan_started_at` is not a valid ISO 8601 timestamp with a time zone (including
    well-formed but impossible dates such as `2026-02-30T00:00:00Z`), or the layout
    is unknown.

  The response body is the job status (see `GET` below). `portal_url` is built from
  `BOOK_SCANNER_PUBLIC_BASE_URL` as `<base>j/<job_id>`.
- `GET /api/scanner/v1/jobs/{job_id}` (§6.4) returns the job status: `job_id`, `state`,
  `portal_url`, `scan_started_at`, `capture_layout`, `events.sans_last_line_no`, `pages`
  (`sequence_number`, `sha256`, `version` of each active version), `covers` (`position`,
  `sha256`, `version` of each active cover), `open_rescans`, and
  `note: "re-finish needed"` while a complete job waits for a re-finish.
- Every scanner job route other than registration (`GET jobs/{id}`, `capture-complete`,
  `abort`, page and cover uploads) serves only jobs with `source=scanner`. A web-created
  job and an unknown ID both answer `404`, so job IDs cannot be probed.
- `POST /api/scanner/v1/jobs/{job_id}/capture-complete` (§6.9) takes the full manifest
  (`finished_at`, `sans_last_line_no`, `pages`, `covers`, `metadata_opf`):
  - `200 {"verified": true, "state": "captured", "portal_url"}`: every listed item is
    held with exactly that active checksum and the event high-water marks agree. The job
    moves `open`/`capturing` -> `captured` and serif stores a digest of the verified
    manifest (its items and `sans_last_line_no`; not `finished_at`).
  - Once the job is `captured`, `finalizing` or `complete`, repeating the verified
    manifest (items in any order) answers `200` again and changes nothing. Any other
    manifest is checked against what serif holds now: `200` only if every listed item is
    held and the high-water marks agree, else `409 incomplete`. Success is never answered
    from the job state alone, because a `200` lets the scanner delete its local copy.
  - `409 incomplete`: `details.missing` and `details.mismatched` list item keys
    (`page:<n>`, `cover:<position>`, `metadata_opf`, `events`); `details.sans_last_line_no`
    is serif's high-water mark. The job stays where it was.
  - `409 job_state`: the job is `cancelled` (or a web job in `paused`).
  - `400 invalid_request`: a malformed manifest (`details.field` names the part),
    including a `finished_at` that is not a valid ISO 8601 timestamp with a time zone.
- `POST /api/scanner/v1/jobs/{job_id}/abort` (§6.10) moves `open`/`capturing` to
  `cancelled` and keeps every uploaded file. Repeating it is `200`; from `captured` or
  later it is `409 job_state`.
- `PUT /api/scanner/v1/jobs/{job_id}/pages/{sequence_number}` (§6.5, #128) uploads one
  page as `multipart/form-data` with a `metadata` part (JSON: `sha256` lowercase hex,
  `camera_role` `left`/`right`, optional `captured_at`, `recognized_page_number` string
  or `null`, `status`) and an `image` part. Either part may be sent with or without a
  filename. `sequence_number` is zero-based; the page is stored as `ScanPage`
  (`logical_sequence_number = sequence_number`, `side = single`) with
  `recognized_page_number` and `status`, and an immutable `ScanPageVersion` with
  `uploader = NULL` and `source = scanner`.
  - `201`: stored as version 1; the job moves `open` -> `capturing`.
  - `200`: the same checksum is already active; nothing changes.
  - `409 page_conflict`: a different image is active for that `sequence_number`
    (`details.sha256`). Replacements go through the rescan flow only.
  - `409 job_state`: the job is not `open`/`capturing` (`details.state`).
  - `422 camera_role_parity` (even numbers are `left`, odd `right`),
    `422 checksum_mismatch`, `422 invalid_image`: not a JPEG/PNG/TIFF/WebP whose pixels
    fully decode within Pillow's decompression-bomb limit (`Image.MAX_IMAGE_PIXELS`).
    Valid headers and PNG chunk checksums are not enough; the staged file is decoded
    before anything is published, so no version is created.
  - `413 payload_too_large`: the image is above `BOOK_SCANNER_SCANNER_MAX_IMAGE_BYTES`
    (the `max_image_bytes` that `ping` reports), or `Content-Length` is above that plus
    1 MiB of multipart overhead. `415` for a body that is not `multipart/form-data`;
    `400` for malformed metadata (`details.field`) or a missing part, including a
    `captured_at` that is not a real ISO 8601 timestamp with a zone (`2026-02-30T...`);
    `400 upload_interrupted` when the body ends early.
  - `507 insufficient_storage` / `503 storage_unavailable`: transient, retry.
- `PUT /api/scanner/v1/jobs/{job_id}/covers/{front|back}` (§6.6) has the same shape with
  `metadata` `{"sha256", "captured_at"}` (same `400`/`422` rules). The active checksum is `200`; a new checksum
  adds a version (`201`) and keeps the earlier ones (`ScanCover`). Allowed in `open`,
  `capturing` and `captured`; a cover does not move the job to `capturing`. At finish
  (scan finalization) the active covers become the Book's front/back `CoverImage`s with
  `source = scanner`: a normalized JPEG (as for covers extracted from files) in the
  library archive, linked to the `ScanCover` it came from; the raw photo stays in scan
  storage. A cover the Book already had is replaced, and the new cover records the old
  one's provenance in `replaced`.

**How an upload is stored.** The body is parsed as a stream (Django's multipart parser,
64 KiB reads); the `image` part is written chunk by chunk to a temporary file in
`scan-artifacts/uploads/` and hashed on the way, so the image is never held in memory and
`request.body` is never read. After the checksum and image checks, the staged file gets
its final name with `publish_unique` (an exclusive hard link on the same volume; nothing
is ever overwritten), the published copy is re-hashed, and only then, under the job's row
lock, is the version row created and made active. A dropped connection, a size, checksum
or image error, or a crash before that commit leaves no version row, and the staged and
published files are removed. (Behind ASGI the server spools the body to a temporary file
before the view runs; it is still never one in-memory blob.)

### Manifest sources: what exists now and how later tickets extend it

`GET jobs/{id}` and `capture-complete` are built from *manifest sources* in
`core/scan_manifest.py`. Each source owns one field (`pages`, `covers`, `metadata_opf`):
what serif holds, its JSON status and how the request's field is parsed. Pages, covers
(S4, `core/scan_covers.py`, registered at app start) and the stored `scan.jsonl` event
lines (S5) exist so far. Until a feature is stored on serif, nothing of it can be verified:

- a field with no registered source is left out of `GET jobs/{id}` (`metadata_opf` until
  S6). In a `capture-complete` request it **fails closed**: an empty value (`null` or
  `[]`) is accepted, but a declared item is reported `missing` (`metadata_opf`), because
  serif cannot have stored it;
- the event high-water mark is the highest stored scanner `line_no` of the job's
  `scan.jsonl` lines (S5, `scan_events.sans_high_water_mark`, registered at app start
  with `scan_manifest.register_event_high_water_mark`), `0` before the first line. A
  request `sans_last_line_no` that differs from it is `mismatched` (`events`).

S4 and S6 call `scan_manifest.register_manifest_source(...)`.

## Raw scans (#127, contract §5)

```
open -> capturing -> captured -> finalizing -> complete
open, capturing --abort--> cancelled
complete --replacement page--> captured ("re-finish needed")
```

- `captured` is a **raw scan**: capture is verified, ScanTailor-Web processing and a
  human finish are still to come. The portal lists these jobs under **Raw scans**
  (`/scan/raw/`) with title, page count, completeness flags and open rescans, and the
  workspace says "Raw scan, being processed".
- ScanTailor stages, rescan requests and page replacements are allowed in `captured`;
  new pages are not. Rescans and replacements are also allowed after `complete` (D7).
- `POST /scan/jobs/{job_id}/finish/` accepts `captured` jobs. It still requires processed
  ScanTailor output unless `allow_raw_pages`. A job without a book first asks the
  registered book linkers (`scan_lifecycle.register_book_linker`; S6/#130 links from the
  OPF) and otherwise queues scan identification, as before.
- A replacement page for a `complete` job puts it back to `captured` with
  `refinish_needed` (note "re-finish needed"). Its library variants stay until the next
  finish, which retires them in favour of the new cleaned output.
- `/j/<job_id>` (the scanner's QR code) requires login and redirects to the job's
  workspace.

**Raw scans never appear in the library.** `core/library_visibility.py` hides, at the
query level, every book that is linked only to unfinished (or cancelled) scan jobs and has
no uploaded/imported variant of its own, and every cleaned variant built by an unfinished
scan job. Library browse, search (both backends; such books are never in the search
index), home, book detail, download and covers use it. A raw scan never hides a book that
is already in the library for another reason.
### `scan.jsonl` event lines (`POST /api/scanner/v1/jobs/{job_id}/events`)

The scanner's append-only process log (`scan.jsonl`, libreflip/sans#48, contract §4.4
and §6.7) arrives line by line. Each line is stored raw as a `ScanEvent` (`origin`,
`line_no`, `raw`, `type`, `received_at`).

- Request: `{"first_line_no": 58, "lines": ["{\"type\":\"attempt\",...}", ...]}`. The
  scanner numbers its lines `1, 2, 3...` in file order; `(job, line_no)` is unique and is
  the idempotency key.
- `200 {"sans_last_line_no": N}`: stored. Lines at or below the stored high-water mark
  must be byte-identical; they are confirmed and skipped, so re-sending an overlapping
  batch changes nothing.
- `409 event_gap`: `first_line_no` is beyond the high-water mark + 1;
  `details.expected_first_line_no` says where to resume.
- `409 event_conflict`: a stored line differs (`details.line_no`). Nothing of the batch
  is stored.
- `422 invalid_event`: a line is not one JSON object with a non-empty string `type`, is
  longer than 64 KiB, or contains a line break (`details.line_no`). Nothing is stored.
- `413 payload_too_large`: more than 1000 lines or 1 MiB of line bytes in one batch.
- `409 job_state`: the job is cancelled. Every other state accepts events, so the final
  `completeness_check` still arrives after capture.
- `404 not_found`: no such job, or a web-created job (only `source=scanner` jobs are
  served).

Overlapping batches that arrive concurrently are serialised by the job's row lock, with
the unique constraint as the backstop: no duplicate and no gap can be stored.

Known event types are projected into the database; all others (`attempt`, future
telemetry) are stored only. For each projected field the latest valid value wins; a
malformed field (wrong type, a number that does not fit a float, NaN/Infinity, a
`sequence_number` beyond 2147483647) is ignored while the line itself is stored:

| Event | Projection |
|---|---|
| `job` | `ScanJob.page_width_mm`, `ScanJob.page_number_crop` |
| `page` | `ScanPage.recognized_page_number`, `ScanPage.status` (for `sequence_number`, side `single`) |
| `completeness_check` | `ScanJob.completeness_flags` (shown in the scan workspace), `completeness_checked_at` |

A `page` event that arrives before its page image is applied when the page is created
(`scan_events.project_page_from_events`). serif appends its own lines (origin `serif`:
`rescan_flag`, `rescan_replace`, each with `"origin": "serif"`, `"at"` and `"todo_id"`)
with `scan_events.append_serif_event`. `scan_events.sans_high_water_mark(job)` gives the
scanner's high-water mark, as `GET jobs/{job_id}` and `capture-complete` report it.

**The rendered file.** After every change, once the transaction has committed, a
background job writes the combined `scan.jsonl`: every stored line in arrival order, each
followed by `\n`. The scanner's lines therefore appear byte-for-byte in `line_no` order,
with serif's lines at the positions where they arrived. The write is atomic (temporary
file in the same folder, then rename) and goes through the storage backend:

- into the job folder in scan storage: `scan-artifacts/scan-jobs/<job_id>/scan.jsonl`;
- once the job is finished (`complete`, linked to a Book), also into the book's archive
  folder as `scan.jsonl` (`books/<book id>/scan.jsonl` under the `job` layout). Events
  that arrive after finish update the book's copy too.

serif never overwrites a file it did not write. The first write to each folder claims a
name with an exclusive create (the allocation `publish_unique` uses): `scan.jsonl`, else
`scan-<job_id>.jsonl`, else `scan-<job_id>-<token>.jsonl`. A name that already exists (a
log uploaded independently, another tool's file) or that another scan job of the same
book has claimed is skipped. The claimed name is kept on the job
(`scan_jsonl_job_identifier`, `scan_jsonl_book_identifier`), and later renders replace
only that file.

Concurrent renders are fenced: each pass takes the next `scan_jsonl_render_generation`
under the job's row lock, stages its snapshot beside the file, and renames it into place
only while still holding the newest generation, again under the row lock. An older pass
that a newer one overtook discards its snapshot. A pass records its high-water mark
(`scan_jsonl_*_rendered_through`) only after the file reads back as the snapshot, and a
render decides whether a file is current by its size and SHA-256, never by the mark, so
a retry after an interrupted or failed write always repairs the file.

The database is authoritative; the files are renderings and can be rebuilt with
`scan_events.write_scan_jsonl(job_id)`.

### Rescans over the device API (#131, contract §6.11)

A member flags a page in the portal (above); the scanner lists the work, prompts the
operator and uploads each replacement against its todo (`core/scanner_rescans.py`).
"Open" means a RESCAN todo in state `open` or `claimed`.

- `GET /api/scanner/v1/rescans`: `{"items": [{job_id, state, title, authors, isbn,
  open_rescans, scan_started_at}]}` for every scanner job, in any state except
  `cancelled`, with at least one open rescan. `title`, `authors` and `isbn` come from the
  current scanner OPF (S6), else from the linked Book, else `null`.
- `GET /api/scanner/v1/jobs/{job_id}/rescans`: `job_id`, `state`, `page_width_mm`,
  `page_number_crop` (from the `job` event), `pages` (every page's `sequence_number` and
  `recognized_page_number`, so the scanner can interpolate missing numbers) and `todos`
  (`todo_id`, `sequence_number`, `reason`, `created_at`, `thumbnail_url`).
- `GET /api/scanner/v1/jobs/{job_id}/pages/{n}/thumbnail?max=800`: a JPEG of the page's
  active (raw) version with its longest edge at most `max`. `max` defaults to 800, is
  capped at 1600 and rounded down to a multiple of 100 (below 100 is `400`). The answer
  carries an `ETag` (version and size) and `Cache-Control: private, max-age=0,
  must-revalidate`; `If-None-Match` gives `304`. Thumbnails are rendered once per version
  and size into `scan-artifacts/thumbnails/` (`core/scan_thumbnails.py`, shared with the
  workspace thumbnails): a JPEG is decoded at reduced scale, Pillow's decompression-bomb
  limit is an error, and at most two renders run at once per process.
- `POST /api/scanner/v1/jobs/{job_id}/rescans/{todo_id}/replacement`: multipart like a
  page upload (§6.5) with `metadata` `{sha256, captured_at, camera_role,
  recognized_page_number, page_number_verified}` and `image`. The image is streamed,
  checked and published as for a page upload; then, in **one transaction** under the
  job's, the todo's and the page's row locks:
  1. a new active `ScanPageVersion` (`source = scanner`, `uploader = NULL`,
     `page_number_verified`, `scanner_metadata.todo_id`); the previous version is kept,
     inactive;
  2. the todo becomes `completed`;
  3. a `rescan_replace` line (origin `serif`, with `sequence_number`, `sha256`,
     `version`, `todo_id`) is appended to `scan.jsonl`;
  4. ScanTailor stages of the page are marked stale and scan-derived library artifacts
     are invalidated (the existing replacement logic);
  5. a `complete` job goes back to `captured` with "re-finish needed"; its library
     variants stay until the next finish.

  Answers: `201` with `{todo_id, todo_state, sequence_number, sha256, version,
  previous_version, state}`; `200` with the same body when this checksum already
  fulfilled the todo; `409 todo_state` for a cancelled todo (the scanner skips the page)
  or one completed with another image; `409 job_state` for a job that takes no rescans
  (cancelled, finalizing); `404` for an unknown todo or a todo of another job; `422
  camera_role_parity` / `checksum_mismatch` / `invalid_image` as for a page upload. A
  failure before the commit leaves no version and no file.

`page_number_verified: false` (the scanner's OCR did not confirm the printed number and
the operator kept the image) is shown as "Page number not verified" on the page card in
the workspace, and reported in the page JSON (`page_number_verified`, also per version).


### Upgrade note: migration `0041_scanjob_string_pk`

The migration changes `core_scanjob.id` from `uuid` to `varchar(64)`, together with every
column that references it: `ScanPage`, `ScanTodo`, `ScanTailorStageState`,
`MetadataSearch`, `MetadataCandidate`, `ReviewTask`, `LLMSuggestion`,
`ContributionEvent` and `ScanLog`. Existing IDs keep their value in canonical string form, so links,
storage paths and `scan_job_id` values in JSON details stay valid. The JSON fields are
`ProcessingJob.details`, `FileVariant.converter_options`, `ScanJob.notes`,
`ReviewTask.decision_payload` and `ContributionEvent.outcome`. Any non-canonical spelling
in them is rewritten.

- **PostgreSQL** casts `uuid::varchar` and rewrites all ten tables and every index on
  the changed columns (primary key, foreign-key indexes, the composite and unique indexes
  that contain `scan_job_id`) under an `ACCESS EXCLUSIVE` lock. Stop the web and worker
  units while `migrate` runs. The string keys are 36 bytes instead of 16, so these
  indexes grow by roughly a factor of two. Measured on PostgreSQL 17 (local Docker,
  synthetic data): about 1.5 s for 200 jobs with 10,000 pages and 10,000 stage states, and about 33 s for 2,000 jobs with 600,000 pages and 600,000 stage states (the other referencing tables were empty). The time grows linearly with the number of pages
  and stage states.
- **SQLite** rebuilds the tables and the migration rewrites the stored 32-digit hex IDs
  into the canonical form.
- **Reversal** works while every job ID is still a UUID. Once a scanner-assigned ID
  exists, reversing raises `IrreversibleError` instead of dropping or corrupting jobs.

