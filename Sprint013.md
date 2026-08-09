# Sprint013 — ScanTailor-Web Framework

## Goal

Build the persistent multi-user workspace and worker architecture for ScanTailor-Web before attempting the difficult algorithms.

## Tasks

- Create ScanTailor workspace UI for a ScanJob.
- Stage-based sidebar and thumbnail batch view.
- Page selection and ordering.
- Persist per-page/per-stage processing state.
- Persist stage parameters and revision numbers.
- Implement processing worker queue separate from interactive HTTP requests.
- Generate low-resolution previews.
- Update browser when preview results are ready using polling, SSE, or WebSocket based on architecture choice.
- Implement dependency graph/invalidation engine for processing stages.
- Add optimistic concurrency or job-level lease mechanism.
- Provide clear stale/conflict indicators.
- Wire existing `Rescan needed` action into this workspace.

Processing state must record input artifact, parameters, algorithm/version, output artifact, timestamp, and status.

## Tests

- Two users can work on different jobs simultaneously.
- Concurrent edits to the same page are detected.
- Changing upstream parameters invalidates downstream stages.
- Worker failure does not corrupt job state.
- Preview artifacts are regenerated predictably.

## Acceptance Criteria

Users can open different scan projects simultaneously, browse pages, adjust placeholder stage settings, receive worker-generated previews, and see correct downstream invalidation even before advanced image algorithms are implemented.
