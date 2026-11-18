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
- **Strongest title evidence agrees:** the matched title hint must come from the
  strongest title source present (embedded > sidecar > header > folder layout >
  filename), or that stronger evidence must name the candidate's title too (same
  title, a "Title: Subtitle" form, or a " - " segment of it). An EPUB embedding "The
  Dragon in the Sea" uploaded as "Dune - Frank Herbert.epub" is not promoted to a Dune
  candidate (`stronger_title_evidence_disagrees`).
- **Strong author:** the typed author score (see above) is at least
  `BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_MIN_AUTHOR` (default 0.9). Filename
  guesses are alternatives, so an "Author - Title" filename costs nothing, but
  embedded/layout coauthors must all be covered: a candidate that omits a genuine
  coauthor (`missing_coauthor`, e.g. Good Omens credited to Terry Pratchett alone) or
  names nobody from an explicit author list (`author_evidence_conflict`, the #92
  guard's test) is not promoted. The title and author must come from different
  hints. Improvements to author-name matching (such as name variants) apply to this
  rule automatically.
- **No conflicting signal:** if the file has an ISBN, the candidate's own edition
  must carry an ISBN and one must match; work-level ISBNs of other editions do not
  count (otherwise `conflicting_isbn` / `file_isbn_unconfirmed`). If the file has a
  language, it must be among the edition's languages as normalised for #36 (`en` =
  `eng` = `english`; see "Edition checks before auto-accept"). A concrete edition
  that states no language or publisher while its work lists some is uncertain, so
  a file with that field is not promoted (`edition_language_unknown` /
  `edition_publisher_unknown`); the file may be another edition of the work. If the
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
author scores, the hints that matched, `title_source` / `strongest_title_source` and
`missing_coauthors`, so reviewers and audits can see why a
candidate was or was not auto-accepted. The rule runs inside `resolve_candidates`,
so it applies equally to upload/incoming/network intake, review re-searches and
scan-job identification.

The rule only raises a score; it does not bypass the other auto-accept checks. A
promoted candidate goes through intake's author, edition and title-evidence checks
(see "Author checks before auto-accept" below) like any other candidate at the
threshold. If its provider
record names no real authors, or its authors conflict with the file's embedded
authors, the import still goes to review with `auto_accept_blocked` set. Its
`_auto_accept_rule` record then says `applied: true`, and the import evidence says
why the accept was blocked anyway.

Author names are compared per name, not by exact string. Case, punctuation,
spacing, and "Last, First" order are ignored, so "H P Lovecraft" equals
"H. P. Lovecraft". A name variant counts as a partial match
(`AUTHOR_VARIANT_MATCH_SCORE`, 0.9) when the surname is the same and the given
names line up in order. Given names line up when they are equal, when one is the
other's initial, or when they are close spellings, so a missing middle name or
initial is allowed ("Iain M. Banks" vs "Iain Banks"). A shared surname alone is
not a match ("Frank Herbert" vs "Brian Herbert").

Author hints are grouped by provenance and meaning (`IdentificationInput.author_evidence`):

- **Alternatives** come from the filename. An ambiguous "Stephen King - Pet Sematary"
  yields both halves, and at most one is the author, so the group scores its best
  single match.
- **Coauthors** come from embedded metadata (every author-role `dc:creator`, with a
  joint "A & B" entry split into names) or a configured folder layout. Every name is a
  real author, so the group scores the share of names the candidate covers: a
  candidate naming only Terry Pratchett covers half of Good Omens' [Terry Pratchett,
  Neil Gaiman]. When the candidate matches a coauthor group at all, that coverage
  decides the author score, so a filename guess naming one coauthor cannot restore
  full credit. A coauthor group the candidate does not match at all is left to the
  author-conflict check below.

Title hints carry their source too (`title_evidence`), ranked embedded > sidecar >
header > folder layout > filename, for rules that must not let a filename guess
override stronger title evidence.

