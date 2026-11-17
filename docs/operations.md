# Operations Guide

## Process topology

Deploy the project as a modular monolith with separate web and RQ worker
processes. PostgreSQL stores canonical and job state, Redis carries background
jobs, an LDAP directory authenticates users, and a network mount stores library
artifacts. The OCR service remains external.

At least these processes are expected:

```text
HTTPS proxy -> Django web -> PostgreSQL
                         -> Redis -> RQ workers
                         -> mounted library/scan storage
                         -> LDAP and public metadata providers
                         -> remote OCR service
```

Run the web application through ASGI so ScanTailor server-sent event connections
remain asynchronous:

```bash
uv run uvicorn ebook_server.asgi:application --host 127.0.0.1 --port 8000
```

The HTTPS proxy must pass streaming responses without buffering and keep the SSE
connection open long enough for its configured lifetime. The application also
sets `X-Accel-Buffering: no`. Tune the one-second/45-iteration defaults with
`BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS` and
`BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_MAX_EVENTS`; account for the resulting
database polling load when sizing large concurrent scanning sessions.

`deploy/nginx/book-scanner.conf` provides a validated Nginx upstream example with
a dedicated unbuffered SSE location. Point its upstream at the Uvicorn service,
add site TLS/static-file policy, and keep direct Uvicorn access private.

### Static files

Uvicorn does not serve `/static/` (CSS, JavaScript, and the compiled ScanTailor
editor), and the application deliberately has no static-file middleware: the
reverse proxy serves them. Without this step pages load unstyled and the
ScanTailor workspace does not start. Collect them after every install or upgrade:

```bash
uv run python manage.py collectstatic --noinput --clear
```

This writes to `STATIC_ROOT`, which is `src/staticfiles/` inside the checkout.
Make that directory available to Nginx at the path used by the example's
`location /static/ { alias /srv/book-scanner/static/; }`, either by copying it:

```bash
sudo rsync -a --delete src/staticfiles/ /srv/book-scanner/static/
```

or by pointing the `alias` directly at `<checkout>/src/staticfiles/` (keep the
trailing slash, and make sure the Nginx user can read and traverse the path).
The Docker acceptance suite does the equivalent by mounting `src/staticfiles`
into its Nginx container.

`manage.py runserver` serves static files itself only when
`BOOK_SCANNER_DEBUG=true` (or with `--insecure`), which is why a development
checkout can appear to work without `collectstatic`. Do not use `runserver` or
`DEBUG` for a shared install.

### Deployment walkthrough (single host)

Example `systemd` units for the web process and RQ workers are in
`deploy/systemd/` (see its README for the assumed user and paths). A minimal
single-host install, run as the unprivileged service user from the checkout:

```bash
uv sync --frozen
cp .env.example .env && chmod 600 .env   # then edit: see below
uv run python manage.py migrate
uv run python manage.py collectstatic --noinput --clear
uv run python manage.py set_local_password <admin-username> --admin   # local mode
uv run python manage.py check            # add --deploy in production mode
```

Then publish `src/staticfiles/` to Nginx as described above, install the
Nginx site (changing its upstream to `127.0.0.1:8000` when Uvicorn runs on the
same host), and enable `book-scanner-web` and at least one
`book-scanner-worker@N`. Repeat `migrate`, `collectstatic`, and the static copy
on every upgrade before restarting the units.

### Plain-HTTP test or lab install

`BOOK_SCANNER_ENVIRONMENT=production` requires HTTPS: startup fails unless the
HTTPS redirect, secure cookies, and HSTS are enabled, and with those enabled a
browser on plain HTTP is redirected to `https://` and secure cookies are never
sent, so sign-in cannot work. For a short-lived, non-TLS lab install keep
`BOOK_SCANNER_ENVIRONMENT=development` and leave the `BOOK_SCANNER_SECURE_*`,
`..._COOKIE_SECURE`, and HSTS settings at their `false`/`0` defaults. Development
mode skips the production safety validation, so set everything it would have
enforced yourself: a random `BOOK_SCANNER_SECRET_KEY`, PostgreSQL and Redis URLs
(without Redis, jobs run synchronously inside web requests), and
`BOOK_SCANNER_LDAP_MODE=local` or `ldap` — never `stub`, which accepts any
password. Leave `BOOK_SCANNER_DEBUG` unset. Do not expose such an install
beyond a trusted network; credentials and session cookies travel unencrypted.

