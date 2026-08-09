# Sprint016 — Dewarping

## Goal

Implement an initial practical dewarping stage for photographed book pages.

This is expected to be one of the most technically difficult parts of the project. Keep it isolated, measurable, and testable rather than letting it infect the whole codebase with geometry goblins.

## Tasks

- Implement the best research-backed dewarping approach identified in Sprint001.
- Keep dewarping behind a modular algorithm interface.
- Support automatic estimation where practical.
- Support manual correction/control points or equivalent interaction if required.
- Generate responsive low-resolution previews.
- Perform full-resolution processing asynchronously.
- Persist geometry/parameters so results are reproducible.
- Provide safe fallback/bypass for pages where dewarping is unnecessary or fails.
- Create a fixture suite covering mild and strong curvature.

## Tests

- Parameter persistence/reproducibility.
- Bypass path.
- Worker failure/retry.
- Upstream content/margin changes invalidate dewarp.
- Quantitative or image-difference regression tests where feasible.

## Acceptance Criteria

An operator can correct typical photographed-page curvature to a useful result, manually adjust when automatic estimation is inadequate, and preserve reproducible settings per page.
