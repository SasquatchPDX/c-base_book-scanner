# Sprint020 — Deferred Algorithms, Library Completeness, and Operational Durability

## Goal

Finish the two ScanTailor stages Sprint019 deliberately deferred, close the
library-completeness gaps that make parts of the Version 1 UI inert, and add the
operational and security work the system needs before it runs unattended in the
hackerspace for long periods.

Sprint019 was a correction sprint: it fixed defects against existing
specifications. This sprint is development: it implements the algorithm work that
Sprint016 and Sprint017 did not complete, populates data the search and browse
surfaces already expose but nothing ever writes, and adds the durability controls
that only become visible after months of real use.

## Context

Sprint019 corrected twenty-six findings and deferred five items with an explicit
rationale. Four of those are carried here (tasks A, B, J, K). The remaining
tasks come from questions the Sprint019 audit raised but which are new
development rather than conformance corrections, so filing them there would have
broadened that sprint's scope.

Findings are indexed at the end of this document. Numbering continues from
Sprint019 so that finding identifiers stay unique across the project.

Two constraints discovered while planning this sprint shape several tasks:

- The pinned OpenCV build is `opencv-python-headless` 4.14. It provides the main
  modules only: `cv2.createThinPlateSplineShapeTransformer` and `cv2.ximgproc`
  are **not** available. Any geometry or segmentation work must use main-module
  primitives (`remap`, `warpPerspective`, morphology,
  `connectedComponentsWithStats`) or supply its own implementation.
- SciPy 1.18 is present in the environment, but only as a transitive dependency of
  scikit-image. Any task that imports it must promote it to a direct dependency in
  `pyproject.toml` rather than relying on that accident.

## Deliverables

- A curvature-correcting dewarping stage that measurably improves the Sprint019
  fixtures, with manual correction and a preserved bypass.
- Automatic text/picture zone segmentation with per-zone algorithm dispatch and
  reviewable, overridable zone parameters.
- Populated categories and genres, so the existing browse filters return results.
- A search implementation with ranking and an index behind it.
- Derived-artifact garbage collection with a documented retention policy.
- Review tasks for non-metadata failures, using the generic model that already
  exists.
- Scanner device identity, and abuse controls on login and upload.
- A module boundary decision, executed.
- An OCR recognition-quality acceptance lane against the hackerspace AI server.

## Tasks

### A — Curvature dewarping (carried from Sprint016)

**Current.** `_run_perspective_dewarp` (`scantailor.py:990`) detects a
quadrilateral and applies `cv2.getPerspectiveTransform` plus `warpPerspective`.
Sprint019 renamed the algorithm honestly and added mild- and strong-curvature
fixtures that the current stage cannot correct.

**Defect.** A projective transform is a plane-to-plane mapping. It removes camera
tilt from a flat page and cannot remove the cylindrical curl of a bound book near
the spine, which is the problem Sprint016 exists to solve. Sprint016 named this
"one of the most technically difficult parts of the project" and asked for it to
be kept isolated, measurable, and testable.

**Change.**

- Implement a non-planar surface model behind the existing modular algorithm
  interface. A text-baseline approach is the best-evidenced fit for photographed
  pages: detect text lines, fit smooth curves to their baselines, estimate the
  page surface from the curvature of those baselines, and resample with
  `cv2.remap`. Detection can use morphology and `connectedComponentsWithStats`
  from the main OpenCV modules; curve fitting and optimization may use SciPy,
  which must then become a direct dependency.
- Register it as a new algorithm name and version alongside the existing
  projective one. Do not replace the projective transform: it is the correct
  choice for flat originals and remains a valid selection.
- Support manual correction. The workspace already ships draggable dewarp control
  points; extend the geometry to control curves or a control grid, and keep the
  existing rule that only editable inputs travel from browser to server while
  worker-derived geometry is never echoed back as user parameters.
- Preserve the bypass path unchanged. Sprint016 requires a safe bypass for pages
  where dewarping is unnecessary or fails, and Sprint019's tests already assert
  it.
- Estimate on a reduced-resolution image and apply the resulting surface at full
  resolution in the worker, consistent with every other stage.
