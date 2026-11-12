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

The suite reserves host ports 55432, 56379, 55389, 58080, 58081, and 58082 by default.
Override the PostgreSQL, Redis, LDAP, and exposed Nginx ports with
`BOOK_SCANNER_INTEGRATION_POSTGRES_PORT`,
`BOOK_SCANNER_INTEGRATION_REDIS_PORT`, and
`BOOK_SCANNER_INTEGRATION_LDAP_PORT`, and
`BOOK_SCANNER_INTEGRATION_PROXY_PORT`; override the OCR fixture port with
`BOOK_SCANNER_INTEGRATION_OCR_PORT`. Port 58081 is the suite-internal host
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

Use the registered source's actual ID. A supported unattended schedule and
application preview/run/retry pages remain Sprint019 task I. COPY retains its
source even when review is needed. MOVE cleanup requires a verified archive; for
duplicates, verification uses the existing variant's declared backend.

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

EPUB, PDF, and CBZ intake attempts front-cover extraction in the ingestion worker.
PDF pages are rendered by liberal-licensed PDFium bindings; archive images are
read with member size/compression limits; Pillow warnings for decompression bombs
are treated as failures. Cover failure is logged and does not fail or alter the
original import. Generated JPEG covers record their source variant and extractor
tool versions and are included in storage consistency checks.

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
