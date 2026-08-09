# AGENTS.md — Hackerspace E-Book Server

## Mission

Build a shared, LDAP-authenticated e-book server for hackerspace members. The system ingests existing digital books and physical-book scans, identifies books against public bibliographic data, stores multiple file variants under a canonical Book record, provides search/download/conversion, and ultimately includes a multi-user web reimplementation of the ScanTailor Advanced workflow.

The project should reuse mature open-source libraries and command-line tools wherever sensible. Do not reimplement format parsing, document conversion, archive handling, OCR, or other solved problems merely for sport. We already have enough interesting problems.

ScanTailor-Web is the major deliberate reimplementation: reproduce the useful processing stages and workflow in a modern web architecture, using ScanTailor Advanced as an algorithmic and UX reference rather than as a codebase to port wholesale.

## Non-Goals

- Do not implement separate user registration. Authentication comes from the existing LDAP server.
- Do not store the main e-book collection on the application VM.
- Do not implement backup management. VM snapshots and network-storage backups are handled externally.
- Do not make scanner hardware responsible for library metadata resolution or image-processing logic.
- Do not make ScanTailor-Web a prerequisite for the first usable library release.

## Core Architecture Principles

### Book != File

A `Book` is the canonical bibliographic entity. A `FileVariant` is one digital representation of that book.

One Book may have many FileVariants: EPUB, MOBI, PDF, TXT, HTML, FB2, DjVu, CBZ/CBR, cleaned page images, OCR-derived files, and converted files.

Different formats or scans of the same book are not duplicates.

### Preserve Originals

Do not destructively overwrite original uploads or raw scans. Processing creates derived artifacts or replacement versions while retaining provenance.

Example lineage:

`Raw scanner photos -> cleaned page images -> OCR output -> EPUB/PDF`

### Asynchronous Work

Slow operations must run as background jobs, not inside web requests. This includes hashing large files, metadata lookup, file moves/copies, conversion, OCR, thumbnail generation, and ScanTailor processing.

### Storage Abstraction

The application VM stores application state, database data, caches, temporary files, and job metadata. Library files live on network storage mounted through NFS or Samba.

Do not scatter absolute network paths through business logic. Implement a storage service/backend abstraction.

### Persistent Shared Job State

The scanner, web application, ScanTailor-Web, OCR service, and workers coordinate through server-side persistent state. They must not depend on fragile direct peer-to-peer coupling.

### Human Review Is a First-Class Workflow

Low-confidence metadata matches, unresolved imports, scan issues, and similar cases should create explicit review tasks. Do not silently guess.

## Preferred Initial Stack

Codex should validate choices during Sprint001, but begin with these defaults unless research reveals a compelling reason not to:

- Backend: Python
- Web framework: Django + Django REST Framework, or FastAPI after explicit comparison
- Database: PostgreSQL
- Background jobs: Celery, Dramatiq, or RQ
- Broker/queue: Redis if appropriate
- Frontend: server-rendered pages for normal library workflows, focused JavaScript/TypeScript components for ScanTailor-Web
- Deployment: initially a modular monolith plus worker processes

Avoid premature microservices.

## Major Domain Objects

Implement or plan for the following entities:

- `User`
- `Book`
- `Author`
- `Identifier`
- `FileVariant`
- `CoverImage`
- `Category`
- `Genre`
- `StorageLocation`
- `SourceLocation`
- `ImportJob`
- `MetadataCandidate`
- `ReviewTask`
- `ProcessingJob`
- `ScanJob`
- `ScanPage`
- `ScanTodo`

Use database migrations for all schema changes.

## Canonical Book Model

A Book should support at least:

- title
- subtitle
- authors
- ISBN-10
- ISBN-13
- publisher
- publication date
- edition
- language
- categories/genres
- description
- front cover
- back cover

Do not assume every book has an ISBN.

## FileVariant Model

A FileVariant should support at least:

- `book_id`
- format
- MIME type
- original filename
- storage identifier/location
- size
- SHA-256 hash
- source type
- uploader
- created/imported timestamps
- parent/source variant where derived
- converter/tool provenance
- processing state

