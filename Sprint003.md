# Sprint003 — Canonical Book and File Model

## Goal

Implement the persistent domain model that separates bibliographic Books from physical/digital FileVariants.

## Tasks

Implement migrations/models for:

- `Book`
- `Author`
- `Identifier`
- `FileVariant`
- `CoverImage`
- `Category`
- `Genre`
- `StorageLocation`
- `SourceLocation`
- `ProcessingJob` base state as needed

Book fields should cover title, subtitle, publisher, publication date, edition, language, description, authors, identifiers, categories/genres, and cover relationships.

FileVariant fields should include format, MIME type, original filename, storage identifier, size, SHA-256, source, uploader, timestamps, parent/source variant, provenance, and processing state.

- Support many FileVariants per Book.
- Support multiple identifiers per Book; do not require ISBN.
- Add front/back cover types.
- Implement basic admin management UI.
- Add a storage service interface/stub used by FileVariant logic rather than raw filesystem paths.
- Define normalized enums/constants for supported formats and source types.

## Tests

- A Book can exist without ISBN.
- Multiple authors can attach to one Book and vice versa.
- Multiple FileVariants can attach to one Book.
- Derived FileVariant can reference its source variant.
- Cover front/back types are enforced.
- FileVariant business logic does not require direct absolute paths.

## Acceptance Criteria

An admin can manually create one Book, attach bibliographic metadata, add front/back covers, and register multiple distinct file variants under the same Book.

## Out of Scope

- Automated upload.
- Hashing/deduplication implementation.
- External metadata lookup.
