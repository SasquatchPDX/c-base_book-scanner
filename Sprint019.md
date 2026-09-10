# Sprint019 — Specification Conformance and Correction

## Goal

Close the gaps found by a full re-read of `AGENTS.md` and Sprint001–Sprint018
against the implementation. This sprint corrects defects in identification
scoring, the metadata-provider abstraction, candidate persistence, OCR
provenance, intake state reporting, concurrency contracts, and converter resource
limits; completes the unattended-ingestion promise of Sprint007; and reconciles
documentation that describes behaviour the code does not have.

This is a correction sprint. It adds no new product features. Where an earlier
sprint's goal cannot be reached without substantial new algorithm work, this
sprint records an honest status, adds the fixtures that future work needs, and
defers the algorithm to Sprint020 rather than leaving a silent shortfall.

## Context

The audit that produced this sprint compared every requirement in `AGENTS.md` and
Sprint001–Sprint018 against the source. Twenty-four findings are indexed at the
end of this document with source locations and the requirement each one violates.

The architectural invariants the project cares most about hold: Book/FileVariant
separation, hash-based duplicate detection, preserved originals with recorded
lineage, storage behind an abstraction with traversal guards, durable job state
for slow work, review tasks instead of silent guesses, and LDAP-only identity with
local roles and bans. The defects are concentrated in metadata identification
quality, the two hardest ScanTailor stages, and the parts of Sprint007 that were
delivered as a command-line tool rather than an application workflow.

`docs/requirements-audit.md` did not overstate anything, but it is now incomplete:
it must record what this sprint corrects and what it defers.

## Deliverables

- A green tree: tests, type checking, linting, formatting, and frontend checks.
- A provider-agnostic identification layer with at least one conformance-tested
  non-Open-Library provider shape.
- A tuned, evidence-backed scoring and acceptance rule with recorded reasoning.
- OCR variants carrying service-reported provenance.
- An observable intake state machine matching the documented progression.
- A stage-mutation contract that cannot silently overwrite a concurrent edit.
- Administrator source-import workflows on the web surface plus a documented
  unattended discovery schedule.
- Corrected `docs/architecture.md`, `docs/scantailor-web.md`,
  `docs/metadata-providers.md`, `docs/operations.md`, `docs/sprint_progress.md`,
  and `docs/requirements-audit.md`.
- Curvature fixtures for the deferred dewarping work.

## Tasks

### A — Restore a green tree (blocking)

**Current.** The working tree changes `intake.py:1169` so only candidates at or
above `AUTO_ACCEPT_SCORE` bypass review. Two source-ingestion tests still assert
the previous behaviour and fail.

**Defect.** Before the change, any candidate at or above `REVIEW_SCORE` (0.55) was
auto-accepted and silently written into canonical `Book` metadata. That
contradicts AGENTS.md ("Weak matches must not be silently accepted") and Sprint005
("Auto-accept only high-confidence matches"). The change is correct; the tests
encode the defect.

**Change.**

- Update `tests/test_source_ingestion.py::test_copy_mode_source_import_keeps_source_file`
  and `::test_move_mode_source_import_removes_source_after_verified_archive` so
  they assert the COPY and MOVE behaviour they exist to verify under a
  `LOW_CONFIDENCE` outcome, or give their stub provider a matching ISBN so the
  candidate legitimately auto-accepts. COPY must still retain the source and MOVE
  must still delete it only after verified archival — that is what these tests
  protect and it must not be weakened.
- Do not lower the threshold to make the tests pass.
- Land the threshold correction and the test updates as one change, before any
  other work in this sprint.

**Done when.** `uv run pytest -q` is green with the corrected threshold in place.

### B — Identification scoring correctness

**Current.** `_apply_scoring` (`identification.py:472`) sums: 0.95 for an ISBN
match, 0.55 × best title similarity, 0.30 × author score, and three 0.05 bonuses
for language, publisher, and description.

**Defects.**

1. `_author_match_score` (`identification.py:462`) divides the intersection by the
   number of *hints*. `parse_filename_signals` (`identification.py:164`)
   deliberately pushes both sides of an `Author - Title` split into
   `author_hints`, so a correctly named `Jane Doe - Some Book.epub` yields hints
   `["Jane Doe", "Some Book"]`, an intersection of one, a score of 0.5, and a
   contribution of 0.15 instead of 0.30. Supplying more evidence lowers the score.