Filename "Last, First" detection ignores comma phrases that contain an edition,
volume, part or ordinal word ("Python Development, Second Edit", "Faust, Zweite
Auflage"), so such titles are never read as author names.

Title similarity is the mean of rapidfuzz `token_set_ratio` and `token_sort_ratio`
over punctuation-normalized titles. `token_set_ratio` alone gives full marks when one
title's words are a subset of the other's ("Foundation" vs "Foundation and Earth"),
so the sort ratio is blended in to make unmatched words cost score. Only titles with
the same words reach a full title score.

Language codes are normalized before comparison (`core/languages.py`, using
`pycountry`): ISO 639-1 (`en`, `de-DE`), ISO 639-2/B and 639-3 (`eng`, `ger`,
`deu`), Open Library keys (`/languages/ger`) and English names all map to the
ISO 639-1 code, or the 639-3 code when no two-letter code exists. `UND`, `mul`,
`zxx`, `mis` and unrecognized values mean "no language signal", never a mismatch.
Normalization is for comparison only; the Book keeps the provider's code form.
Publisher comparison ignores case, punctuation and noise words such as "Books",
"Press", "Ltd", "Verlag", and then needs a rapidfuzz `token_set_ratio` of at least 90.

The file's publisher passes through `identification.publisher_signal()` first (for
single files in `build_identification_input` and for multi-part groups in
`multipart._group_signals`). Values that name no publisher become "no publisher
signal" (no bonus, no edition conflict):

- converters, authoring tools and placeholders from the built-in
  `DEFAULT_IGNORED_PUBLISHERS` list (calibre, Adobe Acrobat/InDesign/Distiller/PDF
  Library, Microsoft Word, LibreOffice/OpenOffice, Pages, Sigil, Pandoc, ABBYY
  FineReader, Kindle Previewer/KindleGen, ePub Bud, "Unknown", "Unknown Publisher",
  "n/a", "[s.n.]", ...), also when followed only by version tokens
  (`Calibre 5.42.0`, `Adobe InDesign CS6`, `calibre (3.48.0) [https://calibre-ebook.com]`);
- a bare web address (`https://...`, `www....`).

A web address next to a real name is removed and the name kept (`Tor.com
(www.tor.com)` becomes `tor.com`). Names that merely contain a domain (`Tor.com`,
`Tor.com Publishing`, `Amazon.com Services LLC`, `Smashwords.com`, `Books.de`) are
real imprints and stay a publisher signal, so a Tor.com file still reveals a Gollancz
edition as `edition_publisher_mismatch`. `BOOK_SCANNER_IDENTIFICATION_IGNORED_PUBLISHERS`
(a JSON list in the environment, default `[]`) adds deployment-specific entries to the
built-in list; entries are compared case-insensitively after whitespace normalization.

### Work versus edition

An Open Library search hit is a *work*; its `isbn`, `publisher` and `language`
lists cover every edition. Search requests ask for these lists and for
`editions` (the one edition that best matches the query) through `fields=`.
A Book record stands for the edition it was matched to:

- the candidate's ISBN, publisher and language come from that edition, or from
  the edition record of an `/isbn/` lookup. When Open Library lists several
  editions, the one carrying the file's ISBN wins, then one agreeing with the
  file's language and publisher. Only edition ISBNs become `Identifier` rows; the
  work-level lists are comparison evidence only;
- a file ISBN equal to one of the edition's ISBNs (ISBN-10 or -13) adds 0.95;
- a file ISBN found only among the work's other editions proves the work, not
  the edition, and adds `BOOK_SCANNER_IDENTIFICATION_WORK_ISBN_BONUS` (default
  0.3);
- the language and publisher bonuses compare with the edition; the work's lists
  count only for a candidate that names no edition. A concrete edition (an
  edition record, a single-edition provider record, or the edition selected from
  a search hit) that lacks a language or publisher has that field *unknown*: it
  earns no bonus, raises no conflict, and is never filled from the work's lists
  (a French or Gollancz file does not match a sparse Ace edition through them).

The ISBN path (`/isbn/<isbn>.json`) runs first whenever the file has an ISBN, so
in practice the work-level comparison matters only when that lookup misses.
`publication_year` is still the work's `first_publish_year` (see "Publication year").

### Edition checks before auto-accept

Scores identify the work; accepting a candidate stores its *edition* on the Book.
`identification.edition_conflicts(candidate, signals)` therefore compares the
normalised file evidence with the candidate's edition, and `intake.decide_auto_accept`
(which receives the file signals as `file_evidence`) routes any conflict to review as
`edition_conflict`, with the reasons and both sides' values in
`auto_accept_block_details`:

- `edition_isbn_mismatch`: the file has an ISBN that is not one of the edition's
  (for example an ISBN of another edition of the same work);
