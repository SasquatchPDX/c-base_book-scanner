# Sprint001 — Architecture and Research

## Goal

Establish the technical foundation before feature implementation. Resolve framework/tool choices, external dependencies, interfaces, and the major unknowns around conversion, metadata providers, scanner communication, and ScanTailor algorithms.

## Deliverables

- Initial repository skeleton.
- `docs/architecture.md`.
- `docs/storage-model.md`.
- `docs/conversion-matrix.md`.
- `docs/metadata-providers.md`.
- `docs/scanner-protocol.md` draft.
- `docs/scantailor-web.md` algorithm/stage survey.
- Framework decision and rationale.
- Background-job system decision and rationale.
- Development/test environment that boots successfully.

## Tasks

### Architecture

- Compare Django + DRF with FastAPI for this application.
- Prefer maintainability, permissions/admin tooling, migrations, background jobs, and server-rendered library pages over fashionable architecture.
- Select PostgreSQL.
- Evaluate Celery, Dramatiq, and RQ.
- Define a modular-monolith boundary map.
- Document external services: LDAP, network storage, public metadata APIs, OCR AI server, physical scanner.

### Conversion Research

Survey open-source support for at least:

- EPUB -> MOBI
- MOBI -> EPUB
- EPUB -> PDF
- PDF -> EPUB
- HTML -> EPUB
- TXT -> EPUB
- FB2 conversions
- DjVu conversion/extraction
- CBZ/CBR extraction/conversion
- scanned images -> PDF
- OCR output -> EPUB/PDF

Investigate Calibre CLI/components, Pandoc, ebooklib, PyMuPDF, Poppler, Ghostscript, DjVuLibre, libarchive/7zip, and other credible tools.

Create a conversion matrix with:

- source
- target
- preferred tool
- fallback tool
- quality expectation
- lossy flag
- OCR requirement
- implementation status
- notes

### Metadata Provider Research

Research public bibliographic APIs supporting ISBN/title/author queries.

Document:

- coverage
- licensing/terms
- rate limits
- authentication requirements
- available fields
- reliability

Design a provider abstraction supporting `search`, `lookup_isbn`, and normalized output.

### Scanner Protocol Draft

Define the first version of the scanner/server contract:

- server-generated job IDs
- job listing
- job creation/selection
- page upload
- page sequence
- checksums
- idempotency
- scanner metadata
- rescan to-do retrieval
- replacement-page semantics

Do not build hardware-specific UI yet.

### ScanTailor Research

Map these stages to modern algorithm/library options:

- Page Split
- Deskew
- Select Content
- Margins
- Dewarping
- Output/Binarization

For each, document inputs, outputs, tunable parameters, likely libraries, computational cost, and known algorithmic approaches.

## Acceptance Criteria

- A new developer can read the architecture documents and understand the intended system boundaries.
- The chosen backend and worker stack are justified in writing.
- Conversion research identifies at least one credible implementation path for the common format pairs.
- At least two metadata-provider options are documented or a clear reason is given if fewer viable choices exist.
- The scanner API draft is concrete enough to implement without inventing protocol details during coding.
- ScanTailor-Web stages have a research-backed implementation direction.
- The base project boots and its test command runs successfully.

## Out of Scope

- Full LDAP implementation.
- Real book ingestion.
- Conversion implementation.
- Scanner hardware implementation.
- OCR processing.
- ScanTailor image-processing implementation.