2. The same function compares author names by exact normalized equality. EPUB OPF
   metadata routinely stores `Doe, Jane`, which never matches `Jane Doe`. Sprint005
   asks for author *similarity*, not equality.
3. Language comparison is exact string equality between an EPUB `dc:language`
   (`en`, `en-GB`) and a provider language code (Open Library returns MARC codes
   such as `eng`), so the 0.05 language bonus is effectively unreachable.
4. Publisher comparison is exact string equality, so `Crown` and
   `Crown Publishing` score nothing.
5. `IdentificationCandidate.description` is populated from the provider payload's
   `subtitle` field (`identification.py:446`). The description bonus therefore
   compares a description against a subtitle and never fires, and the value stored
   in `MetadataCandidate.description` and shown to reviewers is a subtitle
   mislabelled as a description.
6. Consequently a perfect non-ISBN title and author match totals 0.85 against a
   0.86 auto-accept threshold: no ISBN-less import can auto-accept, so every one
   of them lands in the review queue. Sprint005's own test list requires "Good
   title/author match without embedded ISBN" to be a working identification path.

**Change.**

- Score authors by similarity, not set equality. For each candidate author take
  the best `token_set_ratio` against any hint; let `author_score` be the mean of
  those values. Hints that match nothing are guesses and must not act as a
  penalty divisor.
- Rank hint provenance. Embedded metadata is stronger evidence than a filename
  split; filename-derived hints should contribute but must not dilute a correct
  embedded value.
- Normalize language to a single representation (ISO 639-1) on both sides before
  comparing, so the language signal can actually fire.
- Compare publisher by similarity with a high floor rather than equality.
- Map provider `subtitle` to a `subtitle` field and stop labelling it
  `description`. Populate `description` only from a real description field, and
  correct what the review UI shows.
- Replace the pure sum with an explicit acceptance rule. Auto-accept requires
  either a validated ISBN match, or a total at or above the tuned threshold **and**
  a title similarity at or above a high floor **and** a non-zero author score. A
  strong title with an unknown author must never auto-accept.
- Persist the per-signal score breakdown alongside the candidate so tuning and
  review are evidence-based rather than a single opaque number. This needs a
  migration; add a `score_breakdown` JSON field to `MetadataCandidate` or record
  the breakdown in a reserved key of `raw_payload` and document the choice.
- Re-tune `identification_auto_accept` against fixtures once the scorer is
  correct, and record the tuning evidence in `docs/metadata-providers.md`: which
  fixtures were scored, what each signal contributed, and why the chosen values
  separate accept from review.

**Done when.** A correctly named file with correct embedded metadata and no ISBN
auto-accepts; a title-only match with no author corroboration does not; and the
threshold choice is justified in writing.

### C — Provider abstraction decoupling

**Current.** The `MetadataProvider` protocol (`identification.py:31`) exposes only
`search`, with ISBN passed as a keyword argument. `_normalize_candidate`
(`identification.py:383`) hardcodes Open Library payload keys: `author_name`,
`isbn_10`, `isbn_13`, `first_publish_year`, `title_suggest`, `edition_name`, and
`key`.

**Defect.** Sprint001 and `docs/metadata-providers.md` both specify an abstraction
with `search`, `lookup_isbn`, and normalized output. AGENTS.md requires a provider
abstraction "rather than coupling to one service". A second provider configured
through `BOOK_SCANNER_METADATA_PROVIDERS` today produces candidates titled
"Unknown title" with no authors, no identifiers, and a score near zero — silently,
because `_provider_instances` swallows construction errors and `resolve_candidates`
swallows query errors.

**Change.**

- Add `lookup_isbn` to the protocol and route ISBN evidence through it instead of
  the `search(isbn=...)` side channel.
- Move normalization behind the provider boundary: each provider returns a
  normalized candidate structure. Scoring stays central and provider-agnostic and
  must never read a provider-specific key.
- Keep `OpenLibraryMetadataProvider` behaviour byte-identical after the refactor;
  its existing tests must pass unchanged.
