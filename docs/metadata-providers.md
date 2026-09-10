# Sprint001 Metadata Provider Research

## Required abstraction

Public lookup must use an abstraction with:

- `search`
- `lookup_isbn`
- `normalize_result`

No single provider should be a hard dependency in application logic.

## Current implementation and correction scope

The interface above is the design target. The current protocol exposes `search`
with an ISBN keyword, and shared normalization still expects Open Library field
names. Sprint019 tasks B–D cover provider decoupling, scoring, and candidate
persistence; they remain open.

Digital intake requires `BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT` (default 0.86)
to link a provider candidate automatically. Lower scores and missing candidates
create a review task and archive the original without assigning canonical Book
metadata. The previous threshold correction is protected by upload and COPY/MOVE
regression tests.

The scorer is not yet tuned for ISBN-less imports: title and author alone
contribute at most 0.85, author hints can dilute the score, and language/publisher
comparisons need normalization. `BOOK_SCANNER_IDENTIFICATION_REVIEW` currently
does not affect intake acceptance. Task D must give that setting a tested role or
remove it; do not lower auto-accept merely to reduce the review queue.

## Provider options identified

### Open Library (primary)

- Query style: free HTTP API with ISBN and title/author search.
- Fit: good baseline coverage for common titles.
- Reliability: practical; acceptable for initial MVP with retries and backoff.
- Notes: strong for discovery and open catalogs; useful primary signal, not canonical truth.

### Google Books API (secondary)

- Query style: ISBN/title/author lookup and broader coverage in some domains.
- Fit: useful for fallback/confirmatory evidence where Open Library misses coverage.
- Notes: operational terms and quotas need environment-specific validation before go-live.

### WorldCat/other catalogs (later)

- Strong institutional matching in some geographies.
- Adds legal/commercial complexity and may require explicit credentials.
- Keep as optional future expansion once legal/ops review is complete.

## Evidence model

For each hit store:

- provider id
- raw payload fingerprint
- confidence inputs (ISBN match, title similarity, author overlap, language/edition)
- ranking signals
- provenance timestamp

Weak matches must route to review workflow instead of auto-linking.
