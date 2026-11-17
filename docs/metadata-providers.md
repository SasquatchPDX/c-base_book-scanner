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

Author names are compared per name, not by exact string. Case, punctuation,
spacing, and "Last, First" order are ignored, so "H P Lovecraft" equals
"H. P. Lovecraft". A name variant counts as a partial match
(`AUTHOR_VARIANT_MATCH_SCORE`, 0.9) when the surname is the same and the given
names line up in order. Given names line up when they are equal, when one is the
other's initial, or when they are close spellings, so a missing middle name or
initial is allowed ("Iain M. Banks" vs "Iain Banks"). A shared surname alone is
not a match ("Frank Herbert" vs "Brian Herbert").

Title similarity is the mean of rapidfuzz `token_set_ratio` and `token_sort_ratio`
over punctuation-normalized titles. `token_set_ratio` alone gives full marks when one
title's words are a subset of the other's ("Foundation" vs "Foundation and Earth"),
so the sort ratio is blended in to make unmatched words cost score. Only titles with
the same words reach a full title score.

### Author checks before auto-accept

A score at or above the auto-accept threshold is necessary but not sufficient. An
exact ISBN match alone reaches it, and an ISBN says nothing about who wrote the book,
so intake (`intake.decide_auto_accept`) also checks the top candidate's authors:

- **No provider authors** (`no_provider_authors`): the candidate names no authors,
  or only placeholders such as "Unknown" or "Multiple Authors". The import goes to
  review instead of creating, or attaching to, a Book whose authors nobody checked.
  The file's embedded authors are never written onto the Book automatically, also
  not when an existing Book matched by ISBN has no authors yet.
- **Author conflict** (`author_conflict`): both sides name authors and none of the
  candidate's authors is the same person as any of the file's embedded authors,
  using the name matching above. A bare surname on one side ("Tolkien") agrees with
  a full name that ends in it. Missing file authors are not a conflict.

Either case sets `LOW_CONFIDENCE`, opens a review task, keeps every candidate in
`REVIEW` state (none is shown as a `MATCH`), and records in the import's evidence:
`auto_accept_blocked` (the reason above), `auto_accept_block_details` (provider,
score, candidate and file authors) and `suggested_authors` (the file's embedded
authors without placeholder creators such as "Microsoft Office User", "Owner" or
"Administrator"). The review page explains the reason and pre-fills the suggested
authors. They are applied only when a reviewer accepts a candidate that has no
authors of its own, and only to a Book without authors; provider authors always win.

For Open Library ISBN lookups, an edition record without `authors` falls back to the
authors of its first work record (`works[0]`), where Open Library usually keeps them.

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
  search costs two requests. Every outbound request uses the shared per-provider
  limiter, including retries; the default rate is one request per second.
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

## Subject / genre evidence

Subject tags are captured as evidence only; they do not affect candidate scoring and
are not yet written to `Book.categories` / `Book.genres`.

- Embedded: EPUB `dc:subject` and CBZ `ComicInfo.xml` `Genre` (comma-separated) land
  in `ImportJob.evidence["subjects"]` (and therefore the review task's embedded
  metadata).
- Providers: `MetadataCandidate.subjects` holds tags from the provider record
  (Open Library search `subject`, edition `subjects`, Google Books-style
  `categories`). Open Library search requests `subject` explicitly via `fields`,
  because the default search field set omits it.
- Tags are trimmed, de-duplicated case-insensitively, limited to 25 per source and
  128 characters each (`Category.name` / `Genre.name` length), and otherwise kept as
  free text. Reviewers see candidate subjects on the review page.

Mapping these tags onto `Category`/`Genre` (which taxonomy, normalization, and
whether review gates it) is an open owner decision tracked in issue #50.

## Publication year

Providers report a publication *year*, never a reliable day: Open Library's
`first_publish_year` (the work's first publication, not necessarily the edition on
file, and sometimes simply wrong) or Hardcover's `release_year`. It is stored in
`Book.publication_year`. `Book.publication_date` is kept only for a genuinely known
full date (for example entered in the admin) and is otherwise empty, so the book
page shows "2003" instead of a fabricated "Jan. 1, 2003".

Migration `0020_book_publication_year` copies the year of every existing
`publication_date` into `publication_year` and clears dates on 1 January, because
every automated writer stored a year that way; other dates are kept. The book API
(`GET /library/books/<id>/`) gains `publication_year` (from the year field, or the
year of a full date) next to `publication_date`, which is now `null` for year-only
books. Clients that read the year from `publication_date` must switch to
`publication_year`.

## Local LLM identification assist (#26)

Design: `docs/design/26-local-llm-identification-signal.md`. **Off by default**
(`BOOK_SCANNER_LLM_ENABLED=false`). The MVP is text-only.