- Add a second provider implementation, or at minimum a conformance test double
  whose payload shape shares no field names with Open Library, and prove it
  produces correctly populated, correctly scored candidates.
- Keep the existing one-provider-failure-does-not-abort-intake behaviour, but log
  a distinguishable event when a configured provider fails to *construct*, which
  is currently indistinguishable from a provider that simply returned nothing.

**Done when.** A second provider can be added without editing shared scoring or
normalization code, and a conformance test proves it.

### D — Candidate persistence and lifecycle

**Current.** `_save_metadata_candidates` (`intake.py:715`) deletes and rewrites
candidates per job, marking each `MATCH` above auto-accept and `REVIEW` otherwise.

**Defects.**

1. `provider_ref` falls back to `""` when a payload has no `key`, `id`, or `isbn`
   (`identification.py:440`). The unique constraint
   `uniq_import_candidate_provider_ref` covers `(import_job, provider_id,
   provider_ref)`, so two reference-less candidates from one provider raise
   `IntegrityError` and fail the entire import. This is latent with Open Library,
   which always supplies `key`, and becomes reachable the moment task C adds a
   second provider.
2. `MetadataCandidate.CandidateState.REJECTED` is never assigned anywhere.
3. `BOOK_SCANNER_IDENTIFICATION_REVIEW` / `REVIEW_SCORE` is now dead in the intake
   path. Its only remaining reader is `best_candidate` (`identification.py:523`),
   which has no callers and whose two branches return the same value.

**Change.**

- Give reference-less candidates a deterministic synthetic reference (for example
  a stable hash of the normalized payload) so persistence cannot fail, and add a
  regression test with two reference-less candidates from one provider.
- Either give `REVIEW_SCORE` a defined role — for example, candidates below it are
  stored as `REJECTED` and hidden from the default review view but retained as
  evidence — or remove the setting, the constant, and the unused `best_candidate`
  helper. Do not leave an advertised setting that changes nothing.
- Document whichever choice is made in `docs/metadata-providers.md` and
  `.env.example`.

**Done when.** No candidate payload can fail an import, and every advertised
identification setting demonstrably changes behaviour.

### E — OCR provenance and polling budget

**Current.** `OCRPollResult` (`ocr.py:67`) carries `engine`, `model`, `language`,
and `confidence`, and `_coerce_poll_payload` populates them. `_submit_and_poll`
returns only the remote job id and the artifact list. `_materialize_ocr_artifact`
records the values from the *request*, defaulting to `converter_tool="ocr"` and
`converter_version="unknown"`.

**Defect.** Sprint012 requires recording engine, model/version, language,
settings, timestamps, and confidence metadata where available. The service reports
them and the code discards them, so a caller who does not pin an engine gets an
unattributable OCR variant. AGENTS.md requires OCR output provenance to be
traceable.

**Change.**

- Return the full `OCRPollResult` from `_submit_and_poll` and persist
  service-reported engine, model, language, and confidence on the derived variant.
- Prefer service-reported values over requested values, and record both when they
  differ so a mismatch is visible rather than silently resolved.
- Replace the polling defaults. `_poll_limit` × `_poll_interval` (`ocr.py:408`)
  gives roughly an eight-second total budget before `"OCR polling timed out"`;
  no realistic page OCR completes in that window, so the shipped defaults fail
  every genuine job. Choose values sized for the hackerspace AI server and state
  the reasoning in `docs/operations.md`.
- The poll loop blocks an RQ worker with `time.sleep` for the whole job. Either
  bound that occupancy (re-enqueue a continuation instead of sleeping) or document
  the worker-count implication explicitly in `docs/operations.md`. Do not leave it
  undocumented.

**Done when.** An OCR result reporting engine, model, language, and confidence
persists all four on the variant even when the request specified none, and the
default polling budget is defensible for the real service.

### F — Intake state-machine observability

**Current.** `process_import_job` assigns `SIGNAL_NORMALIZATION`
(`intake.py:1144`) and `MATCHED` (`intake.py:1213`) and overwrites both before any
`save()`. `LOW_CONFIDENCE` at `intake.py:1171` is assigned and immediately replaced
by `ARCHIVING`.

