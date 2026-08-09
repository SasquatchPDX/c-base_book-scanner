# Sprint Progress

This file tracks execution status while advancing Sprint documents in order.

> Historical implementation log: “Done” means the originally recorded coding
> pass was completed. It is not an acceptance or production-readiness sign-off.
> The current requirement-by-requirement findings, including remaining UI,
> pipeline, observability, and integration gaps, are in
> `docs/requirements-audit.md`.

## Current snapshot

| Sprint | Status | Completed artifacts |
| --- | --- | --- |
| 001 | ✅ Done | Architecture/research docs, tool-choice docs, project skeleton, dependency/tooling baseline, health endpoints, CI |
| 002 | ✅ Done | Local auth role model/profile, LDAP adapter boundary, Django auth backend scaffold, RQ queue module, and starter queue/storage settings + Sprint002 test coverage |
| 003 | ✅ Done | Canonical book/domain models (Book, Author, Identifier, FileVariant, CoverImage, Storage/Source locations) and baseline migration/test coverage |
| 004 | ✅ Done | Upload/intake path with staging, hashing, metadata extraction, deduplication, durable archive copy, and API status/update behavior |
| 005 | ✅ Done | Metadata provider abstraction, ISBN normalization/extraction, provider abstraction tests, candidate scoring + persistence, and intake confidence routing with low-confidence review tasks |
| 006 | ✅ Done | Review queue/workflow complete with manual decision actions (open/pending listing, owner/admin access, accept/manual/merge/unidentified, tests, migration) |
| 007 | ✅ Done | Source-path ingestion implemented (preview, recursive scan, copy/move behavior, duplicate detection, recoverable failures) |
| 008 | ✅ Done | Search/browse/detail/download APIs, filters/sort/pagination, cover serialization, download streaming and tests |
| 009 | ✅ Done | Conversion graph-based orchestration, conversion jobs/status endpoints, conversion job request flow, duplicate output reuse, provenance capture, and tests |
| 010 | ✅ Done | Scan job and scan-page pipeline implemented, API coverage and replacement/version tests in place |
| 011 | ✅ Done | Rescan queue implemented with page action, todo listing/claim/complete/cancel endpoints, and tests |
| 012 | ✅ Done | OCR client contract, async submission/polling, accepted/running/success/failure/timeout/malformed handling, variant/scan-job sources, API endpoints, and persistence/integration tests added |
| 013 | ✅ Done | Persistent per-page/per-stage workspace endpoints, stage orchestration + worker lifecycle, dependency/invalidation scaffolding, migration for `ScanTailorStageState`, and regression tests |
| 014 | ✅ Done | Page split and deskew stage algorithms (auto/manual split, skew correction), persistence of output parameters, and regression tests added |
| 015 | ✅ Done | Content selection and margin stages implemented with persisted parameters, precedence, and regression tests |
| 016 | ✅ Done | Practical dewarping implementation (auto/manual proxy, projective transform path), safe fallback modes, and regression tests for reproducibility/bypass/failure handling |
| 017 | ✅ Done | Output/Binarization implemented with pluggable algorithms (Otsu/Sauvola/Wolf/Fox/Window/Grad paths), optional CV2-powered local-threshold handling, zone controls, and regression tests added |
| 018 | ✅ Done | Added scan-job finish OCR + conversion chain coverage, OCR/cleaned variant pipeline summaries for job-level workflows, and scan-job finish endpoint wiring for end-to-end completion |

## Sprint evidence checklist (as of 2026-08-08)

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
- 013: ScanTailor workspace/versioned stage lifecycle in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 014: split and deskew transformations in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 015: content selection and margins in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 016: practical dewarping implementation and fallback handling in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 017: output/binarization pipelines and zone controls in `src/ebook_server/core/scantailor.py`, `tests/test_scantailor.py`.
- 018: scan-job finish, OCR/cleaned variant pipelines, and downstream invalidation in `src/ebook_server/core/scan_pipeline.py`, `src/ebook_server/core/views.py`, `tests/test_scan_jobs.py`.

## Notes

- Sprint001 and Sprint002 are accepted after passing test/lint/type-check checks and baseline docs being checked in.
- Current focus: ScanTailor-to-library end-to-end hardening (lineage edge-cases, explicit source selection rules, and operational metrics).
