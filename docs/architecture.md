# Sprint001 Architecture

## Framework decision

**Selected backend**: Django + Django REST Framework (initially)  
**Selected job stack**: Redis + RQ (celery-like but lightweight)

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

### Chosen boundary map

- `ebook_server.core`: settings, storage abstraction, health endpoints, bootstrap checks.
- `ebook_server.library`: bibliographic entities, variants, identifiers, search and import models (future sprint002+).
- `ebook_server.scan`: scanner protocol, scan jobs/pages, ScanTailor workspace, bibliographic review, and finalization pipelines.
- `ebook_server.jobs`: queue registration and task dispatch layer over RQ.
- `ebook_server.workers`: asynchronous task code.

### External service dependencies

- LDAP server: authentication authority and group/identity source.
- PostgreSQL: source of truth for all structured project state.
- Network storage mount (NFS/Samba): canonical artifact archive storage.
- Public metadata providers: discovery and verification (not single-source authority).
- OCR AI server: asynchronous OCR service (later sprint012).

## Sprint001 bootstrap

- Repository now has a runnable Django configuration.
- A health endpoint exists and validates database/storage dependencies by default.
- Entry points are wired so `ebook-server` runs a Django command (defaults to `check` when no command is supplied).
