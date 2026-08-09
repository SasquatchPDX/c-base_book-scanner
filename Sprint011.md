# Sprint011 — Rescan and Shared To-Do Queue

## Goal

Create the persistent feedback loop between web post-processing and the physical scanner.

## Tasks

Implement `ScanTodo` with at least:

- ID
- ScanJob
- ScanPage
- type (`RESCAN` initially)
- reason
- creator
- timestamps
- status: OPEN / CLAIMED / COMPLETED / CANCELLED

Implement:

- web action to flag a page for rescan
- scanner API to list jobs with open todos
- scanner API to list todos for selected job
- claim/complete semantics if useful for scanner workflow
- replacement upload retaining logical ScanPage identity
- prior page version preservation
- active-source switching
- downstream derived-artifact invalidation hooks

Do not require ScanTailor-Web to be complete; expose the rescan action from the basic scan-job page for now.

## Tests

- Create rescan todo.
- Scanner sees correct outstanding job/page.
- Replacement preserves old version.
- Todo completes on valid replacement.
- Downstream state is marked stale/invalid where applicable.

## Acceptance Criteria

A web operator can flag page 37 for rescan, the physical scanner can discover that request through the API, and a replacement image can be uploaded and activated without losing the old source image.