**Defect.** AGENTS.md specifies the progression `DISCOVERED -> HASHING ->
METADATA_EXTRACTION -> SIGNAL_NORMALIZATION -> EXTERNAL_IDENTIFICATION ->
MATCHED/LOW_CONFIDENCE -> ARCHIVING -> COMPLETE`. Three of those states never
reach the database, so a client polling `/imports/<job-id>/` can never observe
them and the documented state machine is not the implemented one.

**Change.**

- Persist `SIGNAL_NORMALIZATION` and `MATCHED` at their correct points.
- Remove the dead `LOW_CONFIDENCE` assignment.
- Keep the number of writes proportionate; batching adjacent field updates is fine
  as long as each documented state is durably observable.
- Assert the observable sequence in a test rather than only the terminal state.

**Done when.** A test observes the full documented progression through the status
endpoint.

### G — Concurrency contract hardening

**Current.** `scan_page_stage` checks `expected_revision` only when the client
sends it (`views.py:2009`), outside any transaction. The server-rendered workspace
form already submits a hidden `expected_revision`
(`templates/core/scan_workspace.html:135`) and the TypeScript editor always sends
it (`frontend/scantailor-workspace.ts:805`).

**Defect.** AGENTS.md requires detecting conflicts "instead of silently
overwriting edits". A client that omits the field silently wins, and because the
read and the write are not in one transaction, two requests carrying the same
expected revision can both succeed. Both shipped clients already comply, so
tightening the contract breaks nothing the project ships.

**Change.**

- Make `expected_revision` mandatory for stage mutations; reject omission with
  `400`.
- Perform the revision comparison and the save in one transaction with
  `select_for_update` on the stage state.
- Leave `scan_job_stage_batch` exempt by design, but document that batch
  application deliberately overwrites and record the revision it replaced.
- Update the HTTP contract section of `docs/scantailor-web.md` accordingly.

**Done when.** A mutation without `expected_revision` is rejected, and two
concurrent mutations with the same expected revision yield exactly one success and
one `409`.

### H — Converter and worker resource limits

**Current.** `_run_conversion_command` (`conversion.py:533`) calls
`subprocess.run` with no timeout, no resource limits, and no privilege separation.

**Defect.** AGENTS.md lists "runaway worker jobs" among the threats to defend
against, and Sprint009 asks for converters to run in restricted worker processes
where practical. Today the only backstop is RQ's job timeout, and
`docs/operations.md` delegates all containment to the operator.

**Change.**

- Give every converter invocation an explicit timeout, and record a timeout as a
  normal conversion failure with a distinguishable error rather than an unhandled
  exception.
- Apply the in-process limits the platform supports — working-directory
  confinement and process resource limits — so the application enforces part of
  the boundary itself.
- Keep the container and host guidance in `docs/operations.md`, but state
  explicitly which limits the application enforces and which remain the operator's
  responsibility.

**Done when.** A converter that never exits is terminated by the configured
timeout and its job is recorded as failed with a clear reason.

### I — Complete Sprint007 unattended ingestion

**Current.** Discovery happens only when someone runs `manage.py ingest_sources`
on the application host. `SourceLocation` is editable in Django admin;
preview, execution, and retry are command-line only. `ProcessingJob.JobType.INGESTION`
exists in the model and is never used.

**Defect.** Sprint007 requires polling or watching the configured incoming folder
"using a robust approach suitable for network filesystems", and its acceptance
criterion has an administrator preview and run an import. Neither the schedule nor
the administrator-facing workflow exists. Every other workflow in this project
offers both JSON and HTML through content negotiation; this one offers neither.

**Change.**

- Add a supported unattended discovery mechanism appropriate for network
  filesystems, and document it in `docs/operations.md` with a concrete timer or
  cron example, including what happens when a mount is temporarily unavailable.
- Add authenticated admin-only routes for source preview, import execution, and
  failed-job retry, returning JSON and HTML through the existing negotiation
  helpers and reusing `_require_role("admin")`.
- Report progress through a durable `ProcessingJob` of type `INGESTION` rather
  than inventing a parallel status model; the type already exists for this.
- Keep `SourceLocation` configuration in Django admin. Only the actions move to
  the application surface.