- Persist the estimated geometry so a result is reproducible from stored
  parameters alone, and record algorithm name and version on the stage state.
- Fail soft: an estimation that does not converge falls back to the projective
  path or to copy, records why, and never leaves the stage in a state that blocks
  finalization.

**Measurement.** Sprint016 asks for "quantitative or image-difference regression
tests where feasible". Adopt one metric and hold it: baseline straightness
residual on the fixture set is the most direct — measure the deviation of fitted
text baselines from horizontal before and after. Record the metric per fixture in
CI so a regression is visible as a number, not a subjective image comparison.

**Done when.** Both curvature fixtures show a measurable, asserted improvement in
the chosen metric; manual correction produces reproducible results from persisted
parameters; the bypass and failure paths behave exactly as they do today.

### B — Automatic text/picture segmentation (carried from Sprint017)

**Current.** `_run_output` (`scantailor.py:1273`) dispatches per-zone
binarization when `zone_mode="manual"` and a text or picture box is supplied.
`zone_mode="auto"` performs no separation and binarizes the whole page with one
algorithm.

**Defect.** Sprint017 requires "text/picture zone separation" alongside manual
zone controls. Only the manual half exists, so an operator must hand-draw zones on
every mixed-content page.

**Change.**

- Implement automatic zone detection using main-module OpenCV: run-length
  smearing or morphological closing to merge glyphs into text blocks, then
  `connectedComponentsWithStats` with size, density, and aspect heuristics to
  separate text regions from continuous-tone regions.
- Emit detected zones into the stage parameters so they are visible in the
  workspace, reviewable, and directly overridable. A detected zone the operator
  edits becomes a manual zone and must be preserved by batch application, which
  already skips manual exceptions.
- Dispatch per-zone algorithms through the existing `_binarize_with_cv2` path.
  Default to an adaptive algorithm for text and a grayscale or halftone-preserving
  path for pictures rather than binarizing photographs into noise.
- Support more than one text and one picture region per page. The current
  parameter shape holds a single `text_box` and `picture_box`; a real page has
  several. Migrate the parameter schema to a list, keeping the single-box form
  readable for existing stage states.
- Record which zones were detected automatically and which were edited, so
  provenance distinguishes machine output from operator intent.

**Done when.** A mixed text-and-photograph fixture produces sensible zones with no
manual input; an operator override survives a batch apply; and switching output
algorithms still invalidates only the appropriate downstream artifacts.

### C — Categories and genres are never populated

**Current.** `Book.categories` and `Book.genres` exist with `Category` and `Genre`
models, admin management, API serialization, and library filters
(`views.py:1169`, `views.py:1172`). Nothing writes them.
`_provision_book_from_metadata` (`intake.py:986`) sets title, authors,
identifiers, publisher, language, description, publication date, and edition —
never categories or genres. `_provision_book_from_manual_metadata`
(`intake.py:883`) and `_build_manual_book_from_payload` (`views.py:914`) do not
accept them either. Only hand-curation through Django admin can fill them.

**Defect.** AGENTS.md requires members to search and browse by category and genre,
and Sprint008 lists both as browse/filter dimensions and makes them part of the
Version 1 milestone. The filters are shipped and permanently return nothing, which
is worse than not offering them.

**Change.**

- Map provider subject data onto the taxonomy. Open Library returns `subject`,
  and any second provider added in Sprint019 will carry its own equivalent; the
  mapping belongs in the provider normalization layer that Sprint019 introduced,
  not in shared code.
- Introduce a controlled vocabulary rather than importing raw subject strings.
  Open Library subjects are long-tailed and noisy; a few hundred uncurated
  categories per book makes browse useless. Define a bounded mapping, keep the
  unmapped raw subjects as evidence on the candidate, and record which mapping
  version produced an assignment.
- Let reviewers set categories and genres during manual entry and candidate
  acceptance, and record the change with the existing decision provenance.
- Add a backfill management command for books already in the library, with a
  dry-run mode consistent with `verify_storage` and `ingest_sources`.
