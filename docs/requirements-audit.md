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
- Scan completion is now a durable `ProcessingJob`: the request returns `202`,
  page mutations are locked while finalization runs, failures are retry-aware,
  and status is queryable. Raw captures are rejected as cleaned output unless an
  explicit, provenance-recorded override is requested.
- Core member workflows now have authenticated server-rendered pages: landing,
  search/browse, book detail and covers, upload/import status, metadata review,
  conversions, scan-job creation, page capture, rescan, and ScanTailor stage
  execution. JSON APIs remain available through content negotiation.
- Downloads, conversion, and OCR honor each artifact's declared storage backend;
  stale/failed variants are not advertised or usable. Locally disabled accounts
  are invalidated even when they still have an old session.
- Admin book deletion requires exact-title confirmation, verifies managed
  artifact hashes, quarantines files before the database transaction commits,
  restores them on failure, and then purges quarantine.
- The bearer-protected Prometheus endpoint reports persistent PostgreSQL/Redis
  queue and workflow state, processing-duration populations, ScanTailor status,
  and metadata-confidence distributions. Provider and worker lifecycle events use
  structured logs with bounded metadata and no document content.
- Unlinked physical scans now use durable provider lookup and the shared review
  queue. Finishing an unlinked scan automatically starts identification; owner or
  admin review then links the ScanJob to a canonical Book before artifact creation.
- Normal digital intake now covers EPUB, MOBI, PDF, TXT, HTML, FB2, DjVu, CBZ,
  and CBR with format-aware signature checks and bounded mature-library metadata
  parsing before mandatory external verification.
- EPUB, PDF, and CBZ imports generate normalized front covers with bounded image
  parsing, source-variant lineage, tool versions, storage checks, and graceful
  failure that preserves the original.
- “Search again” now performs a durable provider re-query for both archived
  digital imports and physical scans, replaces ranked candidates, preserves the
  reviewer’s refined terms/decision provenance, and enforces owner/admin access.
- Storage configuration advertises only implemented local, NFS, and Samba mount
  modes; unknown legacy backend values fail closed during backend resolution.
- Runtime artifacts are no longer tracked by Git. A read-only storage consistency
  command checks missing and hash-mismatched artifacts.
- Ruff, strict mypy, migrations, tests, and dependency auditing are CI gates.
- ScanTailor-Web now has a focused strict-TypeScript interaction layer using
  Interact.js, with direct split/content/dewarp/zone geometry, typed stage
  controls, selected/all-page batching, preserved manual exceptions, optimistic
  conflict handling, and authenticated SSE updates with polling fallback.
- Observability now includes recent failure and oldest-active-job metrics,
  validated Prometheus scrape/alert examples, operator runbooks, a bounded-label
  Grafana dashboard, and an explicit 30-day/size-capped retention policy.
- CI now provisions digest-pinned PostgreSQL, Redis, and a seeded OpenLDAP test
  directory. It applies real PostgreSQL migrations, verifies LDAP search/user
  binds and rejection, runs an actual RQ worker across Redis, and checks live
  database/queue/storage readiness rather than only configuration shape.
- The infrastructure suite now also installs a checksum-verified official Pandoc
  release and proves a member-requested TXT-to-EPUB conversion across Redis and a
  separately bootstrapped Django/RQ worker through authenticated download. Output
  variants retain source, converter/version, request, and ProcessingJob UUID
  provenance; unknown or tampered converter routes fail closed.
- A digest-pinned Nginx proxy and separate Uvicorn process now exercise the
  authenticated ScanTailor SSE route against PostgreSQL. CI proves the initial
  event is not buffered, a live revision/status change arrives on the same
  connection, required stream headers survive proxying, unauthorized access is
  redirected, and disconnecting a client leaves ASGI health intact.

## Remaining gaps by requirement area

- Async purity: development intentionally supports synchronous execution without
  Redis; deployed production is prevented from using that configuration.
- Observability acceptance: the repository provides metrics, alerts, dashboard,
  retention policy, and runbooks; importing them, verifying a real production
  scrape, and testing the site's Alertmanager notification path remain deployment
  work.
- Integration/E2E depth: Calibre-backed PDF/MOBI routes and a browser-driven
  concurrent-editor/full physical-book flow still need dedicated acceptance
  environments. PostgreSQL, LDAP, Redis/RQ, Pandoc TXT-to-EPUB, authenticated
  converted-file download, Nginx/Uvicorn SSE, and live dependency readiness now
  have infrastructure-backed CI coverage.

These items keep the full-project goal active; `docs/sprint_progress.md` is a
historical implementation log rather than an acceptance sign-off.
