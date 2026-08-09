# Sprint005 — Bibliographic Identification

## Goal

Identify imported books using all available evidence plus mandatory public bibliographic lookup rather than trusting raw metadata.

## Tasks

- Implement metadata-provider abstraction from Sprint001.
- Integrate initial provider(s).
- Implement ISBN normalization/verification.
- Parse and normalize embedded metadata.
- Implement filename/folder heuristics, including common patterns:
  - `Book Title.ext`
  - `Author - Book Title.ext`
  - `Book Title - Author.ext`
  - underscore/dash variants
  - author inferred from parent folder
- Store raw evidence separately from canonical Book metadata.
- Build `IdentificationInput` representation.
- Implement candidate search/ranking.
- Implement configurable confidence scoring using ISBN, title, author, language, edition/publication signals as available.
- Store `MetadataCandidate` records with provider provenance and score.
- Auto-accept only high-confidence matches.
- Route uncertain/no-match cases into review state for Sprint006.

## Tests

- Exact ISBN match.
- Good title/author match without embedded ISBN.
- Author derived from folder name.
- Title/author split from filename.
- Conflicting embedded metadata.
- No-match result.
- Weak candidate does not silently overwrite canonical metadata.

## Acceptance Criteria

An imported file with incomplete or unreliable metadata is queried against external bibliographic data and either receives a high-confidence canonical Book identity or is explicitly marked for human review.