Exact duplicate detection is file-hash based. Never use filename or size alone.

## Ingestion Rules

All normal digital-book input routes should converge on one ingestion pipeline.

Expected state progression:

`DISCOVERED -> HASHING -> METADATA_EXTRACTION -> SIGNAL_NORMALIZATION -> EXTERNAL_IDENTIFICATION -> MATCHED/LOW_CONFIDENCE -> ARCHIVING -> COMPLETE`

Low-confidence or failed identification must route to manual review.

Supported digital intake sources:

1. Configured `incoming` folder.
2. Registered arbitrary network paths with COPY or MOVE semantics.
3. User upload through the web application.

A MOVE source file may only be deleted after the archive copy has been verified.

## Metadata Identification Rules

Raw metadata is evidence, not truth.

Collect signals from:

- embedded metadata
- sidecar metadata
- filename
- containing folder
- identifiers such as ISBN
- other safe signals where useful

All incoming books must go through external bibliographic lookup/verification using public metadata providers.

Implement provider abstraction rather than coupling to one service.

Candidate scoring may use ISBN, normalized title similarity, author similarity, edition, publication metadata, and language.

Thresholds must be configurable and empirically tunable.

Weak matches must not be silently accepted.

## Manual Review

Implement a reusable `ReviewTask` workflow.

For ambiguous imports, show:

- original filename/source
- inferred title/author
- embedded metadata
- available cover
- ranked metadata candidates
- confidence score

Reviewer actions should include:

- accept candidate
- search again
- edit search terms
- manually enter metadata
- merge with existing Book
- leave/mark unidentified

Admins can resolve all tasks. Normal users may be allowed to resolve their own uploads.

## Storage Rules

Create a storage service with operations such as:

- `store`
- `copy`
- `move`
- `delete`
- `exists`
- `hash`
- `open`
- `stream`
- `generate_destination`

The on-disk archive layout must be configurable. Database identifiers are authoritative; filesystem paths are implementation details.

## Search and Browse

Members must be able to search/browse by:

- title
- author
- category
- genre
- language
- available format

Search canonical Book metadata, not filenames.

Book pages should show metadata, covers, available variants, download options, and available conversions.

## Permissions

### Normal users

Can:

- authenticate through LDAP
- search and browse
- upload
- download
- request conversions
- participate in permitted scan/review workflows

Cannot:

- delete library files
- ban users
- perform destructive global administration

### Admins

Additionally can:

- ban/disable users locally
- delete files/book records where appropriate
- manage source paths
- manage storage/configuration
- resolve all review tasks

LDAP remains the authentication authority; local state may hold application roles and bans.

## Conversion Architecture

Implement converters through a generic abstraction. Do not assume one tool supports every pair.

Model conversion capabilities as a graph so multi-step paths can be chosen when necessary.

Every generated variant must record:

- source variant
- target format
- converter/tool
- tool version
- options
- job ID
- creation time

Prefer established tools such as Calibre CLI/components, Pandoc, ebooklib, Poppler, Ghostscript, DjVuLibre, and standard archive tooling where licensing and quality fit.

## OCR Integration

OCR compute runs on the existing hackerspace AI server. The e-book server acts as an asynchronous OCR client/orchestrator.

Record engine/model/version, language, source variant, job parameters, and output provenance.

Expected outputs may include plain text, structured page text, searchable PDF, and material suitable for EPUB generation.

## Scanner Protocol

Scanner integration is job-based.

A `ScanJob` must have persistent identity and state shared by the scanner and ScanTailor-Web.

Use server-generated UUIDs.

A scan job should support fields such as:

- job ID
- provisional/final Book link
- title/author hints
- operator
- scanner ID
- timestamps
- status
- optional expected page count

Each `ScanPage` should include:

- page ID
- job ID
- logical sequence number
- side if relevant
- filename/storage artifact
- checksum
- capture timestamp
- scanner metadata
- status/version

Uploads must be idempotent.

## Rescan Workflow

