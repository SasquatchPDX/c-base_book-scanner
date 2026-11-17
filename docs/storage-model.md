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

## Destination format

- `generate_destination(namespace, name)` returns `<namespace>/<name>`; namespaces
  containing `..`, empty components or an absolute prefix are rejected. The id is
  not reserved, so it is only used for namespaces one job or artifact owns
  (`books/<import job id>/`, `_unsorted/<import job id>/`, a per-revision scan
  folder); writing to it again replaces that owner's own file.
- `publish_unique(source, namespace, name, mode=copy|move|link)` is the only way to
  write into a namespace several writers share (a book's archive folder). Allocation
  and publication are one exclusive step: the source is copied to a hidden temp file
  beside the destination and given its final name with a hard link, which fails if
  the name exists (where hard links are unsupported the name is claimed with
  `O_CREAT|O_EXCL` and filled). On a collision, including one with a competing
  writer that won a moment earlier, a short random token is appended and the write
  retried. The readable name is sanitized by `storage.safe_filename` (NFC, no
  control/separator/Windows-invalid characters, no device names, byte-capped). An
  existing artifact is never overwritten.
- Archive namespaces come from `core.archive_layout` (`BOOK_SCANNER_ARCHIVE_LAYOUT`):
  a per-book folder such as `<Author>/<Title>/` assigned once and stored on
  `Book.archive_folder`, `_unsorted/<import job id>/` while an import awaits
  review, or the original `books/<import job id>/` under the `job` layout. See
  `docs/operations.md` ("Archive layout").

## Backend resolution and streaming

`core.storage_registry.backend_for_location` resolves local storage and externally
mounted NFS/Samba shares to the filesystem backend. The reserved `default` and
`scan` locations use `APP_DATA_DIR/library-artifacts` and
`APP_DATA_DIR/scan-artifacts`; other locations use their configured base path.
Unknown backend values fail closed. Object storage is not implemented.

## Read-only locations (index in place)

A `StorageLocation` with `read_only=True` resolves to `ReadOnlyFilesystemStorage`:
reads (`open`, `stream`, `hash`, `exists`, `stat`) work, while `store`, `copy`,
`move`, `delete` and `generate_destination` raise `ReadOnlyStorageError`. Sources in
`index` transfer mode reference files through such a location; their FileVariants
carry `source_mtime_ns` and `reference_state` for drift detection
(`core.referenced_files`). `storage_registry.writable_location_for()` maps a
read-only location to the `default` archive, so conversions, covers and OCR output
derived from referenced files are written there. Book deletion skips files on
read-only locations (records only).

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
