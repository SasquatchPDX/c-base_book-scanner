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


## Read-only inventory and reversible derivative quarantine

`inventory_storage` reports filesystem bytes and database references, grouped by
location, artifact kind and status, with per-file size and age. It includes raw
scan versions, immutable scan covers/logs, rendered JSONL paths, covers and
thumbnails, library variants, archived imports, Book OPF targets and ScanTailor
inputs/outputs/previews. Scanner OPF versions are database bytes, not filesystem
artifacts. Missing references have unknown byte counts; `--verify-hashes` checks
all recorded expected digests independently. Symlinks are never followed.
`.quarantine` and `.trash` are excluded from the filesystem walk. Aliased logical
locations can report the same physical bytes more than once.

Inventory is read-only: it does not create storage roots, register locations,
repair references or remove files. Start with a bounded, location-scoped report
and save it outside library storage:

```sh
uv run python manage.py inventory_storage --location scan --min-age-days 30 > /tmp/storage-plan.json
uv run python manage.py quarantine_derivatives --plan /tmp/storage-plan.json
uv run python manage.py quarantine_derivatives --plan /tmp/storage-plan.json --apply --limit 100
```

The walk defaults to 10,000 entries (`--limit`); an incomplete walk or any inventory
error prevents application of its plan. Plans are limited to 16 MiB. The second
command is a dry run. Review the report and the fresh dry run before adding
`--apply`. Collection defaults to 100 candidates and accepts at most 1,000 per
invocation. Re-running a plan skips already missing files.

Only old, unreferenced `output.png`/`preview.png` files beneath a known ScanTailor
stage revision strictly older than its current revision are eligible, only on
writable `scan` storage. The default retention is 30 days and cannot be below one
day. Unknown ownership, originals/raw captures, current revisions, registered
source roots, read-only roots, referenced aliases and jobs with active processing
or finalization remain protected. Every application rechecks root binding,
mount/sentinel health, file identity, checksum, age, fresh references and activity.
Inventory plans are evidence, never authority to delete arbitrary paths.

**Upgrade and restart all ScanTailor workers before enabling quarantine.** The
new publisher copies privately, then checks stage ownership under the same row
lock before promoting final paths. This prevents an old worker from recreating a
quarantined revision. Inventory alone is safe during a rolling deployment;
quarantine is not supported while older, unfenced publishers remain running.

Quarantine preserves bytes on the same backend beneath
`.quarantine/derived/<uuid>/artifact`, with a journal written before the move.
There is no purge command or automatic deletion policy: quarantine is retained
indefinitely until an operator decides a separate retention policy. Both hard-link
and exclusive-copy fallback publication are supported. An interrupted copy keeps
its source; a crash after moving leaves a planned journal that still supports
restoration. No site inventory or cleanup is run as part of deployment.

To restore a reviewed journal, first dry-run, then explicitly apply:

```sh
uv run python manage.py quarantine_derivatives --location scan --restore .quarantine/derived/<uuid>/manifest.json
uv run python manage.py quarantine_derivatives --location scan --restore .quarantine/derived/<uuid>/manifest.json --apply
```

Restoration checks journal binding, checksums and current protections, and never
overwrites an occupied original path. A retry after a successful restore whose
journal update failed reports `original_present` only after verifying its digest.
If stage ownership was deleted or a journal/artifact is inconsistent, preserve the
quarantine and journal for manual investigation; the command cannot infer safe
ownership. This workflow manages derived artifacts, not external backups.
