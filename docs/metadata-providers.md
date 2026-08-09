# Sprint001 Metadata Provider Research

## Required abstraction

Public lookup must use an abstraction with:

- `search`
- `lookup_isbn`
- `normalize_result`

No single provider should be a hard dependency in application logic.

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
