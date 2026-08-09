# Requirements Audit

This audit compares the implementation to `AGENTS.md` and Sprint001–Sprint018.
Passing tests are evidence for the paths they exercise, not proof that an entire
sprint or production deployment is complete.

## Verified and strengthened

- The canonical `Book` / `FileVariant` separation, source lineage, scan page
  versioning, review tasks, conversion graph, OCR jobs, and stage-state dependency
  model exist in migrations and have automated coverage.
- LDAP now has a real `ldap3` bind/search adapter and fails closed for unknown
  modes. The prior implementation silently selected the permissive stub for every
  non-stub mode.
- Required processing libraries are installed rather than silently falling back:
  pypdf, defusedxml, OpenCV, NumPy, and img2pdf.
- EPUB metadata reads have entity-expansion and archive-member size/compression
  defenses. OCR upload streams from disk instead of loading the entire source in
  memory.
- Production settings validate PostgreSQL, Redis, LDAP, secrets, hosts, HTTPS,
  secure cookies, and HSTS. PostgreSQL now has an installed psycopg driver.
- RQ dispatch applies a default timeout, retention limits, and retry policy.
- Runtime artifacts are no longer tracked by Git. A read-only storage consistency
  command checks missing and hash-mismatched artifacts.
- Ruff, strict mypy, migrations, tests, and dependency auditing are CI gates.

## Remaining gaps by requirement area

- User-facing UI: most workflows are JSON endpoints; the server-rendered library,
  review, scanner, and ScanTailor workspace interfaces described by the sprints
  are not yet complete.
- Intake breadth: normal intake currently accepts EPUB and PDF, not every domain
  format represented by `FileVariant`. Cover/thumbnail extraction is incomplete.
- Administration: explicit, confirmed library deletion with storage/database
  consistency handling is not implemented.
- Physical pipeline: completion can still package raw pages when a successful
  ScanTailor output is absent, and an unlinked scan job does not yet enter the
  bibliographic identification/review pipeline automatically.
- Async purity: development intentionally supports synchronous execution without
  Redis; deployed production is now prevented from using that configuration, but
  scan-job completion still performs cleaned-PDF assembly in the request path.
- Observability: structured event/metric coverage for queue depth, confidence
  distributions, providers, conversion, OCR, and ScanTailor duration/failure is
  incomplete.
- Integration/E2E depth: PostgreSQL, a real LDAP test directory, real worker
  processes, external converter binaries, and a browser-driven full physical-book
  flow need dedicated integration environments.
- Storage backends: mounted filesystems are supported through local paths, but S3
  is declared in the model without an implemented backend and should not be
  advertised as operational.

These items keep the full-project goal active; `docs/sprint_progress.md` is a
historical implementation log rather than an acceptance sign-off.
