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

### Google Books API (secondary, opt-in)

- Provider: `ebook_server.core.metadata_providers.GoogleBooksMetadataProvider`
- Query style: `isbn:` lookup first, then `intitle:`/`inauthor:` search (5 results).
- Fit: useful for fallback/confirmatory evidence where Open Library misses coverage.
- Enabled only when listed in `BOOK_SCANNER_METADATA_PROVIDERS` **and**
  `BOOK_SCANNER_GOOGLE_BOOKS_API_KEY` is set; without a key it is skipped with a
  warning. The key travels in the query string, so errors and logs never include
  the request URL.
- Notes: operational terms and quotas need environment-specific validation before go-live.

### Deutsche Nationalbibliothek (optional, opt-in)

- Provider: `ebook_server.core.metadata_providers.DnbMetadataProvider`
- Public SRU interface (`services.dnb.de/sru/dnb`, `oai_dc` records, no key): `num=`
  ISBN lookup first, then `tit=`/`per=` search. Strong coverage of German-language
  books. Responses are parsed with `defusedxml` and capped at 2 MiB.
- Titles are cleaned of DNB's uniform-title prefix and statement of responsibility;
  creators marked `[Verfasser]`/`[Autor]` are preferred and reordered to "First Last".
- Enabled only when listed in `BOOK_SCANNER_METADATA_PROVIDERS`. Both extra providers
  use the shared per-provider rate limiter (`metadata_rate_limits`, keys `dnb` and
  `googlebooks`, default `metadata_rate_limit_default`).

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

## Search runs and provider errors (#40)

Every identification run (intake, review "Search providers again", scan
identification) is stored as a `MetadataSearch`: the terms actually sent, one
outcome per configured provider (`results`, `no_results` or `error` with a short
reason such as `timed out`, `HTTP 503`, `unreachable`, `rate limited (HTTP 429)`,
`malformed response`) and the candidates it returned. Providers signal failures
by raising (`identification.ProviderRequestError` or any `OSError`); returning an
empty list means a genuine miss. Reasons never contain URLs, queries or API keys.

Runs are non-destructive. A run that returns candidates marks the earlier
candidates `superseded_at` (kept as history, shown under "Earlier searches", not
acceptable, not counted in metrics or the review queue). A run that returns
nothing or fails leaves the earlier candidates current. A failure while saving
results is recorded as a failed run rather than an empty success.

The review form posts its title/author fields to the search action, so the
searched terms are always the edited ones and are saved on the review task
("Save terms only" saves without searching). A scan review's saved terms are
also written to the scan job's provisional hints and are never overwritten when
a later scan identification refreshes the task.

The review page shows a search as in progress only while its queued/running
`ProcessingJob` is live. A RUNNING search older than
`BOOK_SCANNER_METADATA_SEARCH_STALE_SECONDS` (default 300) or a QUEUED one older
than the generic job window is treated as dead: the page stops refreshing and the
next search fails it and starts a new one. New terms submitted while a search is
queued replace its terms; while one is running they are saved and the reviewer is
asked to search again once it finishes (HTTP 409 for API clients).

Searches are fenced so that an obsolete search can never publish. The import
job / scan job records its current search job in `current_search_job_id`, moved
under that row's lock whenever a new search replaces an older one (whose RQ
entry, enqueued with the id `metadata-search-<job id>`, is also cancelled, best
effort). A delivered queue entry only runs while its job is still QUEUED (or
RUNNING with a dead worker) and current; a FAILED, finished or superseded job is
ignored. When the providers return, the results become current only if, under
the task, source and job row locks, the job is still current, still the attempt
that started, and the review is still open (not resolved/cancelled, the import
not complete, the scan not linked to a book). Otherwise the run is stored as a
`discarded` ("Not applied") search whose candidates are history only: a review
resolved while the providers ran keeps its decision and is never reopened.

### ISBN lookup from the review page (#59)

