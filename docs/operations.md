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

New scan jobs default to the `single_page` capture layout, which is what the
project's scanner produces. In that layout, ScanTailor's Page Split completes
automatically as a recorded "no split" passthrough. If a scanner photographs
two-page spreads, list its id in `BOOK_SCANNER_SCAN_SPREAD_SCANNER_IDS` (a JSON
list). To change the default for every scanner, set
`BOOK_SCANNER_SCAN_DEFAULT_CAPTURE_LAYOUT=spread`. See `docs/scantailor-web.md`.

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

### Deployment topology: one attached storage, the VM holds only state

The application VM keeps only PostgreSQL, Redis, caches and short-lived temp
files. Every book byte lives on **one attached storage**: a writable network share
(NFS/SMB) mounted on the application and worker hosts before processes start.
Point `BOOK_SCANNER_STORAGE_ROOT` at that mount. The web and every worker must see
the same paths. Do not place the production collection inside the source checkout
or on the application VM root disk.

Layout under `BOOK_SCANNER_STORAGE_ROOT`:

| Path | Contents |
| --- | --- |
| `library-artifacts/` | The archive (`default` storage location): originals from COPY/MOVE sources and web uploads, plus **every derived artifact** (conversions, covers, OCR output), including those derived from indexed-in-place files. |
| `scan-artifacts/` | Scanner page captures and ScanTailor-Web stage outputs. |
| `uploads/` | Web-upload staging before archiving. |

The archive directory name is fixed (`BOOK_SCANNER_ARCHIVE_DIRNAME`) so the archive
always sits behind the storage-root mount and sentinel checks below; it is not a
separate setting that could point back at the VM disk.

An existing collection does **not** have to be copied into `library-artifacts/`.
Register it as an index-in-place source (see "Source transfer modes") and the
library references each file where it already is, read-only.

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

### Batching, rate limits, and pausing a source

`ingest_sources` never queues a whole source at once. Each run queues at most:

- the source's **batch size**: import jobs in flight (DISCOVERED through
  ARCHIVING; LOW_CONFIDENCE jobs waiting for review do not count). Set it per
  source in admin (`ingest_batch_size`) or globally with
  `BOOK_SCANNER_INGEST_BATCH_SIZE` (default 100); `0` means unlimited. `--limit N`
  overrides it for one run (`--limit 0` = unlimited).
- the source's **hourly rate**: new import jobs created in the last rolling hour
  (`ingest_max_per_hour` per source, or `BOOK_SCANNER_INGEST_MAX_PER_HOUR`;
  default `0` = unlimited). Use it to keep a large one-off migration from
  flooding external metadata providers.

Files beyond the limit are picked up by later runs, so run `ingest_sources` on a
schedule (cron/systemd timer) to work through a large source gradually.

To pause a source, use the "Pause imports" admin action (or set `is_paused`).
A paused source queues nothing, and queued jobs that have not started yet are
skipped by the worker and stay DISCOVERED. After "Resume imports", the next
`ingest_sources` run re-queues them. Each run also re-queues DISCOVERED jobs that
no longer have an RQ job (for example after `rq empty` or a Redis flush). If the
RQ queue cannot be inspected, re-queueing is skipped with a warning.

A job that ends up queued twice is still processed only once. A worker first
claims the job's lease (`ImportJob.lease_owner` / `lease_expires_at`) with one
conditional update, which succeeds only for an unowned job waiting to run
(DISCOVERED, or FAILED with a queue retry due). Any other delivery returns
without doing anything, whichever phase it finds the job in (logged as
`intake_duplicate_delivery_skipped`). Each later write of the job locks its row,
checks that the worker still owns the lease, and renews it. Leaving the active
phases (COMPLETE, LOW_CONFIDENCE, FAILED) releases the lease. The lease lasts
`BOOK_SCANNER_INGEST_LEASE_SECONDS`; the default `0` means the RQ job timeout
plus five minutes. A job whose lease expired is never taken over by another
delivery. `recover_stale_jobs` handles it, see below.
`--preview` lists every importable file and every skipped file, followed by a
per-source summary such as `importable=108 skipped=3 (.chm=2, .lit=1)`. Skipped
files are unsupported formats (`SKIPPED (unsupported format: .chm) => <path>`) and
audio files (see "Audio files and audiobooks" below); the per-extension counts cover
both. With `--json`, rows carry `"status": "importable"` or `"status": "skipped"`;
skipped rows add `"skipped": "unsupported"` or `"skipped": "audio"`, `extension` and
`reason`, and each source ends with a
`{"source_location_id", "summary": {"importable", "skipped", "skipped_audio",
"skipped_by_extension"}}` row.

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