- Only assign a taxonomy from a candidate the system actually accepted. A rejected
  or low-confidence candidate must not silently write canonical taxonomy, for the
  same reason it must not write canonical title metadata.

**Done when.** An imported book with provider subject data appears under a
category and a genre, the browse filters return it, and a reviewer can correct the
assignment.

### D — Search that scales

**Current.** `library_books` (`views.py:1136`) filters with `icontains` across
title, subtitle, description, and author name, joined and de-duplicated, ordered
by created date or title.

**Defect.** There is no index behind any of it and no relevance ranking: results
are ordered by recency regardless of match quality, a substring query scans, and
the description `icontains` will degrade first. Sprint008 defines the first usable
release baseline and AGENTS.md requires searching canonical Book metadata; it does
not require this to be fast, but a shared library that grows for a year will make
this the most visible weakness in the product.

**Change.**

- Implement PostgreSQL full-text search with a stored search vector, a GIN index,
  and relevance ranking, plus trigram similarity for fuzzy title and author
  matching. Both require `django.contrib.postgres`; trigram additionally requires
  the `pg_trgm` extension, which must be created by a migration operation and
  documented as a database privilege requirement in `docs/operations.md`.
- Keep the development SQLite path working. The engine differs, so gate the
  Postgres path on the configured backend and fall back to the current behaviour
  with a documented, tested fallback rather than crashing dev environments.
- Keep result ordering explicit and stable, and keep pagination bounds as they are.
- Do not index or search book *contents*. AGENTS.md is explicit that document
  content is not logged, and Sprint008 scopes search to canonical metadata.

**Done when.** Search is index-backed on PostgreSQL, results are ranked by
relevance, a misspelled title still finds its book, and the SQLite development
path still works and is covered by tests.

### E — Derived-artifact garbage collection and retention

**Current.** Every stage run writes its output and preview under a namespace keyed
by the stage state's current revision (`scantailor.py:139`). When a stage is
re-run, the revision increments and the state row points at the new artifacts. The
previous revision's files are immediately unreferenced and are never deleted.
Nothing in the repository deletes derived artifacts; the only retention policy
anywhere is Prometheus TSDB retention, which concerns metrics, not storage.
`verify_storage` walks database references outward and therefore cannot see an
orphan at all.

**Defect.** A page edited ten times leaves nine orphaned output/preview pairs on
network storage with no database reference. For a scanned book this multiplies by
page count and by stage. AGENTS.md excludes *backup* management from scope, not
artifact lifecycle, and Sprint018's final hardening asks to "verify storage
consistency tooling" — the tooling currently verifies one direction only.

**Change.**

- Add reverse consistency checking: walk storage and report artifacts no database
  row references, as a read-only report first.
- Add a garbage-collection command with an explicit dry run, an age threshold, and
  a refusal to touch anything referenced by a `FileVariant`, `CoverImage`,
  `ScanPageVersion`, or a current stage state. Raw scan page versions are
  immutable history and must never be collected; only superseded derived stage
  outputs and previews are eligible.
- Define and document a retention policy for superseded stage revisions in
  `docs/operations.md`, alongside the existing storage guidance.
- Never delete during a running job: check for active stage, finalization, and
  deletion work before collecting, and reuse the quarantine-then-purge pattern
  already proven in `book_deletion.py` rather than unlinking directly.

**Done when.** An operator can see orphaned artifacts, collect them safely with a
dry run first, and no referenced or immutable artifact can be removed.

### F — Review tasks for non-metadata failures

**Current.** `ReviewTask.TaskType` defines `NEEDS_ATTENTION`, `SOURCE_REVIEW`, and
`QUALITY_REVIEW` (`models.py:861`). Only `NEEDS_ATTENTION` is ever created. A
failed conversion, a failed OCR job, a failed finalization, or a storage
consistency mismatch is recorded in job state and surfaced through metrics and
alerts, but generates no human task.

**Defect.** Sprint006 explicitly requires the `ReviewTask` model to be "designed
generically enough for future OCR, conversion, duplicate-conflict, and scan
issues", and AGENTS.md makes human review a first-class workflow for "low-confidence
metadata matches, unresolved imports, scan issues, and similar cases". The
generic design was delivered; nothing uses it, so failures rely on somebody
watching Grafana.

