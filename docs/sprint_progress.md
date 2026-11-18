# Sprint Progress

This file tracks execution status while advancing Sprint documents in order.

> Historical implementation log: “Implemented” means the planned coding pass has
> repository evidence. It is not an acceptance or production-readiness sign-off.
> The current requirement-by-requirement findings, including remaining UI,
> pipeline, observability, and integration gaps, are in
> `docs/requirements-audit.md`.

## Release evidence

As of 18 November 2026, both CI jobs passed at `c83bf73`. The digital-library
and physical-book milestones have separate site acceptance checks. [CI run](https://github.com/SasquatchPDX/c-base_book-scanner/actions/runs/44278353270)

| Capability | Evidence in the repository | Outstanding acceptance |
| --- | --- | --- |
| Digital intake and review | CI exercises LDAP login, member upload, review, search, conversion and download. [Browser journey](../tests/browser/digital-library.spec.ts) | Record site LDAP, COPY/MOVE, mount-failure recovery and alert delivery. [#162](https://github.com/SasquatchPDX/c-base_book-scanner/issues/162) |
| Search and conversion workers | CI exercises PostgreSQL reassignment and rollback, concurrent conversion delivery and stale-worker takeover. [Search tests](../tests/integration/test_search_reassignment_postgres.py) · [Conversion tests](../tests/integration/test_conversion_concurrency.py) | Measure search latency during intake on 150,000 books. [#167](https://github.com/SasquatchPDX/c-base_book-scanner/issues/167) |
| Scanner and portal | Single-key authentication, scanner-assigned job IDs, event logging and rescan support are implemented. CI exercises concurrent portal edits and a synthetic scan-to-library journey. [Device protocol](scanner-protocol.md) · [Browser tests](../tests/browser/scantailor-concurrency.spec.ts) | Run a device-originated job through re-finish and download on the actual scanner. [#169](https://github.com/SasquatchPDX/c-base_book-scanner/issues/169) |
| ScanTailor and OCR | Workers process split, deskew, crop, margins, perspective and output. CI uses an OCR contract fixture. [Processing behavior](scantailor-web.md) · [OCR fixture](../tests/browser_fixtures.py) | Validate bound-page curvature, automatic picture zoning and recognition quality on representative pages. [#171](https://github.com/SasquatchPDX/c-base_book-scanner/issues/171) · [#172](https://github.com/SasquatchPDX/c-base_book-scanner/issues/172) · [#170](https://github.com/SasquatchPDX/c-base_book-scanner/issues/170) |

## Historical sprint record

The table below records the implementation passes. Later PRs also delivered individual
Sprint020 requirements, including the [ranked search backend](https://github.com/SasquatchPDX/c-base_book-scanner/pull/116),
[browse and thumbnail work](https://github.com/SasquatchPDX/c-base_book-scanner/pull/121) and [scanner authentication](https://github.com/SasquatchPDX/c-base_book-scanner/pull/135).
Sprint-wide acceptance remains open.

| Sprint | Status | Implementation evidence |
| --- | --- | --- |
| 001 | Implemented | Architecture/research docs, project skeleton, dependency/tooling baseline, health/readiness and secured Prometheus endpoints, structured logging, CI |
| 002 | Implemented | Local auth role model/profile, LDAP adapter boundary, Django auth backend scaffold, RQ queue module, and starter queue/storage settings + Sprint002 test coverage |
| 003 | Implemented | Canonical book/domain models (Book, Author, Identifier, FileVariant, CoverImage, Storage/Source locations) and baseline migration/test coverage |
| 004 | Implemented | Common upload/intake path with staging, hashing, format-aware metadata and cover extraction, deduplication, durable archive copy, and API status behavior; later expanded to all declared digital-book formats |
| 005 | Implemented | Metadata provider abstraction, ISBN normalization/extraction, provider abstraction tests, candidate scoring + persistence, and intake confidence routing with low-confidence review tasks |
| 006 | Implemented | Review queue/workflow with owner/admin access, accept/manual/merge/unidentified decisions, editable terms, and durable repeat provider searches for archived imports and scans |
| 007 | Implemented | Source-path ingestion implemented (preview, recursive scan, copy/move behavior, duplicate detection, recoverable failures) |
| 008 | Implemented | Search/browse/detail/download UI and API, cover streaming, completed-variant safeguards, backend-aware storage, and confirmed integrity-aware admin deletion |
| 009 | Implemented | Conversion graph-based orchestration, conversion jobs/status endpoints, conversion job request flow, duplicate output reuse, provenance capture, and tests |
| 010 | Implemented | Scan job and scan-page pipeline implemented, API coverage and replacement/version tests in place |
| 011 | Implemented | Rescan queue implemented with page action, todo listing/claim/complete/cancel endpoints, and tests |
| 012 | Implemented | OCR client contract, async submission/polling, accepted/running/success/failure/timeout/malformed handling, variant/scan-job sources, API endpoints, and persistence/integration tests added |
| 013 | Implemented | Server-rendered/API workspace, strict-TypeScript stage editor, persistent stage state, SSE updates, optimistic conflicts, worker lifecycle, and invalidation handling |
| 014 | Implemented | Page split and deskew algorithms plus direct split-line/manual-angle controls, persisted dimensions/parameters, and regression tests |
| 015 | Implemented | Content/margin stages plus draggable content geometry, alignment and batch controls, manual-exception preservation, and regression tests |
| 016 | Partial | Projective correction, control points, and bypass/failure coverage exist; curvature correction and its fixture baseline remain outstanding |
| 017 | Partial | Output modes and manual zones exist; `fox` is Niblack, `wolf` is approximate, and automatic segmentation is outstanding |
| 018 | Implemented | Added durable queued scan identification/review and finalization, processed-page safeguards, OCR + conversion chain coverage, pipeline status, downstream invalidation, and production-shaped Chromium acceptance for the synthetic physical-book flow |
| 019 | In progress | A and F implemented; L covers backend-aware duplicate verification and streamed scan/OCR artifacts, including preservation of repeated OCR filenames; documentation reconciliation is partial. See the requirements audit for open work |
| 020 | Partially implemented | Deferred algorithms, taxonomy/search, artifact retention, device identity, abuse controls, and other explicitly scoped development |

## Sprint evidence checklist (as of 2026-08-10)

- 001: architecture and migration/test baseline in `docs/`, `src/`, and `tests/test_smoke.py`.
- 002: auth role model and queue baseline in `src/ebook_server/core/auth.py`, `src/ebook_server/settings.py`, `tests/test_auth.py`.
- 003: domain entities and admin registration in `src/ebook_server/core/models.py`, `src/ebook_server/core/admin.py`, `tests/test_domain_models.py`.
- 004: upload/ingest path and hashing/dedup flow in `src/ebook_server/core/intake.py`, `tests/test_intake.py`.
- 005: identification provider abstraction and scoring in `src/ebook_server/core/identification.py`, `tests/test_identification.py`.
- 006: review workflow endpoints in `src/ebook_server/core/views.py` and tests in `tests/test_review.py`.
- 007: scheduled source ingestion and MOVE/COPY behavior in `src/ebook_server/core/intake.py`, `tests/test_source_ingestion.py`.
- 008: library browse/search/download in `src/ebook_server/core/views.py`, `tests/test_library.py`.
- 009: conversion graph/request flow in `src/ebook_server/core/conversion.py`, `tests/test_conversion.py`.
- 010: scan job/page APIs in `src/ebook_server/core/views.py`, `tests/test_scan_jobs.py`.
- 011: rescan queue and claim/complete actions in `src/ebook_server/core/models.py`, `tests/test_scan_todos.py`.
- 012: OCR adapters and async polling in `src/ebook_server/core/ocr.py`, `tests/test_ocr.py`.
- 013: ScanTailor workspace UI/versioned stage lifecycle in templates, `src/ebook_server/core/scantailor.py`, and `tests/test_scantailor.py`.
- 014: split and deskew transformations in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 015: content selection and margins in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 016: practical dewarping implementation and fallback handling in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 017: output/binarization pipelines and zone controls in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 018: scan identification/review and finish, OCR/cleaned variant pipelines, and downstream invalidation in `src/ebook_server/core/scan_identification.py`, `src/ebook_server/core/scan_pipeline.py`, `src/ebook_server/core/views.py`, `tests/test_scan_identification.py`, and `tests/test_scan_jobs.py`.

## Notes

- No sprint should be called acceptance-complete solely from this table; use the
  requirements audit and a deployment-specific acceptance run.
- Current remaining corrections are indexed in Sprint019 and the requirements
  audit. Monitoring acceptance and real AI-server recognition quality still need
  site-connected runs. Reverse Calibre quality fixtures and browser-driven
  physical-book acceptance already exist and run in the infrastructure suite.
- Sprint009 now additionally has a pinned real-Pandoc acceptance path spanning a
  member HTTP request, Redis, a Django-aware RQ worker process, provenance-linked
  EPUB storage, validation, and authenticated download. Pinned Calibre 9.11.0
  similarly verifies EPUB-to-PDF and EPUB-to-MOBI, plus representative reverse
  PDF/MOBI-to-EPUB metadata and readable-text preservation.
- Sprint013 now has a pinned Nginx/Uvicorn/PostgreSQL acceptance path for
  authenticated, unbuffered SSE state updates and clean client disconnects. A
  two-member Playwright scenario additionally proves that stale revisions cannot
  silently overwrite a concurrent ScanTailor edit.