### Formats of one book (`group_sibling_formats`)

Opt-in per source (admin → Source locations → "Formats of one book"), because
generic same-name pairs such as `notes.txt` / `notes.pdf` are not one book in every
collection. When enabled, files in the same folder with the same file name and
different extensions (`Dune.epub`, `Dune.mobi`, `Dune.txt`; names compared
case-insensitively) are *siblings*. Once one sibling is linked to a Book (automatic
match, exact duplicate, or review decision), that Book becomes a strong candidate
for the others. It never replaces verification:

- every file is still looked up externally, and its provider candidates are stored;
- the sibling's Book is scored like a provider candidate, plus
  `BOOK_SCANNER_IDENTIFICATION_SIBLING_BONUS` (default 0.35), and must reach the
  auto-accept threshold. An equal title is enough; the shared name alone is not;
- the file goes to review instead when its embedded ISBN conflicts with the Book's
  ISBNs (`conflicting_isbn`), when its embedded authors name different people than
  the Book's (`author_conflict`), when a provider candidate confidently matches a
  different book (`provider_match_differs`), or when the score stays below the
  threshold (`below_threshold`);
- siblings already waiting in review are re-assessed with the same rule when another
  format is linked. An OPEN task that passes is resolved as "Merged with existing
  book" without a reviewer, with `rule: group_sibling_formats` and the score in its
  decision payload. A task a reviewer has CLAIMED is never resolved automatically;
  it only gets a note under `decision_payload.format_sibling`.

The assessment (sibling job, Book, score, accepted, reason) is stored in the import
evidence under `format_sibling_of`. Decisions within one source are serialised with a
row lock on the source, so two formats finishing at the same time cannot create two
Books.

The review queue (`/reviews/`) shows siblings waiting in review as one entry. The
primary task is the one with the best candidate score, then the format most likely
to carry embedded metadata (EPUB, AZW3, MOBI, FB2, PDF, DjVu, HTML, RTF, TXT), then
the oldest. The other formats are listed under it with their own candidate counts and
links. The JSON response keeps one item per task, so existing clients are unaffected.
Each item gains `sibling_group` (`primary_task_id`, `task_ids`, `size`, or `null`),
and the response gains `group_count`.

### Stopping (cancelling) a source import

Do not stop a bulk import with `rq empty`: it leaves DISCOVERED rows behind, and
RQ's abandoned-job handling can still re-run the job that was in progress when
the worker stopped. Instead run:

```bash
uv run python manage.py cancel_source_import --source-id 1          # dry run
uv run python manage.py cancel_source_import --source-id 1 --apply  # act
```

`--apply` pauses the source and marks its import jobs that have not started
archiving `CANCELLED`. It then deletes their RQ jobs from the queue and from the
started, scheduled, deferred and failed registries. A cancelled job never runs again,
even if RQ redelivers it after a worker restart. A job that is running when it is
cancelled stops at its next phase boundary without archiving, creating a review
task, or creating a Book, and is not retried. Cancelling clears the job's lease
and locks its row. Each phase save of the worker locks the same row before
checking the state, so a cancellation never commits between that check and the
save. Jobs already `ARCHIVING` are left
to finish, because a MOVE source may already be in the archive; `LOW_CONFIDENCE`
jobs waiting for review are untouched. To import the cancelled files later,
resume the source and run `ingest_sources`; they are discovered again.

Use the registered source's actual ID. A supported unattended schedule and
application preview/run/retry pages remain Sprint019 task I. COPY retains its
source even when review is needed. MOVE cleanup requires a verified archive; for
duplicates, verification uses the existing variant's declared backend.