- Preserve the existing guarantees: COPY retains the source, MOVE deletes only
  after a hash-verified destination, failures stay visible and retryable, and
  duplicates across source paths reuse the existing variant.

**Done when.** An administrator can register a network directory, preview it,
choose COPY or MOVE, run the import, and retry failures without shell access to
the application host, and discovery also runs unattended on a documented schedule.

### J — ScanTailor status honesty

**Current.** `_run_perspective_dewarp` (`scantailor.py:990`) applies a four-point
projective transform. The `fox` output mode dispatches to `threshold_niblack`
(`scantailor.py:1214`) and `wolf` is a hand-rolled "Wolf-like approximation"
(`scantailor.py:1231`). `zone_mode="auto"` (`scantailor.py:1289`) performs no
text/picture separation at all. `docs/sprint_progress.md` lists Sprint016 and
Sprint017 as "Implemented" without qualification.

**Defect.** A projective transform corrects a tilted flat page; it cannot correct
the cylindrical curl of a photographed book, which is the problem Sprint016
exists to solve, and the required "fixture suite covering mild and strong
curvature" does not exist. Sprint017 names six output algorithms and asks for
text/picture zone separation; two names do not match their implementations,
Sprint017 explicitly says to avoid bespoke implementations where compatible
libraries exist, and separation is manual-only.

**Change.**

- Rename the dewarping algorithm and its recorded `algorithm` value to describe
  what it does, so stored provenance is not misleading. Existing stage states keep
  their recorded history; document the rename.
- Either bind `fox` and `wolf` to correct implementations or advertise their real
  names in the API, the UI, and `docs/scantailor-web.md`.
- State plainly in `docs/scantailor-web.md` that text/picture separation is manual
  only.
- Add the curvature fixture suite Sprint016 requires, covering mild and strong
  curvature, together with a measurable regression metric. The current stage will
  not correct these fixtures; the suite establishes the baseline Sprint020 must
  improve on and proves the bypass path stays safe.
- Qualify Sprint016 and Sprint017 in `docs/sprint_progress.md` with what is
  implemented and what is deferred.

**Done when.** No stage, recorded algorithm name, or document claims behaviour the
code does not have, and the curvature fixtures run in CI.

### K — Documentation reconciliation

**Current.** `docs/architecture.md` commits to a boundary map of
`ebook_server.library`, `ebook_server.scan`, `ebook_server.jobs`, and
`ebook_server.workers`. All application code lives in `ebook_server.core`, with
`views.py` at roughly 3,600 lines spanning library, scan, ScanTailor, review, and
health concerns.

**Defect.** Sprint001's acceptance criterion is that a new developer can read the
architecture documents and understand the intended boundaries. The document
currently describes a layout that does not exist.

**Change.**

- Correct `docs/architecture.md` to describe the actual layout, or record the
  split as planned work with an explicit status. Do not leave the current text.
- Update `docs/requirements-audit.md` with every correction landed here and every
  item deferred, keeping its existing habit of separating verified work from
  remaining gaps.
- Update `docs/sprint_progress.md` with a Sprint019 row and the Sprint016/017
  qualifications from task J.

**Done when.** Every document describes behaviour the code has.

### L — Streaming and backend consistency

- Resolve duplicate-detection verification through the variant's declared backend
  rather than the default library backend (`intake.py:1135`). It currently fails
  safe by keeping the source, but it is inconsistent with the backend-aware
  handling used everywhere else and would silently stop verifying if artifact
  namespaces ever overlap.
- Stream instead of buffering whole artifacts in `_zip_scan_job_pages`,
  `_materialize_ocr_artifact`, and `_stage_scan_page_from_existing_version`. A
  searchable PDF for a full book is currently held entirely in worker memory.

## Sequencing

1. Task A first and alone; nothing else lands on a red tree.
2. Task C before task B's final tuning — the scorer should be tuned against the
   provider-agnostic candidate shape, not the Open Library one.
3. Task D before or with task C — the reference-less candidate defect becomes
   reachable as soon as a second provider exists.
4. Tasks E, F, G, H, L are independent and can proceed in parallel.
5. Task I after F, so ingestion progress reporting builds on a correct state
   machine.
6. Tasks J and K last, recording the state the rest of the sprint leaves behind.

## Tests

