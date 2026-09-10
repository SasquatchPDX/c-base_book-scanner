# Sprint001 Architecture

## Framework decision

**Selected backend**: Django; Django REST Framework is installed, while the
current HTTP routes use Django views and JSON responses.

**Selected job stack**: Redis + RQ

### Why Django over FastAPI for this project

- Mature admin and auth primitives for operational workflows (user role views, admin-only actions).
- Batteries-included migration tooling for the large domain model the project needs (`Book`, `FileVariant`, `ScanJob`, etc.).
- Strong convention support for server-rendered pages in version 1 (browse/search/download) while still allowing DRF endpoints for scanner and conversion APIs.
- Existing team familiarity with Django ecosystem and predictable testing story for database models and migrations.
- Less architecture overhead in early sprints: we can keep one modular monolith and avoid premature service boundaries.

### Why RQ over Celery/Dramatiq for v1 baseline

- Simple mental model: one queue, one Redis broker, minimal worker lifecycle.
- Better fit for the repository’s initial size and smaller operational surface.
- Sufficient for background tasks in sprint001–004 (hashing, metadata extraction, import orchestration) and extendable if queue needs grow later.

### Current module map

All domain models and migrations belong to the single Django app
`ebook_server.core`. Domain services are separate modules within that app:

- `models.py`, `admin.py`, and `migrations/`: library, scan, review, and job state.
- `intake.py`, `identification.py`, `review_identification.py`, and `covers.py`:
  intake, metadata evidence, review searches, and cover extraction.
- `conversion.py` and `ocr.py`: conversion planning and remote OCR orchestration.
- `scantailor.py`, `scan_pipeline.py`, and `scan_identification.py`: stage
  processing, scan finalization, and bibliographic resolution.
- `auth.py` and `middleware.py`: LDAP integration and local access checks.
- `storage_registry.py`, `book_deletion.py`, and `metrics.py`: backend resolution,
  confirmed deletion, and persistent workflow metrics.
- `views.py`, `urls.py`, and `templates/core/`: Django HTTP and rendered UI surfaces.

At package level, `ebook_server.settings` configures the application,
`ebook_server.storage` defines the storage protocol and filesystem backend,
`ebook_server.jobs` dispatches work through RQ, and `ebook_server.rq_settings`
bootstraps Django in workers. Worker entry points live in the domain service
modules. The focused ScanTailor TypeScript editor lives in `frontend/`.

The previously proposed `library`, `scan`, and `workers` packages do not exist.
Splitting the large view module is planned in Sprint020; it is not a prerequisite
for the Sprint019 corrections and does not require changing model app labels.

### External service dependencies

- LDAP server: authentication authority and group/identity source.
- PostgreSQL: source of truth for all structured project state.
- Network storage mount (NFS/Samba): canonical artifact archive storage.
- Public metadata providers: discovery and verification (not single-source authority).
- OCR AI server: asynchronous OCR service accessed through the OCR client protocol.

Storage choices are limited to local paths and externally mounted NFS/Samba
shares. Unknown legacy backend values fail closed; no unimplemented object-store
backend is advertised in administration.

## Sprint001 bootstrap

- Repository now has a runnable Django configuration.
- `/healthz/` and `/healthz/dependencies/` both check database, storage, and queue
  reachability, returning HTTP 503 when a dependency is down.
- Entry points are wired so `ebook-server` runs a Django command (defaults to `check` when no command is supplied).