**Change.**

- Create review tasks for terminal conversion failures, terminal OCR failures,
  finalization failures that exhausted retries, and storage-consistency mismatches,
  using the existing task types.
- Give those tasks resolution actions appropriate to their kind — retry, cancel,
  acknowledge — reusing the existing decision and provenance fields rather than
  adding a parallel mechanism.
- Apply the existing permission rule: admins resolve everything, owners resolve
  their own.
- Add filtering by task type and state to the review queue, and pagination. The
  queue currently serializes every open task with all candidates on every request,
  which is fine for a handful and not for a backlog.
- Keep the metrics and alerts. Review tasks complement them; they do not replace
  the queue-depth and failure-rate signals Sprint018 hardened.

**Done when.** A failed conversion produces a resolvable task, the queue can be
filtered by type, and the existing metadata-review behaviour is unchanged.

### G — Back covers

**Current.** `CoverImage.CoverPosition.BACK` exists in the model and is serialized
as `cover_back` in the API and the detail template. No code path creates one:
`covers.extract_front_cover` (`covers.py:196`) is the only writer, and Django
admin is the only way a back cover can exist.

**Defect.** Sprint003's acceptance criterion has an admin attaching front *and*
back covers, which admin satisfies, but the library has no application workflow
for the back cover it advertises on every book page. For scanned books the back
cover is a natural capture artifact that the pipeline already handles as an image.

**Change.**

- Allow an authorized member to upload a cover image for either position through
  the book page, with the same bounded image parsing, normalization, size limits,
  and provenance recording that extraction already applies.
- Allow a scan operator to designate captured pages as front and back cover
  sources during finalization, recording the source page version as lineage.
- Keep the existing one-cover-per-position constraint and replacement semantics
  explicit: replacing a cover must record what it replaced.

**Done when.** Both cover positions can be populated without database or
filesystem access, and every cover records where it came from.

### H — Scanner device identity

**Current.** Every scanner endpoint is `@login_required` with an ordinary session.
`ScanJob.scanner_id` is a free-text field the client asserts about itself.

**Defect.** A physical scanner must therefore hold a member's LDAP session
indefinitely, which means a shared or long-lived credential on a device in a
hackerspace. Uploads are attributed to whichever member's session the device
carries, so `ScanPageVersion.uploader` records the credential owner rather than
the device, and there is no way to revoke one scanner without disturbing anyone
else. AGENTS.md requires that LDAP remain the authentication authority and that
filesystem permissions never derive from LDAP identity; a scoped device credential
is consistent with both.

**Change.**

- Introduce a scanner device registration with a scoped credential that can only
  reach the scanner endpoints — job listing, job selection, page upload, todo
  retrieval — and nothing else in the application.
- Attribute uploads to the device *and* to the operator who claimed the job, so
  provenance keeps both.
- Support revocation and rotation without touching LDAP, in the same spirit as the
  existing local ban state.
- Keep the protocol hardware-neutral as Sprint010 requires, and update
  `docs/scanner-protocol.md` to version the contract change.

**Done when.** A scanner authenticates as a device, cannot reach library or admin
routes, and can be revoked individually.

### I — Login and upload abuse controls

**Current.** `library_login` (`views.py:82`) calls `authenticate` on every POST
with no rate limiting, backoff, or lockout anywhere in the codebase. Uploads are
bounded per file by `max_upload_bytes` but not per user or per interval.

**Defect.** Each login attempt is an LDAP bind, so an unthrottled login form turns
the e-book server into a brute-force amplifier against the hackerspace's
authentication authority, and repeated failures may trip lockouts on accounts
elsewhere. AGENTS.md requires defending against "abusive upload sizes" and
"runaway worker jobs"; per-file limits address neither a flood of small uploads
nor a queue-filling loop.

**Change.**

- Add per-account and per-source-address throttling with progressive backoff on
  failed authentication, keeping successful logins unaffected. Local state only —
  never modify LDAP.