Identification and providers:

- Author scoring does not decrease when a correct author hint is accompanied by an
  incorrect one derived from the filename split.
- `Doe, Jane` in embedded metadata matches a `Jane Doe` candidate.
- A perfect non-ISBN title and author match auto-accepts under the tuned rule.
- A strong title match with no author corroboration routes to review.
- Language and publisher signals fire on realistic representations
  (`en` against `eng`, `Crown` against `Crown Publishing`).
- Provider `subtitle` lands in a subtitle field and is not shown as a description.
- A provider whose payload shares no field names with Open Library produces
  correctly populated, correctly scored candidates.
- `lookup_isbn` is used for ISBN evidence, and a failure inside it does not abort
  intake.
- Two reference-less candidates from one provider persist without error.
- Whichever role `REVIEW_SCORE` is given is asserted, or its removal is asserted
  by its absence from settings.

Pipeline and contracts:

- An OCR result reporting engine, model, language, and confidence persists all
  four on the derived variant when the request specified none.
- A request-versus-service provenance mismatch is recorded rather than silently
  resolved.
- Polling a running import observes `SIGNAL_NORMALIZATION` and `MATCHED` in order.
- A stage mutation without `expected_revision` is rejected with `400`.
- Two concurrent stage mutations carrying the same expected revision produce
  exactly one success and one `409`.
- A converter that does not exit is terminated by the configured timeout and the
  conversion job is recorded as failed.

Ingestion and ScanTailor:

- Admin source preview, import execution, and retry through the authenticated web
  surface, including a non-admin rejection for each.
- Unattended discovery creates import jobs without a manual command, and a missing
  or unavailable source path leaves the run recoverable and visible.
- COPY retains the source and MOVE deletes it only after verified archival, both
  through the new web action.
- Curvature fixtures load, run through the pipeline, and produce deterministic
  recorded parameters; the bypass path leaves the input unchanged.

Regression protection: existing coverage for MOVE/COPY safety, duplicate reuse,
path traversal, archive bombs, review permissions, conflict detection,
finalization safeguards, and storage consistency must continue to pass unchanged.

## Security

- Tightening `expected_revision` must not become a way to probe stage state:
  rejection for a missing field and rejection for an unauthorized job must remain
  distinguishable only to authorized users.
- New admin ingestion routes are administrator-only, must reuse the existing
  role check, and must not accept a request-supplied absolute path — imports
  address a registered `SourceLocation` by id, never a raw path.
- Converter timeouts must terminate the whole process group, not only the direct
  child, so a wrapper script cannot outlive its parent.
- Provider normalization now runs on untrusted third-party payloads inside the
  provider; keep the existing bounded-parse discipline and do not let a provider
  response reach the database unvalidated.
- OCR responses remain untrusted artifacts; recording service-reported engine and
  model strings must bound their length and must not log document content.

## Acceptance Criteria

- `uv run pytest -q`, `uv run mypy src tests`, `uv run ruff check .`,
  `uv run ruff format --check .`, `npm run check`, and `npm test` all pass on a
  clean tree, and the infrastructure suite still passes.
- A well-named EPUB with correct embedded metadata and no ISBN reaches a canonical
  Book without human review, and the threshold choice is justified in writing.
- A second metadata provider can be added without editing shared scoring or
  normalization code.
- No candidate payload shape can fail an import.
- An OCR-derived variant records the engine, model, language, and confidence the
  service reported.
- A client polling an import observes the documented state progression.
- No stage mutation can silently overwrite a concurrent edit.
- A runaway converter is terminated by the application, not only by the queue.
- An administrator can preview, import, and retry source paths from the web
  application, and discovery runs unattended on a documented schedule.
- No stage, algorithm name, or document claims behaviour the code does not have.
- `docs/requirements-audit.md` distinguishes corrected items from deferred ones.

## Out of Scope

Deferred to Sprint020, with the fixtures and honest status produced here as its
starting point:

- A genuine curvature dewarping model — cylindrical or spline surface estimation —
  to replace the projective transform.
- Automatic text/picture zone segmentation for the output stage.
- Splitting `ebook_server.core` into the documented module boundaries, if the
  decision is to change the code rather than the document.