`BOOK_SCANNER_ALLOWED_HOSTS` must contain every name or address browsers use to
reach the site, as seen in the `Host` header the proxy forwards. When members
connect by bare IP, list that IP; otherwise Django answers `400 Bad Request`.
List hosts without a port or scheme:

```text
BOOK_SCANNER_ALLOWED_HOSTS=["192.0.2.10","library.lab.example"]
```

`BOOK_SCANNER_CSRF_TRUSTED_ORIGINS` is not needed when the browser's origin
matches the forwarded `Host` exactly, for example `http://192.0.2.10` through
Nginx on port 80. It **is** needed when Nginx listens on a non-default port: the
example config forwards `Host $host`, which drops the port, so a form posted from
`http://192.0.2.10:8080` fails Django's origin check with `403 CSRF verification
failed` unless that origin is trusted (the acceptance suite sets this for the
same reason). Entries include scheme and port:

```text
BOOK_SCANNER_CSRF_TRUSTED_ORIGINS=["http://192.0.2.10:8080"]
```

Behind HTTPS, set `BOOK_SCANNER_SECURE_PROXY_SSL_HEADER=true` instead (see
below); without it every request looks like HTTP and `https://` origins are
rejected.

## Production configuration

Set `BOOK_SCANNER_ENVIRONMENT=production`. Startup then fails closed unless:

- the development secret is replaced;
- explicit allowed hosts are configured;
- PostgreSQL and Redis URLs are supplied;
- the authentication mode is `ldap` or `local` (`stub` is rejected);
- HTTPS redirect, secure cookies, and HSTS are enabled.

When TLS terminates at a trusted reverse proxy, set
`BOOK_SCANNER_SECURE_PROXY_SSL_HEADER=true` and ensure that untrusted clients
cannot inject `X-Forwarded-Proto`. HSTS preload is optional and should only be
enabled after its long-lived operational consequences are understood.

## Authentication modes

`BOOK_SCANNER_LDAP_MODE` selects how members sign in at `/accounts/login/`:

- `local` — **LDAP turned off.** Accounts and hashed passwords live in the
  application database. Nothing contacts a directory, and none of the LDAP
  settings are needed. There is no self-registration; an operator creates each
  account (or resets its password) with:

  ```bash
  uv run python manage.py set_local_password <username>           # prompts twice
  uv run python manage.py set_local_password <username> --admin   # library admin + /admin/
  echo "$PASSWORD" | uv run python manage.py set_local_password scanner --password-stdin
  ```

  Usernames are case-insensitive. Disabling an `AppUserProfile` blocks login and
  ends existing sessions, exactly as in LDAP mode. Accounts keep working if you
  later switch to `ldap` mode, provided the directory uid matches the username.
- `ldap` — authenticate against the directory described below.
- `stub` — accepts any non-empty credentials; development and tests only.
  Production settings refuse to start with it.

## LDAP

`ldap3` performs the credential bind. Configure one of:

- `BOOK_SCANNER_LDAP_USER_DN_TEMPLATE`, for example
  `uid={username},ou=people,dc=example,dc=org`; or
- a search base/filter and optional service bind account.

Usernames are escaped for DN or filter use. Unknown LDAP modes and invalid LDAP
configuration fail closed. The `stub` adapter is strictly a development/test
facility and accepts any non-empty credentials.

In `ldap` mode members sign in at `/accounts/login/` using LDAP credentials, and
there is no local registration or local password. Disabling an `AppUserProfile` invalidates an
existing web session on its next request.

## Infrastructure acceptance environment

