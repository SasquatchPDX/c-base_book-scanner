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
  - uses a root resolved from each artifact's `StorageLocation`.
  - normalizes and guards all resolved paths against traversal.
  - computes SHA-256 as required for duplicate detection.
  - stores artifacts as file blobs for immediate development and test usage.

## Example destination format

- `<namespace>/<original_filename>`
- where namespace is domain-defined (`ingests`, `derived/scan/<scan_job_id>`, etc.).

## Backend resolution and streaming

`core.storage_registry.backend_for_location` resolves local storage and externally
mounted NFS/Samba shares to the filesystem backend. The reserved `default` and
`scan` locations use `APP_DATA_DIR/library-artifacts` and
`APP_DATA_DIR/scan-artifacts`; other locations use their configured base path.
Unknown backend values fail closed. Object storage is not implemented.

Duplicate intake verifies the existing variant on its declared backend before
removing a MOVE source. A matching identifier on another backend cannot authorize
deletion; COPY always retains its source.

Scan-page staging, OCR ZIP packaging, and HTTP OCR artifact downloads use bounded
reads. OCR outputs are hashed while streaming to a temporary file, then stored
under a fresh artifact namespace. Repeated filenames cannot overwrite earlier
outputs; identical content with the same Book, source variant, and output format
reuses the existing OCR variant. Partial downloads remove the temporary file and
do not register a variant. Inline base64 OCR responses are decoded in chunks, but
their JSON response still occupies memory; large results should use download URLs.
