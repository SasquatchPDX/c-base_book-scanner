# Sprint015 — Select Content and Margins

## Goal

Add content-box detection and margin/alignment workflow to ScanTailor-Web.

## Tasks

### Select Content

- Automatic content-area detection.
- Manual content rectangle adjustment.
- Per-page and batch controls.
- Preview overlays.

### Margins

- Configure page margins around selected content.
- Alignment controls consistent with intended ScanTailor workflow.
- Batch/default settings.
- Preserve stage parameters and outputs.

## Tests

- Content detection on text-heavy page.
- Manual override.
- Margin calculation reproducibility.
- Batch settings do not overwrite explicit per-page exceptions unintentionally.
- Changing content selection invalidates margin/dewarp/output artifacts.

## Acceptance Criteria

Operators can identify the usable content region, correct it manually, apply consistent margins/alignment across a book, and receive deterministic processed outputs.