Developers and CI can run `./scripts/run_infrastructure_tests.sh` to provision
ephemeral, digest-pinned PostgreSQL, Redis, and seeded OpenLDAP containers. The
runner applies migrations to PostgreSQL, performs a real LDAP service search and
member bind, starts RQ workers that consume Redis jobs, performs real TXT-to-EPUB
and structured HTML-to-EPUB conversions using checksum-verified Pandoc 3.9.0.2,
performs EPUB-to-PDF and
EPUB-to-MOBI conversions using checksum-verified Calibre 9.11.0, validates the
stored formats and authenticated download, round-trips representative PDF and
MOBI sources to EPUB with metadata/text quality checks, and verifies authenticated
live SSE updates through pinned Nginx and a separate Uvicorn ASGI process. A Playwright
Chromium scenario signs in two LDAP
members, creates a scan workspace, uploads a synthetic page, loads the compiled
editor through Nginx, and proves that optimistic concurrency reloads the winning
revision after a stale edit receives HTTP 409. Install Chromium with
`npx playwright install chromium` before running the suite locally. A second
scenario processes all six ScanTailor stages, requests and fulfills a rescan,
accepts a deterministic provider candidate, and crosses the live Redis/RQ and
HTTP multipart OCR boundaries before searching for and downloading the resulting
OCR variant. The OCR service is a contract fixture; it validates orchestration
and provenance, not recognition quality. Containers are removed when the suite
exits.

The suite reserves host ports 15432, 16379, 13389, 18080, 18081, and 18082 by default.
These sit below Linux's ephemeral port range (32768-60999) on purpose: ports
inside it can be taken at random as source ports by the job's own outbound
connections (image pulls, package downloads), which made binds fail
intermittently with "address already in use". Keep any overrides below 32768.
Override the PostgreSQL, Redis, LDAP, and exposed Nginx ports with
`BOOK_SCANNER_INTEGRATION_POSTGRES_PORT`,
`BOOK_SCANNER_INTEGRATION_REDIS_PORT`, and
`BOOK_SCANNER_INTEGRATION_LDAP_PORT`, and
`BOOK_SCANNER_INTEGRATION_PROXY_PORT`; override the OCR fixture port with
`BOOK_SCANNER_INTEGRATION_OCR_PORT`. Port 18081 is the suite-internal host
Uvicorn listener. Credentials in
`compose.integration.yml` belong only to the public seeded test fixtures and must
never be copied into a deployment.

## Storage

Mount NFS/SMB storage on the application and worker hosts before processes start,
then point `BOOK_SCANNER_STORAGE_ROOT` at that mount. The web and every worker
must see the same paths. Do not place the production collection inside the source
checkout or application VM root disk.

Verify database-to-storage references regularly and after mount incidents:

```bash
uv run python manage.py verify_storage
uv run python manage.py verify_storage --verify-hashes
```

The hash mode is I/O intensive and should run off peak. The command is read-only
and exits non-zero for missing artifacts, hash mismatches, path errors, or an
unsupported backend.

### Free-space reserve

Intake keeps `BOOK_SCANNER_STORAGE_MIN_FREE_BYTES` (default 2 GiB; `0`
disables the check) free on the archive volume. This matters most when the volume
also holds PostgreSQL or Redis data.

- `ingest_sources` estimates the space it has left: free space, minus the
  reserve, minus the source size of imports that are queued but not yet
  archived. It queues files only while they fit. When it stops early it prints
  `insufficient free space on the archive volume` to stderr; files not queued
  are picked up by a later run once there is room.
- Every import, including web uploads and jobs that were already queued, checks
  free space again right before archiving. If the copy would breach the reserve,
  the job fails with a clear `insufficient free space ...` error. The MOVE source
  is left untouched. After freeing space, run `ingest_sources --retry-failed`.
- `/healthz/dependencies/` reports `free_bytes` and `min_free_bytes` for the
  storage root. The storage status becomes `degraded` (not `down`, so readiness
  stays green) while free space is below the reserve.