- Log throttling events as structured events with bounded metadata, and expose a
  counter so the existing alerting can see a credential-stuffing attempt.
- Add per-user upload rate and volume limits, and a bound on concurrently queued
  import jobs per user, so one member cannot saturate the worker pool.
- Ensure throttling responses do not disclose whether an account exists.

**Done when.** Repeated failed logins are slowed and observable, and a single
member cannot exhaust the queue with uploads.

### J — Module boundaries (carried from Sprint019)

**Current.** All application code lives in `ebook_server.core`, with `views.py` at
roughly 3,600 lines covering library, scan, ScanTailor, review, conversion, OCR,
and health concerns. Sprint019 required `docs/architecture.md` to stop describing
a layout that does not exist; whichever way that was resolved, the decision now
needs executing.

**Change.**

- If the decision was to keep one module, this task is closed by Sprint019's
  documentation change and only the view-layer split below applies.
- Split `views.py` by domain regardless of the app-label decision. It is the
  single largest maintenance risk in the repository and can be divided without any
  database implication.
- If application modules are split into separate Django apps, note that changing a
  model's app label rewrites migration state and table names. Use
  `SeparateDatabaseAndState` or keep every model in `core` and split only views
  and services. Do not let a structural tidy-up trigger a data migration.
- Keep URL names, route paths, and JSON payload shapes byte-identical. This is a
  refactor; no external contract may change.

**Done when.** No module exceeds a size the team is willing to review, every test
passes unchanged, and the architecture document matches the tree.

### K — OCR recognition-quality acceptance (carried from Sprint012 and Sprint018)

**Current.** The infrastructure suite drives a real HTTP OCR contract service and
proves submission, polling, artifact retrieval, variant creation, and provenance.
`docs/requirements-audit.md` records that recognition quality against the
hackerspace AI server still needs a site-connected acceptance environment.

**Defect.** The contract is proven; the *output* is not. Nothing asserts that OCR
of a known page yields text resembling that page, so a misconfigured engine,
wrong language, or degraded model would pass every existing test.

**Change.**

- Build a small ground-truth fixture set: page images with known transcriptions,
  covering clean print, a photographed curved page, and at least one non-English
  page if the space's collection warrants it.
- Assert character and word error rates against thresholds agreed with whoever
  operates the AI server, and record the measured rates as build output so drift
  is visible over time.
- Run this as a separate, clearly labelled acceptance lane that requires the
  site-connected environment. It must not become a required gate on a
  contributor's pull request, and its absence must not be reported as success.
- Feed the same fixtures through the full pipeline — cleaned pages, OCR, derived
  text, conversion — so the measurement covers what members actually download.

**Done when.** A site-connected run reports recognition quality as numbers against
agreed thresholds, and a degraded engine fails that lane.

### L — Conversion depth and route quality

**Current.** Eight capabilities are advertised across Calibre and Pandoc.
`docs/conversion-matrix.md` marks FB2, DjVu, and CBZ/CBR conversions as researched
and not advertised, and route selection is breadth-first over available edges.

**Recommendation.** This is optional work, ordered last, and should only be taken
if the collection needs it.

- Enable FB2 through Calibre if the collection contains FB2 files worth
  converting, with the same binary-verified acceptance evidence the existing
  routes have. Intake already accepts the format, so files are already arriving.
- DjVu and CBR conversion both require external tools in a restricted worker
  image; treat each as a deliberate operational decision, not a default.
- The route planner picks the shortest hop count and ignores the quality and
  lossiness columns the matrix already documents. If multi-hop routes multiply,
  weight edges by documented quality so a lossy two-hop path is not silently
  preferred over a better one.
- Whatever is not enabled must stay honestly marked as not advertised. The current
  matrix is trustworthy and must remain so.

## Sequencing

1. Tasks C and E first. Both are small relative to their value: C makes shipped UI
   work, E stops unbounded storage growth that gets more expensive to clean the
   longer it runs.
2. Task I next. It is small, it protects an external system the project does not
   own, and it should not wait behind algorithm work.
3. Tasks A and B in parallel with the above; they are the largest and most
   uncertain items and benefit from the whole sprint.
