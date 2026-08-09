# Sprint002 — Foundation and Authentication

## Goal

Produce a running application with PostgreSQL, LDAP-backed login, roles, background-job infrastructure, storage configuration, health endpoints, and baseline testing.

## Tasks

- Configure application settings and environment-variable handling.
- Configure PostgreSQL and migrations.
- Implement LDAP authentication adapter using the existing LDAP server configuration.
- Do not add independent registration/password management.
- Store local application state keyed to LDAP identity.
- Implement `NORMAL_USER` and `ADMIN` authorization roles.
- Support local ban/disable state without modifying LDAP itself.
- Configure selected background-job queue and worker.
- Add Redis or other broker only if selected architecture requires it.
- Implement storage-backend configuration without yet building full archive ingestion.
- Add health/readiness endpoints for web app, database, queue, and configured storage reachability.
- Configure structured logging.
- Configure formatter, linter, type checking, test runner, and dependency/security scanning.
- Add CI configuration if repository environment supports it.

## Tests

- LDAP authentication success/failure through a test adapter or test LDAP environment.
- Role enforcement.
- Banned local user cannot access the application.
- Database migration smoke test.
- Background job can be enqueued and executed.
- Health endpoints report dependency failure correctly.

## Acceptance Criteria

- A hackerspace member can authenticate using LDAP and reach an empty library page.
- A non-LDAP identity cannot create a local account.
- Admin and normal-user permissions are distinguishable.
- A background worker successfully processes a trivial test job.
- Application starts cleanly from documented setup instructions.

## Out of Scope

- Book metadata schema beyond what is necessary for the empty UI.
- Uploads and ingestion.
- Search.
- Scanner integration.