ScanTailor-Web can create persistent `ScanTodo` items, initially with `type=RESCAN`.

Suggested states:

- OPEN
- CLAIMED
- COMPLETED
- CANCELLED

When a replacement page arrives:

- preserve prior page versions
- retain the same logical page identity
- mark the replacement active
- complete the to-do
- invalidate downstream derived artifacts as necessary

The physical scanner UI should query jobs and outstanding rescan tasks from the server.

## ScanTailor-Web

Build this as a modern multi-user web subsystem with separate processing workers.

Required processing stages:

1. Page Split
2. Deskew
3. Select Content
4. Margins
5. Dewarping
6. Output/Binarization

Output algorithms should eventually include Otsu, Sauvola, Wolf, Fox, Window, Grad, plus text/picture zone handling.

For every page/stage, persist:

- input artifact
- parameters
- algorithm/version
- output artifact
- timestamp
- status

Changing an upstream stage must invalidate dependent downstream stages.

Do not port the original C++/Qt application wholesale. Reproduce required behavior cleanly in the new stack.

## Multi-User ScanTailor Behavior

Multiple members must be able to work on different scan jobs simultaneously.

Avoid global mutable state.

Prefer job-level editing leases or page-level optimistic concurrency with revision numbers. Detect conflicts instead of silently overwriting edits.

Use reduced-resolution previews for interactive operations where practical; perform full-resolution processing asynchronously.

## Security

Treat all uploaded documents and archives as untrusted.

Defend against:

- path traversal
- arbitrary filesystem access
- command injection
- malformed parser inputs
- archive extraction attacks / zip bombs
- abusive upload sizes
- runaway worker jobs

Run converters and image-processing tools with restricted privileges where practical.

Never derive filesystem permissions from LDAP identity.

## Observability

Provide structured logs and useful metrics for:

- queue depth
- job duration
- ingestion success/failure
- metadata confidence distributions
- provider/API failures
- conversion failures
- OCR failures
- ScanTailor worker duration/failures

Do not log book contents.

## Testing Expectations

Every sprint must include tests.

### Static analysis

Configure formatting, linting, type checking, and dependency/security checks.

### Unit tests

Prioritize filename parsing, metadata normalization/scoring, storage destination generation, conversion planning, permissions, and image-processing parameter logic.

### Integration tests

Cover PostgreSQL, LDAP adapter, storage, queue, metadata providers, OCR API, and converters using test doubles where appropriate.

### E2E tests

Maintain end-to-end coverage for upload, manual review, conversion, scanning, rescan, OCR, and eventual full scan-to-library workflow.

## Development Rules for Codex

1. Read this file and the active Sprint00x.md before making changes.
2. Do not implement work from later sprints unless it is a small prerequisite explicitly needed by the current sprint.
3. Prefer mature open-source tools over custom implementations.
4. Keep Book and FileVariant separate everywhere.
5. Preserve originals and processing provenance.
6. Never delete a MOVE source until destination integrity is verified.
7. External metadata verification is mandatory for intake.
8. Surface uncertainty as review tasks; never hide guesses.
9. Keep slow work in background workers.
10. Keep storage access behind an abstraction.
11. Keep scanner/ScanTailor coordination in persistent server state.
12. Make ScanTailor algorithms modular and independently testable.
13. Add migrations, tests, and documentation with each feature.
14. Do not broaden the sprint scope because an adjacent feature looks tempting.
15. Complete acceptance criteria before marking a sprint done.

## Release Milestones

### Version 1 — Digital Library

Complete when LDAP auth, upload/download, incoming and network imports, hashing/deduplication, canonical Book/FileVariant linking, external metadata verification, manual review, search/browse, covers, permissions, and primary conversions work reliably.

Scanner integration, OCR, and ScanTailor-Web must not block this milestone.

### Full Project

Complete when a physical book can flow through:

`Scan job -> page capture -> ScanTailor-Web -> rescan loop -> cleaned pages -> OCR -> derived variants -> bibliographic resolution -> searchable library -> download/conversion`