Confirmed book deletion is available only to local application admins. It checks
artifact hashes, moves files into backend-local quarantine, commits the database
deletion, and purges quarantine. A failed database operation restores moved files.
Do not manually remove `.trash` content while a deletion request is active.

## Intake status and source imports

Fresh intake persists these phases before executing their work:

```text
DISCOVERED -> HASHING -> METADATA_EXTRACTION -> SIGNAL_NORMALIZATION
  -> EXTERNAL_IDENTIFICATION -> MATCHED/LOW_CONFIDENCE -> ARCHIVING
```

Successful matched imports finish as `COMPLETE`; archived ambiguous imports return
to `LOW_CONFIDENCE` until human review. Exact duplicates reuse an existing variant
after hashing and skip metadata lookup. `GET /imports/<job-id>/` reports the
current persisted phase, not a transition history; a polling client can miss short
phases. Regression tests read the endpoint at each saved transition.

Configure sources in Django admin. Preview, discovery, and failed-job retry
currently require the management command:

```bash
uv run python manage.py ingest_sources --source-id 1 --preview --json
uv run python manage.py ingest_sources --source-id 1
uv run python manage.py ingest_sources --source-id 1 --retry-failed
```

### Audio files and audiobooks

This system does not manage audiobooks. The decision and its research are in
`docs/design/32-audiobooks.md` (proposed in PR #107, issue #32): members who want
audiobooks use a separate audiobook server such as Audiobookshelf, reading its own
folder on the NAS. Intake therefore recognises audio files only to skip them:

- Extensions `.m4b .m4a .mp3 .aac .ogg .oga .opus .flac .wav .wma .mka .ape .aif
  .aiff .aa .aax .aaxc` are classified as `audio (not managed here)`. They never
  create an `ImportJob`, are never hashed, copied, moved or converted, and a MOVE
  source leaves them where they are.
- Audible files (`.aa`, `.aax`, `.aaxc`) are DRM-protected and are reported as
  such. This project never removes or works around DRM.
- `ingest_sources --preview` lists them as `skipped [audio (not managed here)] =>
  <path>` plus a `skipped_audio=N` total; with `--json` they are rows with
  `"skipped": "audio"`, `"drm_protected"` and `"reason"` (no `source_type`).
  A discovery run appends `skipped_audio=N` to its summary line.
- Discovery logs one `intake_audio_files_skipped` event (source id and count, no
  filenames) per source and pass. There is no Prometheus series for it, because
  `/metrics/` only reports persisted state and skipped audio is not persisted.
- Web uploads of audio files (by extension, or an `audio/*` content type on a
  non-book extension) are refused with HTTP 415 before anything is staged. The
  message links to `BOOK_SCANNER_AUDIOBOOK_SERVICE_URL` when it is set to an
  `http(s)://` URL, and otherwise asks the member to contact the library admins.
  The upload page shows the same pointer.

### Folder layouts (author/title from paths)

Collections are organised differently, so each source can declare how its folders
are structured (Django admin → Source locations → "Folder layout"). This is opt-in:
a source without layouts behaves as before, because not every folder names an
author. `path_layouts` is an ordered JSON list of templates matched against the
path relative to the source root, without the file extension. The first template
that yields a non-ignored value supplies hints:

| Collection | `path_layouts` |
|---|---|
| `Austen, Jane/Pride and Prejudice.txt` | `["{author}/**"]` |
| Calibre library `Jane Austen/Emma (12)/Emma - Jane Austen.epub` | `["{author}/{title} ({*})/{*}"]` |
| flat `Emma - Jane Austen.epub` | `["{title} - {author}"]` |
| author folders plus loose `Author - Title` files | `["{author}/**", "{author} - {title}"]` |
| `Genre/Weber, David & Evans, Linda/Hell's Gate.epub` | `["{*}/{authors}/{title}"]` |

Placeholders: `{author}`, `{authors}` (split on `&`, `;`, `and`, `und`), `{title}`,
`{series}`, and `{*}` (ignored text within one folder or file name). `**` spans
whole folders: zero or more in the middle, one or more at the end. Limits keep
matching cheap: at most 20 templates per source, 3 placeholders per folder/file
name, 4 `{*}` and 2 `**` per template. Names longer than 255 characters and paths
longer than 1024 characters never match.

`author_name_order` controls reordering: `auto` turns `Last, First` into
`First Last`, `last_first` also reorders names without a comma, and `first_last`
never reorders. `layout_ignore_values` lists captured values that are not names,
for example `["Unknown", "Misc", "Cliff Notes"]`.

Path hints are stored in the import evidence under `path_layout` and are only
*evidence* for the external lookup. A layout author replaces the ambiguous filename
"A - B" split guesses but never outranks embedded authors. A layout title goes after
the embedded title. Hints never bypass provider verification or review thresholds.
Check a layout before importing; each preview row shows
`[layout: author=[...] title=... series=...]` or `[layout: no match]`:

```bash
uv run python manage.py ingest_sources --source-id 1 --preview
```

Use the registered source's actual ID. A supported unattended schedule and
application preview/run/retry pages remain Sprint019 task I. COPY retains its
source even when review is needed. MOVE cleanup requires a verified archive; for
duplicates, verification uses the existing variant's declared backend.

Imported variants record their origin in `FileVariant.source_type`: `upload` for
web uploads, otherwise the source location's `default_source_type`
(`incoming_folder` or `network_source`). An exact duplicate reuses the existing
variant and keeps that variant's original origin.

### Metadata provider rate limits

Every outbound metadata-provider request waits for a client-side slot before it is
sent; a server-side HTTP 429 is only the fallback. Limits are minimum intervals per
provider ID, configured in requests/second:

```text
BOOK_SCANNER_METADATA_RATE_LIMITS={"openlibrary":1.0}   # per provider_id; <= 0 disables
BOOK_SCANNER_METADATA_RATE_LIMIT_DEFAULT=1.0            # providers not listed above
BOOK_SCANNER_METADATA_RATE_LIMIT_MAX_WAIT_SECONDS=60
```

When `BOOK_SCANNER_REDIS_URL` is set, slots are reserved atomically in Redis
(`ebook-server:ratelimit:metadata:<provider_id>`, using Redis server time), so the
limit holds across all web and worker processes. Without Redis the limit is
per process. If Redis is unreachable the lookup falls back to the per-process
limiter and logs `rate_limiter_redis_unavailable`. A lookup whose slot would be more
than the maximum wait away is treated as a failed provider request
(`metadata_provider_rate_limit_wait_exceeded`), which routes the import to review
like any other provider failure; raise the maximum or lower worker concurrency if
this appears during bulk imports. New providers call
`ebook_server.core.rate_limit.throttle_provider(provider_id)` before each request.

`BOOK_SCANNER_METADATA_USER_AGENT` defaults to a project identifier with the
repository URL; an empty value uses that default. Override it to add a contact
address for your deployment.

## Workers and external tools

Start RQ workers with the queue name configured by
`BOOK_SCANNER_QUEUE_NAME` (default `ebook-server`). Conversion routes only appear
when their executable is installed. Install Calibre and/or Pandoc in the worker
image, run converters without elevated privileges, and apply host/container CPU,
memory, process, and filesystem limits in addition to the application job timeout.
Workers must load Django before importing queued application functions:

Headless Calibre PDF conversion also requires EGL/OpenGL and XCB runtime
libraries. Set `QT_QPA_PLATFORM=offscreen` in worker environments; the acceptance
runner enforces this, and CI installs `libegl1`, `libopengl0`, `libxcb-cursor0`,
and `libxkbcommon-x11-0`.

```bash
uv run rq worker --with-scheduler --config ebook_server.rq_settings \
  --url redis://localhost:6379/0 ebook-server
```

**`--with-scheduler` is mandatory on at least one worker.** Failed jobs retry
with delays (`BOOK_SCANNER_QUEUE_RETRY_INTERVALS`); in rq 2.x a retry with an
interval goes to the scheduled registry and only a worker started with
`--with-scheduler` ever moves it back to the queue. Without one, a retrying
job (for example scan finalization) would sit forever with its ScanJob in
FINALIZING. As a safeguard `ConfiguredQueue` checks for the scheduler's Redis
lock (`rq:scheduler-lock:<queue>`) at enqueue time and uses immediate retries
(no delay) when none exists, logging `rq_scheduler_not_running_retries_immediate`.
Treat that log line as a deployment bug. `recover_stale_jobs` also warns when
no scheduler lock is present.

### Worker concurrency (ScanTailor-Web batches)

One RQ worker processes jobs strictly one at a time. ScanTailor-Web queues one
job per page and stage, so a stage batch over a realistic book (hundreds of
pages) on a single worker takes as long as the sum of all pages (#54). Run
several worker processes against the same Redis queue to process pages in
parallel; this is standard RQ and needs no code change. With the
`deploy/systemd/` template unit, for example:

```bash
systemctl enable --now book-scanner-worker@2 book-scanner-worker@3 book-scanner-worker@4
```

Size the count to the VM's cores and memory: each stage job decodes a
full-resolution page (a 600 dpi colour scan is tens of MB decoded), and other
job types (conversion, OCR orchestration, finalization) share the same queue.
Keep `--with-scheduler` on at least one of them (several is fine; they
coordinate through a Redis lock).

Stage jobs are safe to run concurrently: each claims its stage row by revision
and only writes its result if that revision is still current. The one ordering
caveat is between stages of the same page. A stage job whose upstream stage has
not finished yet fails with `upstream stage <name> not complete` instead of
waiting. With one worker, FIFO order hides this. With several, wait for a stage
batch to finish before you start the next stage's batch, or re-run the affected
pages afterwards.

### Recovering from killed workers

A worker killed mid-job (OOM, `kill -9`, host restart) leaves its
ProcessingJob RUNNING and, for finalization, the ScanJob FINALIZING. Jobs whose
`started_at` is older than their timeout plus five minutes are treated as stale
and are re-entered automatically if RQ redelivers them. To clean up manually:

```bash
uv run python manage.py recover_stale_jobs          # dry run: lists what it would change
uv run python manage.py recover_stale_jobs --apply  # act
```

`--apply` marks stale RUNNING jobs, and QUEUED jobs with no matching RQ job
(for example after a Redis flush), as FAILED with an explanatory error, and
returns ScanJobs stuck in FINALIZING with no live finalization job to their
prior state so Finish can be retried. It does not re-queue anything itself;
re-request OCR/conversion/identification from the UI afterwards.

Do not grant a worker access to the Docker socket merely to obtain a converter.
Bake reviewed converter binaries into its restricted image instead.

Configure `BOOK_SCANNER_OCR_SERVICE_URL` and the worker network policy so only
workers can submit jobs. Polling is bounded by a wall-clock deadline, not an
attempt count: by default 25 minutes (`BOOK_SCANNER_OCR_POLL_DEADLINE_SECONDS`),
polling every 2 seconds and backing off by 1.5x to a 15-second cap
(`BOOK_SCANNER_OCR_POLL_INTERVAL_START_SECONDS` / `..._MAX_SECONDS`). Unknown
non-terminal remote statuses (`waiting`, `preprocessing`, ...) are treated as
running with a warning; up to six consecutive transport errors or 5xx/408/429
responses during polling are tolerated before the job fails. Uploads use a
separate long read/write timeout (`BOOK_SCANNER_OCR_SUBMIT_TIMEOUT`, default
300 s) while `BOOK_SCANNER_OCR_REQUEST_TIMEOUT` (30 s) covers polls and
downloads. OCR jobs get their own RQ timeout (`BOOK_SCANNER_OCR_JOB_TIMEOUT_SECONDS`,
default 7200 s); the poll deadline is clamped to stay below it. The legacy
`BOOK_SCANNER_OCR_MAX_ATTEMPTS` / `BOOK_SCANNER_OCR_POLL_INTERVAL_SECONDS` are
honoured only if changed from their old defaults (40 / 0.2), giving a fixed
interval and an attempts x interval deadline. An OCR run that yields no usable
output in the requested formats fails instead of reporting success, and an
equivalent active OCR job for the same source variant and formats is reused
rather than starting another remote run. Converter subprocesses have a timeout
(`BOOK_SCANNER_CONVERTER_TIMEOUT_SECONDS`, default 1800 s, always below the queue
job timeout) and their stderr tail is included in the failure message. The
blocking OCR loop occupies an RQ worker until completion or timeout. Persisting service-reported engine/model/language/confidence
remain Sprint019 task E; current variants record requested engine/model/language
and artifact-level confidence.

All OCR/converter tuning names above are optional `settings.py` fields and may
be set in `.env` or the process environment; unset values use the defaults.

OCR clients implement `iter_artifact(remote_job_id, descriptor)` as a bounded
byte iterator. HTTP downloads and scan ZIP packaging use 1 MiB chunks; inline
base64 results are decoded in chunks, but the containing JSON response is still
loaded into memory. Prefer artifact download URLs for whole-book outputs. Workers
hash downloaded bytes while staging them on disk and remove partial temporary
files after download failures. Changed results receive separate storage paths
even when the service repeats a filename, preserving prior outputs.

Digital intake accepts EPUB, MOBI, PDF, TXT, HTML, FB2, DjVu, CBZ, and CBR.
Beautiful Soup handles bounded HTML metadata, Charset Normalizer detects text
encodings, RarFile recognizes CBR containers, and bounded defused XML/ZIP readers
handle FB2 and CBZ metadata. Intake never extracts a whole archive. CBR page
extraction still requires an explicitly configured external archive tool in a
restricted worker environment.

MOBI (and other Kindle-family formats once intake accepts them) metadata is read
with Calibre's `ebook-meta --to-opf` when it is on `PATH`. It runs through the
conversion runner with a 30 s timeout that kills the whole process group, and its
OPF output (capped at 2 MiB) is parsed with defusedxml. A missing tool, timeout,
failure, or unusable output falls back to filename signals and does not fail the
import (`ebook_meta_unavailable` / `ebook_meta_failed` are logged).

EPUB, PDF, and CBZ intake attempts front-cover extraction in the ingestion worker.
PDF pages are rendered by liberal-licensed PDFium bindings; archive images are
read with member size/compression limits; Pillow warnings for decompression bombs
are treated as failures. Cover failure is logged and does not fail or alter the
original import. Generated JPEG covers record their source variant and extractor
tool versions and are included in storage consistency checks.

Source-folder imports (not web uploads) prefer a sibling `cover.jpg`, `cover.jpeg`,
or `cover.png` (Calibre-style) over format extraction, but only when the folder
holds a single book: every intake-supported file in it, and every file previously
queued from it, must share the job file's name stem (`metadata.opf` and the cover
itself are ignored). A folder of several books never lends its cover to any of them.
The sidecar is untrusted input: symlinks and folders outside the source root are
ignored, files over 25 MiB or 40 megapixels are rejected, and the image is verified
and decoded by Pillow (JPEG/PNG only, decompression-bomb warnings are failures)
before the usual normalization. An invalid sidecar is logged
(`sidecar_cover_rejected`) and format extraction is used instead. Such covers record
`extractor_tool=sidecar+pillow`. The sidecar file is only read: it is left in place
for COPY *and* MOVE sources (MOVE removes only the verified book file), so a
reviewed MOVE import can still use it later. Disable with
`BOOK_SCANNER_SIDECAR_COVERS_ENABLED=false`.

Reviewers can edit title/author terms and use “Search providers again.” Repeat
searches are durable `ProcessingJob` records and do not depend on the original
staging file, which may already have been removed after verified archival. Poll
`GET /review-identifications/<job-id>/`; terminal dispatch/worker failures reopen
the task instead of leaving it permanently claimed.

Scan finalization (`POST /scan/jobs/<id>/finish/`) is queued and returns a durable
job identifier. Poll `GET /scan-finalizations/<job-id>/` or inspect the scan-job
pipeline payload. Finalization requires successful ScanTailor output for every
page by default. `allow_raw_pages=true` is an emergency/manual override; it is
recorded in variant provenance and should not be used for normal production scans.

An unlinked scan is routed through public bibliographic lookup before finalization.
Operators can start this explicitly with `POST /scan/jobs/<id>/identify/`; a finish
request starts it automatically when no canonical Book is linked. Poll
`GET /scan-identifications/<job-id>/`, then resolve the resulting shared
`ReviewTask` through the normal review queue. Candidate acceptance, manual entry,
and merge decisions link the ScanJob to the selected Book without inventing a
placeholder file variant.

## Routine checks

```bash
uv run python manage.py check
uv run python manage.py makemigrations --check --dry-run
uv run pip-audit --skip-editable
uv run pytest -q
```

Monitor queue depth, failed `ProcessingJob` and `ImportJob` records, provider/OCR
errors, storage capacity, and health endpoint status. Do not log document content.

## Metrics and logs

Set a long random `BOOK_SCANNER_METRICS_BEARER_TOKEN` in production and configure
Prometheus to scrape `/metrics/` with `Authorization: Bearer <token>`. Production
configuration fails closed when metrics are enabled without a token. The endpoint
derives its values from PostgreSQL and Redis, so web and worker state is visible
without relying on process-local counters.

Metrics include queue health/depth, collection size, import/processing/review/scan
state counts, ScanTailor stage status, processing-duration bucket populations,
and metadata-confidence bucket populations by provider. Labels use bounded model
enums or configured provider IDs; filenames, titles, usernames, and book contents
are never labels. Structured logs record provider latency/failure and processing
job lifecycle events without document content.

The deployable scrape example, alert rules, 30-day retention policy, and Grafana
dashboard are under `deploy/prometheus/` and `deploy/grafana/`. Validate them with
`promtool`, provision the dashboard against a Prometheus data source whose UID is
`prometheus`, and complete an actual scrape plus test Alertmanager notification
before declaring production monitoring accepted.

## Alert runbooks

### Book Scanner metrics target down

Check `/healthz/`, the ASGI process, proxy route, TLS certificate, Prometheus
target error, and bearer-token file. Do not print the token. Restore scraping,
then confirm `up{job="book-scanner"}` returns `1`.

### Book Scanner queue unavailable

Check Redis health and network policy from both web and worker hosts, then inspect
the configured URL and queue name. Avoid switching production to inline execution.
Once Redis is healthy, verify the queue `up` measure is `1` and workers consume a
small test job.

### Book Scanner queue backlog

Compare queue depth, worker count, job types, and oldest queued age. Inspect failed
worker logs and storage latency before scaling workers; do not retry jobs blindly
when the underlying converter, OCR service, or mount remains unhealthy.

### Book Scanner stuck jobs

Find the durable `ProcessingJob` by type/state and correlate its job ID with
structured worker logs. Verify worker liveness, timeout settings, external tool or
OCR response, and storage access. Preserve artifacts and provenance when retrying
or marking a genuinely abandoned job failed.

### Book Scanner workflow failures

Use the dashboard's 24-hour failure panel to identify the job/source/stage class,
then inspect bounded structured logs and the durable record's `last_error` or job
details. Check provider, converter, OCR, storage, and worker dependencies as
appropriate. Never include document contents in incident notes or metrics labels.

### Book Scanner review backlog

Break the backlog down by task type and age, confirm metadata providers are
healthy, and assign reviewers. Do not lower confidence thresholds merely to clear
the queue; uncertain matches must remain explicit human-review work.
