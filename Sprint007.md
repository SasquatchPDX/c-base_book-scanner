# Sprint007 — Automated Filesystem Sources

## Goal

Add unattended ingestion from the configured `incoming` folder and administrator-registered network paths using the same intake pipeline as uploads.

## Tasks

### Incoming Folder

- Poll or watch the configured incoming folder using a robust approach suitable for network filesystems.
- Discover supported files.
- Create ImportJobs.
- Run normal hash/metadata/identification/archive workflow.
- Remove source only after successful destination verification.
- Leave failed items recoverable and visible.

### Registered Source Paths

Implement admin-managed source configuration:

- path
- enabled flag
- COPY or MOVE mode
- recursive flag
- optional default/provenance metadata

Implement:

- path validation
- dry-run/preview
- import execution
- progress/job reporting
- retry behavior

MOVE mode must delete source only after destination integrity is verified.

## Tests

- Incoming folder happy path.
- Failed import does not disappear.
- COPY retains source.
- MOVE removes source only after verified archive write.
- Recursive import.
- Duplicate files across multiple source paths.

## Acceptance Criteria

An administrator can point the application at an existing network directory, preview the import, choose COPY or MOVE behavior, and import a mixed set of supported books without manual upload.