Per-source progress (files and bytes still waiting to be copied, or for
index-in-place sources hashed, plus review/complete/failed counts):

```bash
uv run python manage.py ingest_sources --status
uv run python manage.py ingest_sources --source-id 1 --status --json
```

### Source transfer modes

Choose the mode per source in Django admin (`Source locations`). Existing sources
keep their mode; new sources default to COPY.

| Mode | What intake does with the file | Source access needed |
| --- | --- | --- |
| `copy` | Copies it into `library-artifacts/`, keeps the source. | read |
| `move` | Copies it into `library-artifacts/`, deletes the source only after the copy's SHA-256 is verified. | read + delete |
| `index` (index in place) | Hashes and identifies it, then records a FileVariant that **references the file where it is**. Nothing is copied; nothing on the source is ever written, moved or deleted. | read |

Use `index` for a collection that already lives on the attached storage, or on
another long-lived read-only share (for example an existing Calibre library tree:
its `Author/Title (id)/` folders are kept as they are). Use `copy` for a transient
source that will go away. Identification still reads the file at its original
path, so folder-layout hints (`path_layouts`, e.g. the Calibre template above) and
sidecar `cover.jpg` files work for indexed sources exactly as for copied ones.
Collections in any other layout are also indexed as they are; the application does
not reorganize or move files on the share.

Index in place details:

- The source gets a **read-only storage location** (`StorageLocation.read_only`).
  By default one named `source-<id>` is created from the source root on first
  import. To index several sub-folders of one share under a single location,
  create a read-only storage location for the share root first and select it as
  the source's `index storage location`; the source root must lie inside it.
- Each referenced variant stores that location plus the path relative to it, the
  size, the mtime and the SHA-256. Database identifiers stay authoritative;
  changing a source's `root_path` later does not break variants already indexed.
- Downloads, conversions, cover extraction and OCR read through the storage
  service from that location. Derived artifacts are written to
  `library-artifacts/`, never onto the read-only share.
- Exact-duplicate detection is by SHA-256 across archived and referenced
  variants. A MOVE source whose only duplicate is a referenced file is **kept**:
  the application does not control that copy.
- Deleting a book removes only the database records of referenced files; the
  files stay on their share.
- Free-space budgeting ignores index sources: they add no archive bytes.

#### Drift check for referenced files

The application does not own referenced bytes, so check them regularly (systemd
timer or cron, e.g. hourly; `--verify-hashes` off peak):

```bash
uv run python manage.py check_referenced_files
uv run python manage.py check_referenced_files --verify-hashes --json
uv run python manage.py check_referenced_files --location source-3
```

Files whose size or mtime changed are re-hashed: identical content stays served
(the new mtime is recorded); different content is flagged `changed`, an absent
file `missing` (`FileVariant.reference_state`). Every download, conversion and OCR
request also compares size/mtime first and flags on mismatch. Flagged variants
return HTTP 409 for downloads and are refused as conversion/OCR sources; they are
never deleted. The command exits non-zero when it finds drift or an outage.

**Mount readiness.** An unmounted share usually still leaves its mount point as an
(empty) directory, so "the directory exists" proves nothing. Before checking any
variant on a read-only location, the check requires:

- the base directory to exist, and
- the location's `sentinel_file` (set it in admin, `Storage locations`) to exist
  under the base path, or, when no sentinel is configured, the base directory to be
  non-empty.

Configure a sentinel for every index share (e.g. create an empty
`.ebook-index-sentinel` at the share root and enter that name): it also catches a
mount point that has stray local files in it. A location that fails this check is
an **outage**: nothing on it is flagged, its variants are reported as errors and
listed as `UNAVAILABLE storage_location:<identifier>`, a
`referenced_storage_unavailable` error is logged, and a queued check job ends
`failed` (`ebook_server_processing_jobs{job_type="reference_check",state="failed"}`).
A download or conversion that finds the file absent applies the same test, so it
is refused (409) without flagging during an outage.

