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

### Hardcover.app (optional, opt-in)

- Provider: `ebook_server.core.hardcover_provider.HardcoverMetadataProvider`
  (`provider_id = "hardcover"`).
- Access: free personal API token from <https://hardcover.app/account/api>. A token
  limited to a read-only catalog scope (`read:catalog`, or `read:catalog:search` plus
  `read:catalog:data`) is sufficient; do not use an `all`-scope token.
- Enable: add the provider to `BOOK_SCANNER_METADATA_PROVIDERS` **and** set
  `BOOK_SCANNER_HARDCOVER_API_TOKEN`. Listed without a token, the provider is skipped
  with a single `metadata_provider_unavailable` warning. Optional
  `BOOK_SCANNER_HARDCOVER_TIMEOUT_SECONDS` (default 10).
- Transport: `POST https://api.hardcover.app/v1/graphql` with
  `Authorization: Bearer <token>` and a JSON GraphQL body. The token is never logged or
  included in error messages.
- Queries (fields taken from the official `schema.graphql` in
  `hardcoverapp/hardcover-docs`):
  - ISBN: `editions(where: {isbn_13 | isbn_10 _eq})` with edition title, subtitle,
    ISBNs, release year, publisher, language (`code3`, falling back to `code2`), cover
    image, and the parent book's description and authors
    (`contributions` filtered to `contributor_role.slug = "author"`).
  - Title/author: `search(query_type: "Book")` returns ranked `ids`; a second
    `books(where: {id: {_in: ids}})` query fetches typed fields. The untyped
    `search.results` JSON is not parsed. Work-level search candidates carry no ISBN,
    publisher or language, because those belong to a specific edition.
- Limits (official docs): free plan 60 requests/minute, burst 10, 5,000/day; queries
  time out after 30 s (search after 2 s); responses carry `Retry-After` on 429. A title
  search costs two requests. Client-side pacing hooks in via `throttle_hook()` in the
  provider module (to be wired to the shared per-provider limiter from issue #10).
- Failures: HTTP 401/403/408/429/5xx, GraphQL `errors`, non-JSON, unexpected shapes and
  responses over 1 MiB are provider failures (`metadata_provider_failed`), not "no
  results". A 429 is retried once only if `Retry-After` is at most 5 seconds.
- Candidates go through the same scoring and review thresholds as Open Library; a
  Hardcover hit is never accepted on weaker evidence than any other provider.
- Terms: the Hardcover API is in beta and may change; tokens may be reset. The data
  must not be used to train publicly available or commercial LLMs. Cover images are
  user-uploaded; the provider only records their URL.

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
