# Sprint018 — Full Physical-Book Pipeline

## Goal

Connect scanner intake, ScanTailor-Web, rescan, OCR, bibliographic identification, and library variants into one end-to-end workflow.

## Tasks

- Define scan-job completion transitions.
- On ScanTailor completion, create/register cleaned image-only variant/artifact.
- Make cleaned output eligible for OCR submission.
- Send OCR automatically or via explicit user action according to final product decision.
- Register OCR/searchable/text outputs under the same Book.
- Route bibliographic uncertainty through existing ReviewTask workflow.
- Support conversion of OCR-derived text into supported book formats where available.
- Ensure lineage/provenance can be traced from final EPUB/PDF back through OCR, cleaned pages, and raw scanner images.
- Ensure rescanning a source page invalidates and allows regeneration of affected downstream artifacts.
- Add user-visible pipeline status.
- Add E2E fixtures/test harness for a small synthetic scanned book.

## End-to-End Acceptance Test

`Create/select scan job -> capture pages -> process in ScanTailor-Web -> flag bad page -> scanner fulfills rescan -> finish clean page set -> OCR on AI server -> resolve bibliographic identity -> create library variants -> book appears in search -> member downloads or converts it`

## Acceptance Criteria

A physical book can enter the system at the scanner and emerge as a searchable, downloadable Book in the shared library without manual filesystem manipulation.

## Final Hardening

Before declaring the full project complete:

- review security boundaries
- review worker resource limits
- review failure/retry semantics
- verify storage consistency tooling
- verify audit/provenance data
- verify multi-user behavior
- verify documentation for operators/admins/developers