"Look up by ISBN" (`POST /reviews/<id>/isbn-lookup/`, field `isbn`) queues the
same background search with ISBN-only signals: no title, author or folder
fallback, and the task's saved title/author terms are left alone. The value must
be one ISBN-10/13 with a valid check digit (spaces, hyphens and an `ISBN:` prefix
are ignored). The run is recorded with `query.mode = "isbn"`.

Reviewers usually judge an import from the actual ebook (the review page's
reader, #6), so the ISBN they type is normally read from the book itself (its
copyright page or metadata). It is still only a query (external verification is
mandatory): a candidate counts as *ISBN verified* only when the provider's own
record lists that ISBN, in either its 10- or 13-digit form (compared with
isbnlib). Only identifiers in the stored raw provider payload count; keys
starting with `_` are ours, so the `_queried_isbn` that Open Library's ISBN
lookup echoes into the payload is never evidence, and normalization no longer
copies it onto a record that does not list it.

- **Exactly one distinct book verified** (one provider, or several describing
  the same book): the review is resolved automatically by accepting the best such
  record through the normal accept path (`ReviewTask.Decision.ACCEPT_CANDIDATE`,
  `selected_candidate`, the record's metadata used fully), with
  `decision_payload.resolution_source = "reviewer_isbn"` and `decision_payload.isbn`;
  the reviewer who entered the ISBN is the task's reviewer. This happens only
  while the lookup is still the source's current search and the review is still
  unresolved (the same fencing as above), and only when the record names its
  authors; an authorless record is offered for one-click "Accept ISBN match"
  with an authors field instead (#42).
- **Several distinct books carry the ISBN:** nothing is accepted; the page shows
  the verified records for the reviewer to choose. Records are different books
  when their normalized titles differ, when both name authors and none of them is
  the same person (the scorer's name matching: "Dickinson, Emily" and "Emily
  Dickinson" agree, Emily Dickinson and William Blake do not, so two "Selected
  Poems" are two books), or when both list ISBNs and share none. A record only
  joins a book when it agrees with every record already in it.
- **Nothing verified, or providers failed:** the task stays open with its
  earlier candidates.

**Identifiers on the accepted Book.** A provider record can list several ISBNs
(other editions, print and ebook), and the candidate's normalized `isbn13` is the
record's first one. When a candidate is accepted, the ISBN that identifies the
edition is:

- the reviewer-entered ISBN, for an automatic acceptance; or
- the searched ISBN that the record itself lists (an ISBN lookup's query, or the
  file's ISBN used by an intake search), for any other accept of such a candidate.

That ISBN, in both forms (ISBN-13 and, for `978` ISBNs, ISBN-10), is used to
find an existing Book and is always written to the Book, including an existing
Book matched by title and author. The record's other ISBNs are neither looked up
nor written, so a multi-ISBN record never merges into a Book that only holds one
of its other ISBNs. Without a searched ISBN, the candidate's ISBN-13 is used,
together with its ISBN-10 only when that is the same ISBN (a record's first
ISBN-10 and first ISBN-13 can belong to different editions).

## Subject / genre evidence

Subject tags are captured as evidence; they do not affect candidate scoring.

- Embedded: EPUB `dc:subject` and CBZ `ComicInfo.xml` `Genre` (comma-separated) land
  in `ImportJob.evidence["subjects"]` (and therefore the review task's embedded
  metadata).
- Providers: `MetadataCandidate.subjects` holds tags from the provider record
  (Open Library search `subject`, edition `subjects`, Google Books-style
  `categories`). Open Library search requests `subject` explicitly via `fields`,
  because the default search field set omits it.
- Tags are trimmed, de-duplicated case-insensitively, limited to 25 per source and
  128 characters each, and otherwise kept as free text. The raw tags are never
  rewritten; mapping them to genres (below) is a separate step.

## Genres and categories (#50)

**Genre vs Category.** `Genre` is the literary genre facet (fiction and nonfiction:
Science fiction, Mystery, Biography & memoir, History, ...) and is the only target of
tag-derived classification. `Category` is library shelving (for example "Reference",
"Hackerspace manuals") and stays manual: only admins set it, in the Django admin.
Free-text subject tags never create or assign categories.

**Normalization.** A tag matches a genre when its key equals the key of a `Genre` name
or a `GenreAlias`. The key (`models.genre_key`) folds case, accents, `&`/"and",
punctuation and whitespace, so "SciFi", "Sci-Fi" and "sci fi" are the same key. On
top of the exact key the resolver only:

- drops trailing "General" segments ("Fiction / Science Fiction / General",
  "Fiction, science fiction, general");
- walks up BISAC-style `/` hierarchies one level at a time ("FICTION / Science
  Fiction / Space Opera" -> "FICTION / Science Fiction").

Comma-separated Open Library strings are not truncated further, because "Science
fiction, history and criticism" is not a science fiction book. There is no fuzzy
matching.

**Unknown tags are not auto-created.** A tag that maps to nothing stays evidence
only. Admins promote it by adding a `Genre`, or a `GenreAlias` on an existing genre,
in the Django admin (Genre change page, "Genre aliases" inline, or the Genre aliases
list). The admin rejects an alias that duplicates another alias or another genre's
name. Migration `0023_seed_genres` ships a small starter set (about 20 genres, with
common fiction spellings and BISAC/Open Library strings) that admins may edit or
delete. It reuses an equivalent genre or alias an admin already created instead of
adding a twin, and marks only the rows it creates (`seeded=True`). Reversing it
deletes only those seeded aliases and the seeded genres that have no books and no
remaining aliases; pre-existing admin taxonomy is never removed.

**Assignment and precedence.** When an import is linked to a Book through an
accepted candidate (auto-accept in intake, or a reviewer's "Accept"):

1. Reviewer choice wins. The review page shows, for each candidate, which tags map
   to which genres, with the suggested genres ticked. Reviewers can untick
   suggestions or tick other existing genres; the ticked set is saved with
   `source="reviewer"` (an empty set is a valid choice). API clients do the same with
   `genre_ids: [...]`; omitting `genre_ids` means "no choice" and step 2 applies.
2. Otherwise genres come from the accepted candidate's provider subjects
   (`source="provider"`), and only if none of those map to a genre, from the file's
   embedded subjects (`source="embedded"`).
3. Manual entry has no provider record, so only embedded subjects apply (or the
   reviewer's `genre_ids`). Merging into an existing book changes no genres.

Automatic assignment only adds links. It never removes a link, and it does nothing
for a Book whose genres are curated: a reviewer genre choice (including the empty
choice) or an admin edit of the genre links records `Book.genres_curated_at` and
`genres_curated_by`, and a Book with a reviewer- or admin-set link also counts as
curated. Curation is stored on the Book, so "no genres" stays "no genres" even
though it leaves no link rows. A reviewer's choice turns
matching automatic links into reviewer links but never changes admin-set ones, and it
does not remove genres the existing Book already has from other files.

**Provenance.** `Book.genres` uses the `BookGenre` through model (same
`core_book_genres` table) with `source` (`provider`, `embedded`, `reviewer`, `admin`),
`source_tag` (the tag that matched) and `created_at`. Links that existed before
migration `0022` and links added in the admin are `admin`. The review task's
`decision_payload` records `reviewer_genres` or `automatic_genres`.

**Browse.** `/library/books/?genre=...` matches a genre name or alias exactly
("sci-fi" finds Science fiction books); any other value is a substring match on genre
names, as before. Book pages list their genres as browse links.

**Backfill.** `python manage.py assign_genres` applies the automatic rules to
already imported books (curated books, including ones a reviewer left with no
genres, are skipped); re-run it after
adding genres or aliases. `--dry-run` reports without writing, and `--unmapped N`
lists the N most frequent tags that map to no genre, which are candidates for new
aliases.

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
