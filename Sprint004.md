# Sprint004 — Upload and Intake MVP

## Goal

Allow a member to upload a supported e-book file and reliably archive it as an intake job with hash-based exact duplicate detection.

## Tasks

- Implement `ImportJob` and intake state machine.
- Implement authenticated upload UI/API.
- Validate supported format/MIME/extension safely.
- Stream uploads rather than assuming files fit comfortably in memory.
- Compute SHA-256.
- Detect exact duplicate hashes.
- Implement the storage service operations needed for archive placement.
- Preserve original filenames as metadata while generating safe storage destinations.
- Store ingestion provenance and uploader.
- Extract available embedded metadata using mature libraries/tools.
- Create a provisional Book/FileVariant relationship sufficient for later identification.
- Generate basic cover/thumbnail when safely supported by existing tools.
- Expose job status and errors.
- Make intake retryable rather than silently dropping failures.

## Security

- Reject path traversal filenames.
- Do not shell-concatenate user filenames into commands.
- Enforce upload size/resource limits.
- Treat document parsers as untrusted-input surfaces.

## Tests

- EPUB upload.
- PDF upload.
- Exact duplicate upload.
- Malformed/unsupported file.
- Dangerous filename.
- Storage failure leaves source/upload recoverable.

## Acceptance Criteria

A logged-in member can upload an EPUB or PDF, the server hashes it, archives it safely on configured storage, registers a FileVariant, and shows successful job status. Re-uploading the exact same bytes does not create a second physical copy.