What it does: for an import that went to review with **zero candidates** or a top
score below `BOOK_SCANNER_IDENTIFICATION_REVIEW` (0.55), or when a reviewer presses
**Ask the AI server** on the review page, a `ProcessingJob` of type
`llm_identification` runs on its own RQ queue (`BOOK_SCANNER_LLM_QUEUE_NAME`,
default `llm`). It sends a bounded evidence bundle to a local OpenAI-compatible
`/v1/chat/completions` endpoint (Ollama, vLLM or llama.cpp's server) and asks for a
JSON object with `title_hints`, `author_hints`, `isbn_hints`, `language`,
`publication_year`, `is_single_book`, `evidence_refs`, `confidence` and a short
`rationale` (`core/llm.py`).

The bundle holds the path relative to the source root (at most 5 segments), the
filename, up to 20 sibling filenames from the same source folder's import records,
embedded metadata, `path_layout` hints, the titles and authors of up to 5 candidates
that did not match well, and the first `BOOK_SCANNER_LLM_TEXT_SAMPLE_CHARS` (4,000)
characters of the file's text. Text is read only for TXT, HTML, EPUB and FB2 through
the review reader's bounded readers; PDFs, MOBI and scans contribute path and
metadata only (OCR-then-LLM and vision are later phases, see below).

How the answer is used (`core/llm_identification.py`):

1. It is validated with Pydantic against a strict schema (no extra fields, at most 3
   hints per list, length caps, ISO 639-1 language, no control characters or
   markup). Invalid output fails the job as `llm_output_invalid`; the import stays in
   review exactly as before.
2. Grounding: an ISBN hint is **discarded** unless it is a valid ISBN whose
   characters appear verbatim (ignoring separators) in the evidence. Title and author
   hints that share no token with the evidence are kept for the search but flagged
   `grounded=false` for the reviewer.
3. The hints go first in a merged `IdentificationInput` (the file's own signals
   follow), the **existing providers** are queried with it, and the **existing
   scorer** ranks the results. New candidates (deduplicated by provider and ref) are
   added to the open review task with `raw_payload["_hint_source"] = "llm"`, always in
   `review` state.
4. `intake.decide_auto_accept` returns `BLOCK_LLM_DERIVED` (`llm_derived`) for any
   LLM-found candidate, even at a score of 1.0: these candidates **never
   auto-accept**. The review page explains the block, labels the candidates "Found
   with AI-suggested search terms (unverified)", and shows an "AI suggestion
   (unverified)" panel with the hints, grounding flags, discarded ISBNs, the model's
   own (uncalibrated) confidence, the rationale, and model/version. Nothing is
   pre-selected; "Use as search terms" copies the hints into the search fields only
   when the reviewer clicks it.
5. Each request stores an `LLMSuggestion`: trigger, requester, model, model version
   (`system_fingerprint`), `prompt_template_version`, the SHA-256 of the bundle (not
   the bundle), the validated output, the grounding report and the outcome
   (`hints_used`, `no_new_candidates`, `invalid_output`, `failed`).
   `ImportJob.evidence["llm"]` points at the latest one.

Safety properties: the output has no authority (it only becomes search terms and a
displayed suggestion; no tools are offered; nothing is accepted or written to a
Book), untrusted evidence is wrapped in `<untrusted_evidence>` with those delimiters
stripped from the evidence itself, and the service URL must be loopback, RFC 1918 or
ULA, or a host listed in `BOOK_SCANNER_LLM_ALLOWED_HOSTS`; otherwise the client
refuses to start and `manage.py check --deploy` reports `ebook_server.E101`. Logs
carry job id, model, prompt template version, input size and hash, duration, schema
validity, grounded/ungrounded hint counts and outcome, never the prompt, the
response or the text sample.

Budget: at most one request per import attempt plus one per reviewer click, one
attempt each (no automatic retries), `BOOK_SCANNER_LLM_DAILY_CAP` automatic requests
per source location per rolling 24 hours (uploads share one bucket; `0` turns the
automatic trigger off; reviewer requests are not counted), and
`BOOK_SCANNER_LLM_REQUEST_TIMEOUT`, `BOOK_SCANNER_LLM_MAX_TOKENS` and
`BOOK_SCANNER_LLM_JOB_TIMEOUT_SECONDS` per request. A source opts out with
`SourceLocation.default_metadata = {"llm_assist": false}` (for example a Calibre
library with good OPF data).

Metrics: `ebook_server_llm_requests{trigger,outcome}`,
`ebook_server_llm_requests_recent{outcome,window}`,
`ebook_server_llm_assisted_reviews{resolution}` (did reviewers accept an LLM-found
candidate, another one, enter metadata manually, or mark the file unidentified), the
`llm` queue depth in `ebook_server_queue` while the assist is enabled, and the usual
processing-job duration buckets for `job_type="llm_identification"`. The
`BookScannerLLMAssistFailing` alert fires when more than half of the last hour's
requests failed, for 30 minutes.

### Offline evaluation

`docs/design/data/llm-eval-labels.csv` holds 66 hand-labelled weak-name rows from
`tests/fixtures/real_world_samples/ebooks-share-filetree.csv` (title, author,
`is_single_book`, notes; an empty author means it was not verified). Add rows freely;
the design target is about 200. Run it against the real AI server by hand:

```bash
uv run python manage.py evaluate_llm_identification
uv run python manage.py evaluate_llm_identification --with-providers --json
```

It reports title and author hint precision and coverage, the share of ungrounded
hints, discarded ISBNs, chapter-detection precision and recall for `is_single_book`,
p50/p95 latency, and with `--with-providers` the provider hit rate and top-1/top-5
correctness of today's pipeline against the pipeline plus LLM hints. It runs in
path-only mode (the fixture has no content), which is the hardest case. The test
suite runs it against a fake client; CI never calls a model.

**Go/no-go before enabling the automatic trigger in a deployment:** a clear gain in
hit rate over the current pipeline, ungrounded hints that rarely become accepted
candidates, and p95 latency within budget. Record the numbers here. No run against
the c-base AI server has been recorded yet.

Not in the MVP: vision models and OCR-then-LLM for image-only PDFs (they depend on
what the AI server offers), grounded-ISBN auto-accept (a separate decision, behind a
setting), and tie-breaking help (#46).