- Recognition-quality validation against the hackerspace AI server, which still
  requires a site-connected acceptance environment.
- Any new conversion routes; the matrix already marks FB2 and DjVu as researched
  and not advertised, and that remains correct.

## Findings index

| # | Finding | Location | Requirement |
| --- | --- | --- | --- |
| 1 | Two tests assert pre-fix auto-accept behaviour | `tests/test_source_ingestion.py` | Sprint005 |
| 2 | Author score divided by hint count, so evidence lowers it | `identification.py:462` | Sprint005 |
| 3 | Author matching is exact equality, so `Doe, Jane` never matches | `identification.py:462` | Sprint005 |
| 4 | Filename split feeds both sides into author and title hints | `identification.py:164` | Sprint005 |
| 5 | Language compared without normalization (`en` vs `eng`) | `identification.py:494` | Sprint005 |
| 6 | Publisher compared by exact equality | `identification.py:497` | Sprint005 |
| 7 | Candidate `description` populated from provider `subtitle` | `identification.py:446` | Sprint005, Sprint006 |
| 8 | Non-ISBN matches cannot reach the auto-accept threshold | `identification.py:472` | Sprint005 |
| 9 | Provider protocol lacks `lookup_isbn` | `identification.py:31` | Sprint001 |
| 10 | Normalization hardcodes Open Library payload keys | `identification.py:383` | AGENTS.md, Sprint001 |
| 11 | Reference-less candidates collide on the unique constraint | `identification.py:440`, `models.py:841` | Sprint005 |
| 12 | `REJECTED` candidate state never used | `models.py:788` | Sprint005 |
| 13 | `REVIEW_SCORE` and `best_candidate` are dead | `identification.py:28`, `identification.py:523` | Sprint005 |
| 14 | Service-reported OCR engine/model/language/confidence discarded | `ocr.py:67`, `_submit_and_poll` | Sprint012 |
| 15 | OCR polling budget of about eight seconds | `ocr.py:408` | Sprint012 |
| 16 | Blocking poll loop holds a worker for the whole OCR job | `ocr.py` `_submit_and_poll` | AGENTS.md |
| 17 | `SIGNAL_NORMALIZATION` and `MATCHED` never persisted | `intake.py:1144`, `intake.py:1213` | AGENTS.md |
| 18 | Dead `LOW_CONFIDENCE` assignment | `intake.py:1171` | AGENTS.md |
| 19 | `expected_revision` optional and checked outside a transaction | `views.py:2009` | AGENTS.md, Sprint013 |
| 20 | Converter subprocesses have no timeout or sandbox | `conversion.py:533` | AGENTS.md, Sprint009 |
| 21 | No incoming-folder watcher, scheduler, or admin import surface | `management/commands/ingest_sources.py` | Sprint007 |
| 22 | Dewarping is projective only; no curvature fixtures | `scantailor.py:990` | Sprint016 |
| 23 | `fox` is Niblack; `wolf` is an approximation; no auto zone separation | `scantailor.py:1214`, `1231`, `1289` | Sprint017 |
| 24 | Documented module boundary map does not exist | `docs/architecture.md` | Sprint001 |
| 25 | Duplicate verification uses the default backend | `intake.py:1135` | AGENTS.md |
| 26 | Whole artifacts buffered in memory | `ocr.py`, `views.py:663` | AGENTS.md |

## Verified and unchanged

Checked against the specifications during this audit and requiring no work in this
sprint: canonical Book/FileVariant separation and derived-variant lineage;
hash-based duplicate detection that never relies on filename or size; MOVE
deletion only after a hash-verified destination; path-traversal defences at both
the filename and storage-resolution layers; archive member-count, ratio, size, and
unsafe-path limits with defused XML parsing; fail-closed LDAP with escaped DN and
filter inputs and no local registration path; production configuration validation
for PostgreSQL, Redis, LDAP, secrets, hosts, HTTPS, cookies, and HSTS;
integrity-aware admin book deletion with quarantine and restore; durable queued
scan finalization that refuses raw captures without a recorded override;
downstream invalidation inside ScanTailor and outward into derived library
variants; the honest conversion matrix; bearer-protected metrics with persistent
state collection; and the static-analysis, migration, and dependency-audit CI
gates.