**Re-checking from the admin.** The admin actions never check files in the web
request (a check stats and may re-hash every file). "Queue re-check of selected
referenced files" (`File variants`) and "Queue re-check of all referenced files on
selected locations" (`Storage locations`) record a `reference_check`
`ProcessingJob` scoped to the selection and hand it to the RQ worker; the message
links the job, whose `details` hold the report (counts, affected variant ids)
once it finishes. Without a configured queue the job stays `queued`; run
`uv run python manage.py check_referenced_files --run-queued` to process it.

Resolve flagged variants in Django admin (`File variants`, filter by reference
state): restore the original file and queue a re-check, or delete the stale
variant record and re-run `ingest_sources` so the new file is indexed as a new
variant. A changed file at an already indexed path is not silently repointed;
its import fails with a message pointing at the drift check until the old record
is resolved. `ebook_server_referenced_file_variants{reference_state=...}` exports
the counts for alerting.

Imported variants record their origin in `FileVariant.source_type`: `upload` for
web uploads, otherwise the source location's `default_source_type`
(`incoming_folder` or `network_source`). An exact duplicate reuses the existing
variant and keeps that variant's original origin.

### Books split across per-chapter files (multi-part works)

Some collections store one book as many files in one folder (`ch01.pdf` …
`ch20.pdf`, `Appendix1.pdf`, `FM.pdf`, `Index.pdf`). Source discovery detects these
from file names only: at least three files of one format in the same folder whose
names share a prefix followed by a chapter/part/section/lesson token
(`ch`, `chap`, `chapter`, `kapitel`, `part`, `pt`, `teil`, `sec`, `section`,
`lesson`, `lektion`) and distinct numbers (digits, or roman numerals after a
separator). Appendices (`AppA`, `Appendix 2`) and front/back matter (`FM`, `toc`,
`preface`, `glossary`, `index`, …) join a detected group but never form one.
Volume/book/band numbers are not part tokens (those are usually separate books),
and the *folder* name is ignored: a single complete novel in a folder called
`CHAPTER ONE` is not a split work. On the two real-world listings in
`tests/fixtures/real_world_samples/` the detector flags exactly the two known split
books and nothing in the 1,136-folder Calibre library.

Each part is still hashed, validated and archived on its own (COPY keeps the source;
MOVE removes it only after the archive copy verified). Instead of identifying every
chapter separately, the parts wait in `low_confidence` and, once all of them have
settled, one review task of type `multipart_work` is opened for the whole folder,
searched by the folder name. `ingest_sources --preview --json` shows
`multipart_group`/`part_label` for files that would be grouped.