- `edition_isbn_unconfirmed`: the file has an ISBN and the candidate none;
- `edition_language_mismatch`: the file's language is not the edition's (or the
  work's, when no edition language is known);
- `edition_publisher_mismatch`: the file's publisher does not match the edition's,
  and no file ISBN confirms the edition (with a confirmed ISBN a differing publisher
  is usually an imprint or distributor and is tolerated).

Missing values on either side are no conflict. Among equally scored candidates, one
without edition conflicts ranks first, so an `/isbn/` edition record wins a tie with
a search hit for another edition.

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
- **Stronger title evidence disagrees** (`stronger_title_evidence_disagrees`): the
  same title-provenance check as the strong-match rule above, applied to every
  candidate at or above the threshold however its score got there. A Dune candidate
  matching only "Dune - Frank Herbert.epub" while the EPUB embeds "The Dragon in the
  Sea" is held for review even when title, author and a shared language already
  score 0.90 without the rule. The details record the candidate title, the matched
  hint's source and the strongest titles.

Any of these cases sets `LOW_CONFIDENCE`, opens a review task, keeps every candidate in
`REVIEW` state (none is shown as a `MATCH`), and records in the import's evidence:
`auto_accept_blocked` (the reason above), `auto_accept_block_details` (provider,
score, candidate and file authors) and `suggested_authors` (the file's embedded
authors without placeholder creators such as "Microsoft Office User", "Owner" or
"Administrator"). The review page explains the reason and pre-fills the suggested
authors. They are applied only when a reviewer accepts a candidate that has no
authors of its own, and only to a Book without authors; provider authors always win.

**Local consensus suggestion.** A file that no provider record fits still goes to review. Local
consensus (`local_identity.py`, `BOOK_SCANNER_IDENTIFICATION_LOCAL_CONSENSUS`, on by default)
only makes that review faster. If at least two *independent* origins name the same title and a
plausible author is known, the import's evidence records `local_identity`, and the review page
shows it as a pre-filled suggestion. The origins are:
- the path: file name, folder layout and Calibre sidecar, which count as one witness;
- the text header, but only a title it states on its own, such as a `Title:` label or a Project
  Gutenberg header. A header line that merely repeats the file name doesn't count;
- the embedded metadata.

When a provider record names the same title and author, the suggestion confirms that record,
and the reviewer accepts it in one click. Its score is unchanged. Otherwise the manual-metadata
form is pre-filled. Local consensus never accepts a book. It gives no suggestion when a provider
names the title by a different author, and genre folders ("Science Fiction", "Short Stories")
never count as authors. Providers that failed during the search are noted on the suggestion.

**Ties.** Candidates at or above the threshold whose score is within
`BOOK_SCANNER_IDENTIFICATION_TIE_MARGIN` (default 0.02) of the best one count as
tied (the comparison allows for floating-point rounding, so 1.0 and 0.98 tie). With
several providers an ISBN match often scores 1.0 at each of them, and their order is
only the configured provider order. Intake therefore applies the author checks *and*
the edition checks (see "Edition checks before auto-accept"; `decide_auto_accept`
receives the normalised file ISBNs, language and publisher as `file_evidence`) to every
tied candidate and accepts the first one that passes, recording `auto_accept_tie_break`
(the chosen candidate and the ones passed over, with their block reasons and edition
conflicts) in the evidence. A tied fallback whose ISBN contradicts the file's, or that
has no ISBN when the file has one, is never eligible. Passed-over candidates stay in
`REVIEW` state. If tied candidates that pass describe different books, the import
goes to review as `ambiguous_tie`. Two records are the same book when they share an
ISBN; otherwise they need the same title and agreeing authors, and neither disjoint
ISBN lists (two editions) nor disjoint languages. If none passes, the first-ranked
candidate's block reason applies. A candidate outside the margin is never accepted in
place of a blocked better one.

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
refuses to start and `manage.py check --deploy` reports `ebook_server.E101`. The
HTTP client ignores ambient proxy settings (`HTTP(S)_PROXY`, `ALL_PROXY`; httpx
`trust_env=False`), re-resolves the host on every request, refuses the request if any
address is no longer local (unless the host is allowlisted), and connects to the
address it just checked with the configured name kept in the `Host` header and the
TLS server name. Responses are requested uncompressed and read as a stream; reading
stops at 256 KiB. Logs
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
