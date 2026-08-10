# Hackerspace E-Book Server

A Django-based shared e-book library and physical-book processing pipeline. The
application keeps canonical bibliographic `Book` records separate from stored
`FileVariant` artifacts, authenticates against LDAP, and coordinates ingestion,
conversion, scanning, ScanTailor-style image processing, and remote OCR through
persistent jobs.

## Development setup

Requirements:

- Python 3.13
- Node.js 22.12 or newer (for ScanTailor-Web frontend changes)
- [uv](https://docs.astral.sh/uv/)
- Docker with the Compose plugin and `curl` (for infrastructure integration tests)
- Playwright Chromium (`npx playwright install chromium`) for browser acceptance
- EGL/OpenGL and XCB runtime libraries required by headless Calibre PDF output
- PostgreSQL and Redis for a production-like environment
- optional conversion binaries such as Calibre (`ebook-convert`) and Pandoc

Install and verify the project:

```bash
uv sync --frozen
uv run python manage.py migrate
uv run python manage.py check
uv run pytest -q
uv run mypy src tests
uv run ruff check .
uv run pip-audit --skip-editable
npm ci
npm run check
npm test
npm run build
```

Development defaults use SQLite, local artifact storage, synchronous jobs, and
the explicit `stub` LDAP mode. Copy `.env.example` to `.env` and configure
PostgreSQL, Redis, storage, and LDAP before exercising production-like behavior.

Run the infrastructure suite against ephemeral, digest-pinned PostgreSQL, Redis,
and seeded OpenLDAP containers:

```bash
./scripts/run_infrastructure_tests.sh
```

The runner applies all migrations to PostgreSQL, exercises a live LDAP
search-and-bind, starts real RQ workers against Redis, converts a member-requested
TXT file to a downloadable EPUB with a checksum-verified official Pandoc release,
converts a generated EPUB to validated PDF and MOBI variants with a
checksum-verified official Calibre release,
streams authenticated ScanTailor events through pinned Nginx and a separate
Uvicorn ASGI process, and drives two real LDAP member sessions through a
concurrent ScanTailor edit. The Chromium scenario uploads a synthetic page and
verifies that a stale editor receives a visible conflict instead of overwriting
the latest revision. A second Chromium scenario drives a synthetic physical book
through every ScanTailor stage, a rescan and downstream invalidation, provider
review, queued finalization, a separate HTTP OCR contract service, canonical
search, and authenticated OCR-result download. The runner also checks dependency
readiness and removes its containers on exit. Its known credentials and
high-numbered ports are isolated test fixtures, not deployment defaults.

Run the web process and an RQ worker:

```bash
uv run python manage.py runserver
uv run rq worker --config ebook_server.rq_settings --url redis://localhost:6379/0 ebook-server
```

The worker applies a 30-minute default job timeout, bounded result retention,
and configured retry intervals. Slow work must not use the no-Redis development
fallback in a deployed environment; production configuration validation requires
Redis.

Production web processes should serve `ebook_server.asgi:application` through an
ASGI server so ScanTailor live event streams do not tie up WSGI threads. The
repository includes Uvicorn; reverse-proxy requirements are documented in the
operations guide.

## Main workflows

- `/accounts/login/` — LDAP-backed member sign-in
- `/library/` — member landing page and recent additions
- `/library/books/?view=html` — canonical metadata search and browse UI
- `/imports/upload/` — authenticated upload into the common intake pipeline
- `/reviews/?view=html` — low-confidence metadata review queue
- `/review-identifications/<job-id>/` — durable repeat metadata-search status
- `/scan/jobs/?view=html` — scanner jobs and page capture
- `/scan/jobs/<id>/workspace/?view=html` — per-page ScanTailor workspace
- `/scan/jobs/<id>/identify/` — queue public metadata lookup for an unlinked scan
- `/scan-identifications/<job-id>/` — durable scan-identification status
- `/scan/jobs/<id>/finish/` — queue scan packaging and optional OCR
- `/scan-finalizations/<job-id>/` — durable scan-finalization status
- `/healthz/` and `/healthz/dependencies/` — liveness/readiness
- `/metrics/` — bearer-protected Prometheus metrics
- `/admin/` — Django administration for application operators

Workflow routes return JSON to API clients and HTML to browsers through content
negotiation. Rendered-page and form tests protect the browser workflows in CI.
Normal intake accepts EPUB, MOBI, PDF, TXT, HTML, FB2, DjVu, CBZ, and CBR. Each
format receives signature/container validation before metadata parsing, and every
file still goes through external bibliographic verification. EPUB, PDF, and CBZ
imports generate bounded normalized front covers when one can be extracted safely.

Artifacts are never addressed by request-supplied absolute paths. Library and
scan data live below the configured storage root and are intentionally ignored by
Git. Originals and page versions are preserved; derived variants record lineage.
Scan finalization requires processed output for every page by default and runs in
the worker queue in production.

## Operations

See [docs/operations.md](docs/operations.md) for production configuration,
LDAP modes, network mounts, worker operation, consistency verification, and
security boundaries. Nginx streaming-proxy, Prometheus alert/scrape, and
provisioned Grafana examples live in `deploy/`. Architecture and protocol
documents live in `docs/`, while the sprint specifications remain the
authoritative feature requirements.

## Current maturity

The repository has broad automated coverage of the API and processing services,
but the sprint-completion table should not be treated as a production-readiness
certificate. Remaining requirement gaps and verification evidence are tracked in
[docs/requirements-audit.md](docs/requirements-audit.md).
