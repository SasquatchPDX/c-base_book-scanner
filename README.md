# Hackerspace E-Book Server

A Django-based shared e-book library and physical-book processing pipeline. The
application keeps canonical bibliographic `Book` records separate from stored
`FileVariant` artifacts, authenticates against LDAP, and coordinates ingestion,
conversion, scanning, ScanTailor-style image processing, and remote OCR through
persistent jobs.

## Development setup

Requirements:

- Python 3.13
- [uv](https://docs.astral.sh/uv/)
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
```

Development defaults use SQLite, local artifact storage, synchronous jobs, and
the explicit `stub` LDAP mode. Copy `.env.example` to `.env` and configure
PostgreSQL, Redis, storage, and LDAP before exercising production-like behavior.

Run the web process and an RQ worker:

```bash
uv run python manage.py runserver
uv run rq worker ebook-server --url redis://localhost:6379/0
```

The worker applies a 30-minute default job timeout, bounded result retention,
and configured retry intervals. Slow work must not use the no-Redis development
fallback in a deployed environment; production configuration validation requires
Redis.

## Main workflows

- `/accounts/login/` — LDAP-backed member sign-in
- `/library/` — member landing page and recent additions
- `/library/books/?view=html` — canonical metadata search and browse UI
- `/imports/upload/` — authenticated upload into the common intake pipeline
- `/reviews/?view=html` — low-confidence metadata review queue
- `/scan/jobs/?view=html` — scanner jobs and page capture
- `/scan/jobs/<id>/workspace/?view=html` — per-page ScanTailor workspace
- `/scan/jobs/<id>/finish/` — queue scan packaging and optional OCR
- `/scan-finalizations/<job-id>/` — durable scan-finalization status
- `/healthz/` and `/healthz/dependencies/` — liveness/readiness
- `/admin/` — Django administration for application operators

Workflow routes return JSON to API clients and HTML to browsers through content
negotiation. Rendered-page and form tests protect the browser workflows in CI.

Artifacts are never addressed by request-supplied absolute paths. Library and
scan data live below the configured storage root and are intentionally ignored by
Git. Originals and page versions are preserved; derived variants record lineage.
Scan finalization requires processed output for every page by default and runs in
the worker queue in production.

## Operations

See [docs/operations.md](docs/operations.md) for production configuration,
LDAP modes, network mounts, worker operation, consistency verification, and
security boundaries. Architecture and protocol documents live in `docs/`, while
the sprint specifications remain the authoritative feature requirements.

## Current maturity

The repository has broad automated coverage of the API and processing services,
but the sprint-completion table should not be treated as a production-readiness
certificate. Remaining requirement gaps and verification evidence are tracked in
[docs/requirements-audit.md](docs/requirements-audit.md).
