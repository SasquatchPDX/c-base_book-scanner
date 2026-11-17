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

Title and author alone contribute at most 0.85 to the score, author hints can
dilute it, and language/publisher comparisons in the score need normalization.
`BOOK_SCANNER_IDENTIFICATION_REVIEW` currently does not affect intake acceptance.
Task D must give that setting a tested role or remove it; do not lower auto-accept
merely to reduce the review queue.

### Auto-accept without an ISBN (exact title + strong author rule)

Many books predate ISBNs, and plain-text files rarely carry language or publisher
metadata, so a correct title+author match would otherwise always stop at 0.85 and
go to review. Instead of lowering the global threshold, a separate, conservative
rule lets such a match reach auto-accept. When the top candidate scores below
`BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT`, its score is raised to that threshold only
if **all** of the following hold:

- **Exact title:** some title hint has a title score of at least
  `BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_MIN_TITLE` (default 1.0, i.e. the same
  words after case/punctuation normalization). Subset titles ("Foundation" vs
  "Foundation and Earth") do not qualify.
- **Strong author:** some author hint scores at least
  `BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_MIN_AUTHOR` (default 0.9) against the
  candidate's authors under the normal author-score function. Each hint is scored on
  its own, because an "Author - Title" filename puts both halves into the author
  hints. The title and author must come from different hints. Improvements to
  author-name matching (such as name variants) apply to this rule automatically.
- **No conflicting signal:** if the file has an ISBN, the candidate must carry an
  ISBN and one must match (otherwise `conflicting_isbn` / `file_isbn_unconfirmed`).
  If the file has a language, it must be among the candidate's languages, after
  mapping common ISO 639-1/639-2 codes and names (`en` = `eng` = `english`). If the
  file has a publication year, it must not be more than
  `BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_YEAR_TOLERANCE` (default 2) years before
  the candidate's year. A later year is a reprint and is not a conflict. Missing
  values on either side are not conflicts.
- **No ambiguity:** the runner-up is more than
  `BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_TIE_MARGIN` (default 0.05) below the top
  candidate, and no other candidate also passes the rule. Two near-identical records
  (two works titled "Poems" by the same author, or the same book returned twice)
  therefore go to review.

`BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_ENABLED` (default true) turns the rule
off. Whenever the rule is evaluated, the decision is stored on the top candidate's
`raw_payload` under `_auto_accept_rule`. The record lists `rule`, `applied`,
`blocked_by` (the failed conditions), `base_score`, `runner_up_score`, the title and
author scores, and the hints that matched, so reviewers and audits can see why a
candidate was or was not auto-accepted. The rule runs inside `resolve_candidates`,
so it applies equally to upload/incoming/network intake, review re-searches and
scan-job identification.

Title similarity is the mean of rapidfuzz `token_set_ratio` and `token_sort_ratio`
over punctuation-normalized titles. `token_set_ratio` alone gives full marks when one
title's words are a subset of the other's ("Foundation" vs "Foundation and Earth"),
so the sort ratio is blended in to make unmatched words cost score. Only titles with
the same words reach a full title score.

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
