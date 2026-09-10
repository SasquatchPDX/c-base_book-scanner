# Requirements Audit

This audit compares the implementation to `AGENTS.md` and Sprint001–Sprint020.
Passing tests are evidence for the paths they exercise, not proof that an entire
sprint or production deployment is complete.

## Sprint019 correction pass — 2026-09-10

All 34 project Markdown documents were reread, excluding installed dependency
documentation and generated caches. Sprint019 is in progress; Sprint020 is a
future specification, including where its prose assumes Sprint019 is complete.
Existing working-tree intake and browser-test work was preserved.

Corrections implemented in this pass:

- **A:** the existing strict auto-accept fix remains in place. The two failing
  COPY/MOVE tests now require review for their weak candidates and verify archive
  hashes and source retention/deletion. They no longer require unsafe acceptance.
- **F:** signal normalization, matching, and the pre-archive low-confidence phase
  are persisted in order. Tests observe both accepted and review paths through the
  HTTP status endpoint at each saved transition. This is current-state reporting,
  not an append-only transition history.
- **L:** duplicate cleanup resolves the variant's declared storage backend.
  Tests cover valid, corrupt, and missing archives under COPY and MOVE, including
  a misleading matching artifact on the default backend.
- **L:** existing-page staging and legacy replacement, scan ZIP packaging, and
  OCR downloads stream bytes. Tests reject unbounded source reads, verify HTTP
  response closure, and check partial-download cleanup and failed job state.
- OCR materialization also used to overwrite an earlier result when a later
  result reused its filename. New artifacts receive separate namespaces; tests
  verify prior hashes remain intact and identical results still reuse a variant.
- **K, partial:** architecture and storage docs now describe the actual module
  layout and implemented backends. Sprint status and ScanTailor docs distinguish
  projective correction/manual zones from the deferred algorithms, and remove
  stale claims that reverse conversion and browser acceptance fixtures are absent.
- Dependency audits identified vulnerable locked releases. Targeted updates now
  use [pypdf 6.16.1](https://github.com/py-pdf/pypdf/releases/tag/6.16.1),
  [sqlparse 0.6.0](https://sqlparse.readthedocs.io/en/latest/changes.html#release-0-6-0-aug-13-2026),
  and [Vitest 4.1.11](https://github.com/vitest-dev/vitest/security/advisories/GHSA-82fw-gwwq-j7x9).
  `pip-audit --skip-editable` and `npm audit` report no known vulnerabilities for
  these lockfiles as of this run.

Validation on 2026-09-10: 186 local Python tests, Ruff lint/format checks, strict
mypy, Django system/migration checks, TypeScript checks, all four frontend tests,
and the production frontend build passed. The rebuilt browser bundle is unchanged.
The infrastructure suite passed all eight tests and all three Chromium workflows
(digital library, concurrent editors, and the physical-book pipeline) with real
PostgreSQL, LDAP, Redis/RQ, Nginx, Uvicorn, Pandoc, and Calibre. The OCR service in
that suite is a contract fixture. Eight infrastructure tests are skipped by the
local-only run and exercised separately by the infrastructure runner.

The next correction work stays within Sprint019:

| Tasks | Remaining work |
| --- | --- |
| B–D | Provider-owned normalization and ISBN lookup, reference-less candidate safety, scoring and threshold tuning, and an effective review-threshold contract |
| E | Service-reported OCR provenance, defensible polling defaults, and worker occupancy policy |
| G | Mandatory, transactionally checked stage revisions; existing stale-editor coverage does not prove the concurrent-write contract |
| H | Converter subprocess timeouts that terminate the process group, and application resource limits |
| I | Administrator source preview/run/retry pages and supported unattended discovery |
| J | Accurate advertised algorithm names and quantitative curvature fixtures |
| K | Further reconciliation as those corrections land |

Curvature correction, automatic zones, taxonomy/search, derived-artifact retention,
and device/abuse controls remain Sprint020 work. Neither correction sprint nor the
full project is acceptance-complete.

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
- Playwright Chromium now drives two independent members authenticated against
  seeded OpenLDAP through the Nginx/Uvicorn/PostgreSQL stack. It creates a scan
  job, uploads a synthetic page, verifies the compiled editor bundle is served,
  and proves a stale stage edit receives HTTP 409, visibly reloads the winning
  revision, and cannot silently overwrite it. This path also caught and closed a
  PostgreSQL-only missing-transaction defect in stage-state creation.
- The infrastructure suite now installs a SHA-512-verified official Calibre
  9.11.0 release and proves member-requested EPUB-to-PDF and EPUB-to-MOBI routes
  across Redis and a separately bootstrapped RQ worker. It validates output
  signatures and hashes, checks source/tool/version/job provenance, and downloads
  the MOBI through the authenticated library endpoint.
- A second Chromium scenario now completes the synthetic physical-book
  acceptance chain through the production-shaped stack: all six ScanTailor
  stages, explicit rescan and downstream invalidation, replacement processing,
  provider-backed human review, Redis/RQ finalization, HTTP multipart OCR,
  cleaned/TXT/searchable-PDF variants, canonical search, and authenticated
  download. OCR service URL/timeout/poll controls are now genuine environment
  settings rather than unreachable worker defaults.
- Representative four-chapter Calibre fixtures now verify both reverse routes,
  PDF-to-EPUB and MOBI-to-EPUB, through HTTP, Redis, and separately bootstrapped
  workers. The resulting EPUBs pass signature/hash checks, retain source/tool/job
  lineage, preserve canonical title metadata, and contain the expected readable
  prose when inspected with Beautiful Soup.
- The real Pandoc worker fixture now covers both TXT and structured HTML inputs.
  HTML headings, emphasized prose, ordered-list evidence, EPUB metadata,
  signature, hash, provenance, and authenticated download are verified across
  HTTP and Redis/RQ rather than only by unit tests.

## Remaining gaps by requirement area

- Async purity: development intentionally supports synchronous execution without
  Redis; deployed production is prevented from using that configuration.
- Observability acceptance: the repository provides metrics, alerts, dashboard,
  retention policy, and runbooks; importing them, verifying a real production
  scrape, and testing the site's Alertmanager notification path remain deployment
  work.
- Integration/E2E depth: recognition-quality validation against the hackerspace
  AI server still needs a site-connected acceptance environment. PostgreSQL,
  LDAP, Redis/RQ, Pandoc TXT/HTML-to-EPUB, Calibre EPUB-to-PDF/MOBI and reverse
  PDF/MOBI-to-EPUB quality, authenticated
  converted-file download, Nginx/Uvicorn SSE, concurrent browser editors, the
  browser-driven synthetic physical-book/OCR contract flow, and live dependency
  readiness now have infrastructure-backed CI coverage.

These items keep the full-project goal active; `docs/sprint_progress.md` is a
historical implementation log rather than an acceptance sign-off.
