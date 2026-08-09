# Sprint006 — Manual Review Queue

## Goal

Provide a first-class human workflow for ambiguous metadata and unresolved intake.

## Tasks

- Implement generic `ReviewTask` model.
- Add `NEEDS_ATTENTION` / review queue UI.
- For metadata review display:
  - source filename/path
  - uploader/source
  - embedded metadata
  - inferred title/author
  - cover/thumbnail if available
  - ranked candidates
  - confidence scores
  - provider provenance
- Reviewer actions:
  - accept candidate
  - search again
  - modify search terms
  - manually enter metadata
  - merge with existing Book
  - mark unidentified
- Admins can resolve all tasks.
- Normal users may resolve their own upload tasks if authorization design allows it.
- Record reviewer and decision provenance.
- Design ReviewTask generically enough for future OCR, conversion, duplicate-conflict, and scan issues.

## Tests

- Permissions around review tasks.
- Candidate acceptance updates canonical Book linkage.
- Manual entry works without ISBN.
- Merge with existing Book preserves variants.
- Review decisions are auditable.

## Acceptance Criteria

An ambiguous import can be completely resolved through the web UI without touching the database or filesystem manually.

## Current state

- Review queue endpoints for low-confidence imports are implemented (`/reviews/...`).
- Permissions are enforced for uploader-admin model.
- Resolution actions implemented: accept candidate, manual metadata entry, merge existing book, mark unidentified.
- Migration `0005_reviewtask` added and tests cover queue creation, permissions, and all implemented resolution actions.