4. Task D after C, so ranking and indexing are designed against a schema that
   actually carries taxonomy.
5. Tasks F, G, H as capacity allows.
6. Task J late, when the code it reorganizes has stopped moving.
7. Task K whenever the site-connected environment is available; it is gated on
   access, not on the other work.
8. Task L last, or not at all.

## Tests

Algorithms:

- Curvature fixtures show an asserted improvement in the baseline-straightness
  metric after dewarping, for both mild and strong curvature.
- Dewarping results are reproducible from persisted parameters alone.
- Non-convergent estimation falls back deterministically and records why.
- The bypass path still leaves the input unchanged.
- Automatic segmentation finds text and picture zones on a mixed-content fixture.
- An operator-edited zone survives a batch apply that would otherwise overwrite it.
- Multiple text and picture zones per page round-trip through the parameter schema.

Library completeness:

- An import with provider subject data receives mapped categories and genres.
- A low-confidence candidate assigns no taxonomy.
- Browse filters by category and genre return the imported book.
- A reviewer can correct a taxonomy assignment and the change is attributed.
- Search ranks an exact title match above an incidental description match.
- A misspelled title still finds its book through trigram similarity.
- The SQLite development search path still works.
- Both cover positions can be populated through the application, with provenance.

Operations and security:

- Orphan detection reports an unreferenced stage artifact and ignores referenced
  and immutable ones.
- Garbage collection in dry-run mode deletes nothing.
- Collection refuses to run against an active job.
- A terminal conversion failure creates a resolvable review task with correct
  permissions.
- The review queue filters by task type and paginates.
- A scanner device credential can upload pages and cannot reach library or admin
  routes; revoking it stops uploads immediately.
- Repeated failed logins are throttled without disclosing account existence, and a
  successful login is unaffected.
- A user cannot exceed the configured concurrent import-job limit.

Regression protection: everything Sprint019 left green stays green, including
MOVE/COPY safety, duplicate reuse, path traversal, archive bombs, conflict
detection, finalization safeguards, provenance, and storage consistency.

## Security

- Automatic zone detection and dewarping run on untrusted images inside the
  worker. Keep the existing bounded-parse discipline: dimension limits, decompression-bomb
  handling, and deterministic failure rather than unbounded allocation.
- Garbage collection is the most dangerous new capability in this sprint. It must
  be read-only by default, must quarantine before deleting, must never consider
  raw scan page versions, and must be covered by tests that assert refusal cases
  before it is documented as safe.
- Scanner device credentials must be scoped, revocable, stored hashed, and never
  reachable from a browser session. A compromised scanner must not become a
  library-wide compromise.
- Throttling state must not become an oracle for account existence, and must not
  let one attacker lock a legitimate member out of the application.
- Taxonomy imported from providers is untrusted third-party text: bound its length,
  map it through a controlled vocabulary, and never render it unescaped.
- Any new upload path for covers inherits the full intake threat model — signature
  validation, size limits, safe filenames, no shell interpolation.

## Acceptance Criteria

- Both curvature fixtures are measurably corrected, with the metric asserted in
  CI, and manual correction plus bypass behave exactly as documented.
- A mixed-content page is segmented automatically, and operator zone edits survive
  batch application.
- Category and genre browse returns real results for imported books.
- Search is index-backed and ranked on PostgreSQL, with a working development
  fallback.
- An operator can find and safely collect orphaned derived artifacts, and the
  retention policy is documented.
- Terminal conversion, OCR, finalization, and consistency failures produce
  resolvable review tasks.
- Both cover positions are populatable through the application with recorded
  provenance.
- A scanner authenticates as a revocable device, scoped to scanner routes.
- Login and upload abuse controls are active, observable, and non-disclosing.
- `docs/architecture.md`, `docs/scanner-protocol.md`, `docs/operations.md`,
  `docs/scantailor-web.md`, `docs/conversion-matrix.md`, and
  `docs/requirements-audit.md` all describe the system as it then exists.
- The full gate passes: `uv run pytest -q`, `uv run mypy src tests`,
  `uv run ruff check .`, `uv run ruff format --check .`, `npm run check`,
  `npm test`, `npm run build`, and the infrastructure suite.

