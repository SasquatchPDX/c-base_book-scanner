# Sprint010 — Scanner Foundation

## Goal

Introduce persistent physical-book scan jobs and a stable scanner/server API.

## Tasks

Implement:

- `ScanJob`
- `ScanPage`
- scanner identity/configuration as needed
- scan-job status lifecycle
- server-generated UUIDs

API should support at least:

- create scan job
- list/select available jobs
- fetch job details
- upload page
- replace/re-upload page idempotently
- finish/pause job

Page upload metadata should include:

- logical sequence number
- side if applicable
- checksum
- capture timestamp
- scanner metadata

- Preserve raw page images as immutable/versioned artifacts.
- Link ScanJob to provisional Book metadata or an existing Book.
- Expose a basic web view of scan jobs and page thumbnails.
- Keep scanner protocol hardware-neutral.

## Tests

- Create/list scan job.
- Ordered page uploads.
- Idempotent duplicate request.
- Interrupted upload/retry.
- Page version preservation.

## Acceptance Criteria

A physical scanner client can create or select a scan job and upload an ordered batch of page images that persist in the server with stable logical page identities.
