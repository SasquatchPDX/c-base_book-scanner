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

- `POST /scan/pages/{page_id}/rescan/` creates a new RESCAN todo for the page.
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
  (`sequence_number`, `sha256`, `version` of each active version), `open_rescans`, and
  `note: "re-finish needed"` while a complete job waits for a re-finish.
- Every scanner job route other than registration (`GET jobs/{id}`, `capture-complete`,
  `abort`) serves only jobs with `source=scanner`. A web-created job and an unknown ID
  both answer `404`, so job IDs cannot be probed.
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

### Manifest sources: what exists now and how later tickets extend it

`GET jobs/{id}` and `capture-complete` are built from *manifest sources* in
`core/scan_manifest.py`. Each source owns one field (`pages`, `covers`, `metadata_opf`):
what serif holds, its JSON status and how the request's field is parsed. Only pages exist
so far, plus the stored `scan.jsonl` event lines (S5). Until a feature is stored on serif,
nothing of it can be verified:

- a field with no registered source is left out of `GET jobs/{id}` (covers until S4,
  `metadata_opf` until S6). In a `capture-complete` request it **fails closed**: an empty
  value (`null` or `[]`) is accepted, but every declared item is reported `missing`
  (`cover:front`, `cover:back`, `metadata_opf`), because serif cannot have stored it;
- the event high-water mark is the highest stored scanner `line_no` of the job's
  `scan.jsonl` lines (S5, `scan_events.sans_high_water_mark`, registered at app start
  with `scan_manifest.register_event_high_water_mark`), `0` before the first line. A
  request `sans_last_line_no` that differs from it is `mismatched` (`events`).

S4 and S6 call `scan_manifest.register_manifest_source(...)`; S5 calls
`scan_manifest.register_event_high_water_mark(...)`. S6 (#130) registers `metadata_opf`:
`GET jobs/{id}` reports `"metadata_opf": {"sha256", "version"}` (or `null`), and a
capture-complete `metadata_opf: {"sha256"}` must match the current OPF version
(`missing`/`mismatched` item key `metadata_opf`; `null` expects nothing).

## Scanner `metadata.opf` (#130, contract D3, §4.5, §6.8)

`PUT /api/scanner/v1/jobs/{job_id}/metadata` takes `multipart/form-data` with two parts:
`metadata` (JSON `{"sha256": "<lowercase hex>"}`) and `opf`
(`application/oebps-package+xml`, at most `BOOK_SCANNER_SCANNER_MAX_OPF_BYTES`, default
1 MiB).

| Status | Meaning |
|---|---|
| `201` | Stored as a new version (versions are immutable; the newest is current). |
| `200` | The checksum equals the current version. Nothing changes. |
| `400 invalid_request` | Not multipart, or the `metadata`/`opf` part is missing or malformed. |
| `413 payload_too_large` | The OPF is larger than the limit. |
| `422 checksum_mismatch` | The `opf` bytes do not match `sha256`. |
| `422 invalid_opf` | Not well-formed XML, no `package/metadata`, or forbidden XML (DTD entities such as "billion laughs", external entities/XXE). Parsed with `defusedxml`; nothing is expanded or fetched. |
| `409 job_state` | The job is `cancelled`. |
| `404` | Unknown job, or not a scanner job. |

The response carries `metadata_opf` (`sha256`, `version`) and the parsed `metadata`
(title, authors, ISBN, language). Fields come from the intake OPF-sidecar parser
(`intake.parse_opf_metadata`); the ISBN is stored in its ISBN-13 form. The raw bytes of every
version are kept in `ScanJobOpf`. An OPF carrying the application's own marker
`<meta name="ebook-server:generated" content="true"/>` is stored but never used as metadata.

**While the scan is raw** the current OPF's title, authors, ISBN and language are shown on
the job page (`/scan/jobs/{id}/workspace/`, also in its JSON as `metadata_opf`) and in
**Raw scans** (`/scan/raw/`, `metadata`). No Book is created.

**At finish** (`scan_lifecycle.register_book_linker`, `scanner_opf.link_book_from_opf`):

1. The OPF's ISBN matches exactly one Book: the job is linked to it and gains a new
   variant. The OPF fills that Book's empty fields; a differing field opens a review.
2. Otherwise a new Book is created from the OPF fields. Every field gets a provenance row
   with source `scanner_opf` (`BookMetadataProvenance`).
3. No OPF, or an OPF without a title: the existing scan identification and review queue
   run, with the OPF's authors as hints.

Two cases go to scan identification and its review instead of guessing (AGENTS.md):
an ISBN that **several** Books carry, and an **ISBN-less** OPF whose normalised title and
author set equal an existing Book's (a likely duplicate). The review page shows the
scanner metadata, a "Link to existing book" button per possible duplicate and "Create a
new book from the scanner metadata" (`POST /reviews/{id}/scanner-opf/`,
`action=create_book`).