## Out of Scope

- Full-text search of book *contents*. AGENTS.md scopes search to canonical
  metadata and forbids logging document content; indexing extracted text is a
  separate decision with its own storage, privacy, and licensing questions.
- Recommendations, reading progress, lending, or any member-facing feature beyond
  the Version 1 and full-project definitions in AGENTS.md.
- Replacing RQ, PostgreSQL, or Django. Sprint001 justified those choices and
  nothing found since contradicts them.
- Distributing ScanTailor processing across machines. A single worker pool remains
  adequate; revisit only with measurements.
- Backup management, which AGENTS.md excludes explicitly. Garbage collection in
  task E is artifact lifecycle, not backup, and the two must not be conflated in
  documentation.
- Any conversion route enabled without binary-verified acceptance evidence.

## Findings and recommendations index

Numbering continues from Sprint019.

| # | Finding or recommendation | Location | Source |
| --- | --- | --- | --- |
| 27 | Dewarping cannot correct page curvature; projective only | `scantailor.py:990` | Sprint016 |
| 28 | No automatic text/picture segmentation; `zone_mode="auto"` does nothing | `scantailor.py:1289` | Sprint017 |
| 29 | Output stage supports only one text and one picture zone | `scantailor.py:1121` | Sprint017 |
| 30 | Nothing ever writes `Book.categories` or `Book.genres` | `intake.py:986`, `views.py:914` | AGENTS.md, Sprint008 |
| 31 | Category and genre browse filters are permanently empty | `views.py:1169` | Sprint008 |
| 32 | Search is unindexed `icontains` with no relevance ranking | `views.py:1136` | Sprint008 |
| 33 | Superseded stage artifacts are orphaned on disk and never collected | `scantailor.py:139` | Sprint018 |
| 34 | Consistency checking is one-directional; orphans are invisible | `management/commands/verify_storage.py` | Sprint018 |
| 35 | No artifact retention policy; only Prometheus TSDB retention exists | `docs/operations.md` | Sprint018 |
| 36 | `SOURCE_REVIEW` and `QUALITY_REVIEW` task types are never created | `models.py:861` | Sprint006 |
| 37 | Conversion, OCR, and finalization failures create no human task | `conversion.py`, `ocr.py`, `scan_pipeline.py` | AGENTS.md, Sprint006 |
| 38 | Review queue has no type filter and no pagination | `views.py:3096` | Sprint006 |
| 39 | No code path can create a back cover | `covers.py:196` | Sprint003 |
| 40 | Scanner authenticates as a member session; `scanner_id` is self-asserted | `views.py:2559`, `models.py:368` | Sprint010 |
| 41 | No throttling on the LDAP-backed login form | `views.py:82` | AGENTS.md |
| 42 | No per-user upload rate or queued-job limits | `views.py:3008` | AGENTS.md |
| 43 | `views.py` spans every domain at roughly 3,600 lines | `views.py` | Sprint001 |
| 44 | OCR recognition quality is unmeasured | `tests/integration/test_infrastructure.py` | Sprint012, Sprint018 |
| 45 | Route planner ignores documented quality and lossiness | `conversion.py:186` | Sprint001, Sprint009 |
| 46 | OpenCV headless lacks TPS and ximgproc; SciPy is only transitive | `pyproject.toml` | engineering constraint |

## Notes on scope

Three things are deliberately left undone and should stay that way unless the
hackerspace asks for them.

The projective dewarping path is not being removed. It is the correct transform
for a flat original photographed at an angle, and Sprint016's own framing — keep
the hard geometry isolated — is better served by two clearly named algorithms than
by one that tries to be both.

The taxonomy work stops at a controlled vocabulary. Automatic subject
classification from book text is a different project with different failure modes,
and AGENTS.md's instruction to surface uncertainty rather than guess applies with
particular force to metadata that shapes how members find things.

Nothing here adds a second queue, a second datastore, or a service boundary. The
modular monolith is still the right shape at this size, and AGENTS.md says so
plainly.
