# Sprint001 Storage Model

Storage is abstracted so business logic never depends on hard-coded network paths.

## Rules

- Database identifiers are authoritative.
- Concrete filesystem/backend locations are implementation details returned by storage methods.
- Every operation uses logical storage IDs (artifact ids), not raw request-supplied paths.
- MOVE semantics are only allowed after verified copy in higher-level pipelines (enforced by pipeline code in later sprints).

## Required operations

The `StorageBackend` protocol defines:

- `store`
- `copy`
- `move`
- `delete`
- `exists`
- `hash`
- `open`
- `stream`
- `generate_destination`

## Initial implementation

- `LocalFilesystemStorage`:
  - uses a configured root path (default: `APP_DATA_DIR`).
  - normalizes and guards all resolved paths against traversal.
  - computes SHA-256 as required for duplicate detection.
  - stores artifacts as file blobs for immediate development and test usage.

## Example destination format

- `<namespace>/<original_filename>`
- where namespace is domain-defined (`ingests`, `derived/scan/<scan_job_id>`, etc.).

## Planned extension

- add SMB/NFS-aware backends or object storage wrappers while keeping the protocol stable.
