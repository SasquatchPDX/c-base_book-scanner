# Sprint Progress

This file tracks execution status while advancing Sprint documents in order.

> Historical implementation log: “Implemented” means the planned coding pass has
> repository evidence. It is not an acceptance or production-readiness sign-off.
> The current requirement-by-requirement findings, including remaining UI,
> pipeline, observability, and integration gaps, are in
> `docs/requirements-audit.md`.

## Current snapshot

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
| 016 | Implemented | Practical dewarping with draggable control points, projective transform path, safe fallback modes, and reproducibility/bypass/failure tests |
| 017 | Implemented | Pluggable Otsu/Sauvola/Wolf/Fox/Window/Grad output, direct text/picture zones, threshold controls, and regression tests |
| 018 | Implemented | Added durable queued scan identification/review and finalization, processed-page safeguards, OCR + conversion chain coverage, pipeline status, downstream invalidation, and production-shaped Chromium acceptance for the synthetic physical-book flow |

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
- Current remaining work is quality-fixture coverage for reverse Calibre routes,
  importing and exercising the supplied monitoring assets, and browser-driven
  full physical-book acceptance. PostgreSQL, seeded OpenLDAP, Redis/RQ, Nginx,
  Uvicorn, Playwright Chromium, and live readiness checks now run in CI.
- Sprint009 now additionally has a pinned real-Pandoc acceptance path spanning a
  member HTTP request, Redis, a Django-aware RQ worker process, provenance-linked
  EPUB storage, validation, and authenticated download. Pinned Calibre 9.11.0
  similarly verifies EPUB-to-PDF and EPUB-to-MOBI; reverse routes remain
  binary-advertised but need representative quality fixtures.
- Sprint013 now has a pinned Nginx/Uvicorn/PostgreSQL acceptance path for
  authenticated, unbuffered SSE state updates and clean client disconnects. A
  two-member Playwright scenario additionally proves that stale revisions cannot
  silently overwrite a concurrent ScanTailor edit.
