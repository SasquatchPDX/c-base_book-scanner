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

## Production configuration

Set `BOOK_SCANNER_ENVIRONMENT=production`. Startup then fails closed unless:

- the development secret is replaced;
- explicit allowed hosts are configured;
- PostgreSQL and Redis URLs are supplied;
- LDAP mode is `ldap`;
- HTTPS redirect, secure cookies, and HSTS are enabled.

When TLS terminates at a trusted reverse proxy, set
`BOOK_SCANNER_SECURE_PROXY_SSL_HEADER=true` and ensure that untrusted clients
cannot inject `X-Forwarded-Proto`. HSTS preload is optional and should only be
enabled after its long-lived operational consequences are understood.

## LDAP

`ldap3` performs the credential bind. Configure one of:

- `BOOK_SCANNER_LDAP_USER_DN_TEMPLATE`, for example
  `uid={username},ou=people,dc=example,dc=org`; or
- a search base/filter and optional service bind account.

Usernames are escaped for DN or filter use. Unknown LDAP modes and invalid LDAP
configuration fail closed. The `stub` adapter is strictly a development/test
facility and accepts any non-empty credentials.

Members sign in at `/accounts/login/` using LDAP credentials. There is no local
registration or local password. Disabling an `AppUserProfile` invalidates an
existing web session on its next request.

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

## Workers and external tools

Start RQ workers with the queue name configured by
`BOOK_SCANNER_QUEUE_NAME` (default `ebook-server`). Conversion routes only appear
when their executable is installed. Install Calibre and/or Pandoc in the worker
image, run converters without elevated privileges, and apply host/container CPU,
memory, process, and filesystem limits in addition to the application job timeout.

Configure the OCR service URL and network policy so only workers can submit jobs.
OCR responses are treated as untrusted artifacts and retain engine/model/language
provenance.

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