**An OPF received after linking** is applied to the Book field by field:

- an empty field is filled;
- a field the scanner set last, and that nobody changed since, is updated (with a history
  row holding the old and new value);
- a field a reviewer edited by hand (admin edits are recorded as `reviewer` provenance),
  or whose value came from another source or changed outside the scanner, is **not**
  overwritten. The conflict opens a `quality_review` task listing scanner and current
  values, with "keep the current values" preselected
  (`POST /reviews/{id}/scanner-opf/`, `action=resolve`, `choice=current|scanner`).

### Owner decision: scanner OPF is primary, and it is still verified externally

Contract decision D3 says the scanner's OPF is authoritative and server-side
identification only runs when the scanner sends no title. AGENTS.md says external
metadata verification is mandatory for intake. The project owner decided to satisfy both:

- The scanner OPF is the primary metadata source. Book creation and linking follow §4.5
  as above, without waiting for providers.
- After a Book is created or linked from an OPF (and after each later OPF version applied
  to it), a background job (`ProcessingJob` type `scanner_opf_check`) looks the OPF's
  title, authors and ISBN up through the existing provider abstraction. Provider calls
  are rate-limited as always (`core/rate_limit.py`).
- Provider records whose own identifiers carry the OPF's ISBN decide; without such a
  record only candidates at or above `BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT` count.
  - `confirmed` (one of them agrees with title and authors), `no_results`,
    `inconclusive` (only weak matches) or `provider_error`: the scanner metadata stays.
  - `contradicted` (a confident record names another title or other authors, for example
    the record for the scanner's ISBN describes a different book): nothing is
    overwritten. A `quality_review` task shows the scanner values next to the provider
    record, with **the scanner OPF preselected** (`choice=scanner|provider`). Choosing
    the provider record writes it as a reviewer edit, which later OPFs do not overwrite.
- The result is recorded on the OPF version (`ScanJobOpf.provider_check`), in the Book's
  provenance (`action=provider_check`) and in the processing job's details.

### Out-of-order workers and stale reviews

A provider check can finish after the scanner has sent a newer OPF. Results and review
decisions are therefore fenced to the OPF version and Book they were made for (lock order
everywhere: scan job, Book, review tasks):

- A check records the OPF version id and Book id it was started for. Before the lookup
  and again when it completes, under the job's row lock, it only publishes if that OPF is
  still the job's effective one and the job is still linked to that Book. Otherwise the
  result stays on the processing job as history (`details.stale`, `published: false`) and
  nothing else changes.
- A check only creates or refreshes an **open** review task for its own OPF version. A
  **claimed** (or resolved) review is never modified: its evidence is what the reviewer is
  deciding on. A new OPF version cancels unclaimed conflict reviews of older versions.
- Each scanner-OPF conflict review stores its OPF version id and a fingerprint of the
  Book's values for the fields it shows (`book_revision`). The review detail exposes
  `scanner_opf.revision`, a fingerprint of that evidence; `action=resolve` must send it
  back (`revision`, required). Resolving is rejected - `409` with the reason and the
  current `revision` for API clients, a form error on the review page for browsers - and
  nothing is written to the Book when:
  - the revision no longer matches the task (its evidence changed after it was loaded);
  - the task's OPF version is no longer the effective one, or the scan is linked to
    another Book: the task is closed as obsolete (`state=cancelled`, evidence kept, plus
    an `obsolete` marker), and the current OPF gets its own review if it needs one;
  - the Book's values for the reviewed fields changed since the task was opened: the task
    is refreshed with the current values and a new revision, to be decided again.

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