"All of them" is the membership detected from the folder listing (stored on the
group with each file's source path), not only the jobs created so far: if a byte
budget stopped discovery after some parts, the group waits until a later run
imports the rest. A part whose import failed, whose source disappeared, or which was
imported on its own is shown as *missing*; confirming the group then requires the
reviewer to acknowledge the incomplete set first (**Confirm without the missing
files**), and the decision records which files were left out. A file of an already
confirmed or rejected group that turns up later is imported on its own.

A part whose bytes are identical to a file already attached to *another* Book is not
silently resolved onto that Book (which used to drop it from the group). It is
archived for the group like every other part and shown on the review as a
*duplicate* part, naming the other Book. Confirmation is blocked until the reviewer
acknowledges it (the same acknowledgement as for missing parts). Confirming then
makes it a part `FileVariant` of the group's Book, backed by the part's own
verified archive copy, with `converter_options.multipart_identical_to` naming the
identical variant and its Book. The other Book and its variant are not changed:
the chapter belongs to this work, and Book != FileVariant. Two cases reuse the
existing file instead of keeping a second copy, and delete the part's redundant
archive copy (never a source and never a file referenced in place):

- the group is linked to the Book that already holds those bytes, and
- the grouping is rejected, so the part is an ordinary import and the normal
  exact-duplicate rule applies.

The check runs at every membership evaluation, so a file imported elsewhere while the
review is open is also caught before confirmation.

The reviewer can fix the proposed order and labels, then either

- accept a candidate, enter metadata, or link an existing book: every part becomes
  its own `FileVariant` of that one Book with `part_index`/`part_label` set (book
  pages list them in order; parts are never offered as single-chapter conversions),
  or
- choose **Not one book**: the group is rejected and every file is identified as an
  independent import in the background (auto-accepted only under the normal rules,
  otherwise an ordinary review task). A part still being processed when the grouping
  is rejected is identified on its own as soon as its job finishes.

Once confirmed, a book page offers **Combine N parts into one file** for PDF and TXT
part sets (`POST /library/books/<id>/combine-parts/` with `target_format`; poll
`GET /combine-jobs/<job-id>/`). A background job re-hashes every part against its
recorded SHA-256, joins them in `part_index` order (pypdf for PDF, adding one
bookmark per part label; charset-normalizer-decoded UTF-8 text for TXT) and stores
a new `generated` FileVariant. That variant records the first part as
`source_variant`, the tool and version, the job ID and, in `converter_options`, the
ordered part variant IDs, hashes and labels. Repeat requests reuse it. If the
output happens to equal a file of another book, it is still stored and recorded for
the requested book. Staging counts the bytes actually read: a part larger than its
recorded size, or a set over the combine size limit, fails the job without being
copied in full. At most one combine job per book and format is queued or running
(the request locks the Book row; a partial unique constraint enforces it). It is a
normal conversion source (for example PDF → EPUB). Other formats have no safe
concatenation and are not offered.

Originals are never merged, rewritten or deleted by either path. Completeness cannot
be verified automatically; gaps in the numbering are shown on the review page.
Disable detection with `BOOK_SCANNER_MULTIPART_DETECTION=false`.

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

## Reviewing imports in bulk

Large source imports create many review tasks. The review queue
(`/reviews/?view=html`, or `GET /reviews/` as JSON) supports this work:

- Filters: `source` (`upload`, `scan`, or a `SourceLocation` id), `state`
  (`open`/`claimed`), `min_score`/`max_score` on the top candidate's confidence
  (0–1), and `has_candidates` (`yes`/`no`).
- Sorting: `sort=-created` (default), `created`, `-confidence`, `confidence`;
  tasks without candidates sort last.
- Pagination: `page` and `page_size` (default
  `BOOK_SCANNER_REVIEW_QUEUE_PAGE_SIZE`, 50; capped at 200). JSON responses
  carry `count` (all matching tasks), `page`, `num_pages` and `page_size`.

Selecting tasks and choosing **Accept top candidate for selected** posts to
`POST /reviews/batch-accept/` (`task_ids` as repeated form fields or a JSON list;
an optional `min_score` may only raise the threshold). Each task is checked
individually with the same permission rule as single-task actions (admins: all
tasks; members: their own uploads and scan jobs) and is resolved through the same
code path as accepting a candidate on the detail page. A task is accepted only if:

- its top candidate scores at least `BOOK_SCANNER_REVIEW_BATCH_MIN_SCORE`
  (default 0.75), and
- the runner-up trails by more than `BOOK_SCANNER_REVIEW_BATCH_AMBIGUITY_MARGIN`
  (default 0.05); exact ties are always ambiguous.

Everything else stays open and is reported per task (`accepted`, `ambiguous`,
`below_min_score`, `no_candidates`, `closed`, `forbidden`, `not_found`,
`requires_individual_review`, `error`). Multi-part works require their individual
review page to confirm membership, missing or duplicate parts, and reading order;
batch acceptance always leaves those tasks unchanged. A batch holds at most
`BOOK_SCANNER_REVIEW_BATCH_MAX_TASKS` (default
100) tasks. Batch-resolved tasks record `batch_accept`, the scores and the
thresholds used in `decision_payload`. The originals were already archived when
the review task was created, so accepting only links records; no archive copy,
parsing or rendering happens in the request. Front-cover extraction for each
accepted file is a background job (`intake.extract_front_cover_job`) queued after
that task's transaction commits, exactly as for a single accept; see "Covers from
review decisions" below. Batch accept never changes the intake auto-accept
threshold (`BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT`).

The queue shows formats of one book (see `group_sibling_formats`) as one entry,
grouped within the current page; only that entry has a batch checkbox, and the
other formats are re-assessed when it is accepted.

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

It also lists import jobs stuck in an active phase (HASHING through ARCHIVING)
whose lease has expired, and import jobs without a lease (left by a worker from
before leases existed) that have not changed for one lease duration. `--apply`
marks them FAILED (`import lease expired in <phase>`) and clears the lease. The
update is conditional, so a worker that renewed its lease in the meantime is not
touched, and a worker that lost its lease stops at its next write. These jobs are
not retried automatically, because a MOVE source may be half moved. Check the
source, then re-queue them with `ingest_sources --source-id N --retry-failed`.

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

Digital intake accepts EPUB, MOBI, AZW, AZW3, PRC, RTF, PDF, TXT, HTML, FB2,
DjVu, CBZ, and CBR. AZW/AZW3/PRC must carry a PalmDB `BOOKMOBI` header (PRC may
also be plain PalmDOC `TEXtREAd`); RTF must start with `{\rtf`. AZW/AZW3/PRC
metadata is read with Calibre's `ebook-meta` like MOBI; RTF metadata currently falls
back to filename/folder signals before external verification. These four formats
are stored and downloadable as uploaded, but no conversion route is offered from them
until one is binary-verified (see `docs/conversion-matrix.md`).
Files in any other format (for example CHM, LIT, LRF, PDB, DOC/DOCX, ZIP/RAR) are
not imported: source discovery logs a `source_files_skipped_unsupported` warning
with per-extension counts, and `--preview` lists them (see above).
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

### Covers from review decisions

Resolving an import in review (accept candidate, merge, manual entry, batch accept,
or a format sibling linked automatically) does not extract the cover in the web
request. It queues `ebook_server.core.intake.extract_front_cover_job` with
`transaction.on_commit`, so the job is dispatched only once the decision is committed
(a rolled-back decision queues nothing) and runs without holding any row locks. The
job is idempotent: the Book's existing front cover is reused, so a repeated delivery
does no storage work. Failing to dispatch is logged as
`cover_extraction_dispatch_failed` and never undoes the decision. Without a configured
queue (development only; production requires Redis) a single decision extracts inline
after commit, while batch accept skips extraction and logs
`cover_extraction_skipped_no_queue`.

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

A sibling Calibre-style `metadata.opf` is read as extra identification evidence
under the same single-book-folder rule (`intake.single_book_folder`), so the cover
and metadata sidecars always agree about a folder. A stray OPF in a folder of
several books is ignored (`sidecar_metadata_ignored`), and so is a `metadata.opf`
the application generated itself, recognised by
`<meta name="ebook-server:generated" content="true"/>`
(`reason=generated_by_ebook_server`). The OPF is parsed by the same
defusedxml parser used for `ebook-meta` output. Symlinks, files over 2 MiB,
malformed XML, and entity declarations are rejected (`sidecar_metadata_rejected`)
and treated as "no sidecar"; they never fail the import. Sidecar values are kept
under `evidence.sidecar_metadata` (`fields`, `applied_fields`, `conflicts`). They
only fill gaps: empty fields, placeholders, a title the extractor marked as a
filename fallback, and an ISBN added as `isbn` when the file has none. Every
extraction records `evidence.title_source` (`embedded`, `filename`, or `sidecar`
once a sidecar filled it); an embedded title equal to the filename stem is still
embedded and is never replaced. They never overwrite embedded metadata. If
the sidecar's ISBN, title, or authors contradict the file's embedded metadata,
nothing is merged and the import goes to manual review even when the top candidate
clears the auto-accept threshold (`sidecar_metadata_conflict`). Disable with
`BOOK_SCANNER_SIDECAR_METADATA_ENABLED=false`.

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

## Library search index

`/library/books/` searches a denormalised row per book (`BookSearchDocument`
plus `BookSearchFacet`, migration 0033, #28). On PostgreSQL it uses full-text
search (`simple` configuration over accent- and case-folded text), `pg_trgm`
substring and typo matching, and GIN/B-tree indexes. On SQLite, and on
PostgreSQL until every book has a row, it falls back to plain `icontains`
filters (slow on large libraries, but correct).

Rows are rewritten automatically after every committed change to a book, its
authors, genres, categories, identifiers, covers or file variants. Build them
once after upgrading to migration 0033, and rebuild nightly as a safety net:

```bash
uv run python manage.py rebuild_search_index     # all books; safe to repeat
uv run python manage.py verify_search_index      # exit 1 on missing/stale rows
uv run python manage.py verify_search_index --fix
```

Queryset `.update()` and `bulk_*` writes send no model signals; code that changes
indexed data that way calls `search.schedule_reindex` *after* the write. The
verifier rebuilds a sample of rows in memory and compares every column, the
facets and a SHA-256 digest of the weighted full-text input (so a changed
description or publisher is caught), and on PostgreSQL also the stored
`tsvector` itself; use `--sample` larger than the library to check every row.

The migration creates the `pg_trgm` extension, which needs a role allowed to
`CREATE EXTENSION` (pg_trgm is a trusted extension, so the database owner is
enough on PostgreSQL 13+). Browse pages use signed `?cursor=` tokens; `?page=`
still works for one release and answers with a `Deprecation: true` header.
Global facet counts and the unfiltered total are cached for up to five minutes.
Search logs record the backend, sort, result count and duration, never the
query text.
`ebook_server_search_index_lag_seconds` (age of the oldest book without a search
row) and `ebook_server_library_derived_rows_missing{kind="search_documents"}`
should stay at 0; anything else means indexing failed for those books.

### Landing page and author index

`/library/` is search-first (#28): a search box, a facet sidebar with global
counts (format, language, genre, category, author, decade, cover), shelves
("Recently added", "Recently identified" from review, and "New in <language>"
for the second and third most common languages) and an author A–Z index at
`/library/authors/`. Every facet link opens `/library/books/` with an exact
filter: `format`, `language`, `genre_id`, `category_id`, `author_id`, `decade`
(`1990` for 1990–1999) or `has_cover` (`1`/`0`). The global counts, the total
and the language shelves are cached for five minutes; "Recently added" and
"Recently identified" are live. Everything works without JavaScript.

### Cover thumbnails

Lists and shelves show 120 px and 240 px wide WebP thumbnails instead of the
full covers. They are derived, regenerable files (`CoverThumbnail`, migration
0034) in the `cover-thumbnails` storage location, a cache on the application
VM. Its directory is `BOOK_SCANNER_COVER_THUMBNAIL_ROOT` (default
`<storage root>/cover-thumbnails`), copied into the location's `base_path`
when the location is first created; change it later in the admin. Expect about
15 KB per book.

A background job renders them whenever a front cover is stored. After the
upgrade to migration 0034, and whenever the
`ebook_server_library_derived_rows_missing{kind="cover_thumbnails"}` metric is
above zero, backfill them:

```bash
uv run python manage.py generate_cover_thumbnails            # missing or stale only
uv run python manage.py generate_cover_thumbnails --enqueue  # one RQ job per cover
uv run python manage.py generate_cover_thumbnails --force    # regenerate all
```

Thumbnail URLs (`/library/covers/<sha256>/<size>.webp`) contain the file's
hash and are served with `Cache-Control: private, max-age=31536000, immutable`.
Django always checks the session and the member's status. With
`BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX=/_protected/cover-thumbnails/` it
then hands the file to nginx (`X-Accel-Redirect`) instead of streaming it from
a Python worker; see `deploy/nginx/README.md`. A cover that cannot be decoded
keeps the full-size cover in lists and is logged as `cover_thumbnail_failed`.

A job publishes only when the cover still has the bytes it rendered. It checks
the cover's current hash under a lock on the cover row, so a slow job for a
replaced cover ends as `stale` and leaves the newer thumbnails alone.
Thumbnail files are content-addressed and shared by identical covers, so they
are never deleted inline. When the last row referencing a file is replaced or
deleted, the file's `CoverThumbnailFile` registry row is marked unreferenced.
A periodic collection deletes files that stayed unreferenced for longer than
`BOOK_SCANNER_COVER_THUMBNAIL_GC_GRACE_SECONDS` (default one day). It re-checks
references under the same registry row lock that publication takes, and a
publisher that finds a file collected writes it again. Run collection from cron
or a systemd timer, for example hourly:

```bash
uv run python manage.py collect_cover_thumbnails
uv run python manage.py collect_cover_thumbnails --grace-seconds 3600 --limit 5000
```

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
