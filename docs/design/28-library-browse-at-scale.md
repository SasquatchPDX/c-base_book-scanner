# Design 28: library browse and search for 100,000+ books

- **Issue:** [#28](https://github.com/SasquatchPDX/c-base_book-scanner/issues/28)
- **Status:** Proposed. Waiting for the owner to confirm the decisions in
  [Decisions to confirm](#decisions-to-confirm).
- **Related:** #50 (genre/category never captured), #35 (series hints are
  evidence only), #6 (in-browser reader), #7 (storage on the application VM).

## 1. Problem

`/library/books/` (`views.library_books`) is an offset-paginated list with
case-insensitive `icontains` filters joined across `Book`, `Author`,
`Category`, `Genre` and `FileVariant`, plus `.distinct()` and Django's
`Paginator`. `/library/` shows the 12 newest books and two `count()`s. That
works for tens of books. The target is well over 100,000. A flat list of
everything is not a usable way to browse a collection that size, and the
current query shape does not scale (measured below).

## 2. Measurements on 150,000 synthetic books

**Setup.**

- A scratch script (not committed) loaded data straight into a PostgreSQL 17.10
  container with the current migrations applied:
  - 150,000 `Book` rows with titles drawn from the vocabulary of
    `real_world_samples/ebooks-share-filetree.csv`;
  - 37,500 authors and 187,373 book-author links;
  - 220,214 `FileVariant` rows across 6 formats;
  - 120,147 front covers (80%);
  - 40 categories and 60 genres;
  - languages in a 60/30/10 split of en, de and other.
- The database was 325 MB.
- The machine was a Windows 11 developer laptop, with Docker Desktop (WSL2) and
  the stock PostgreSQL configuration: `shared_buffers=128MB`, `work_mem=4MB`.
- Absolute numbers are indicative only. The *ratios* and the plan shapes are
  the point.
- "View" times are Django test-client requests to the current view
  (median of 3, JSON response). SQL times are `EXPLAIN ANALYZE` execution
  times.

### 2.1 Current implementation

| Request | View time | Queries | Slowest query |
|---|---:|---:|---:|
| `/library/books/` (newest, page 1) | 1,299 ms | 35 | 614 ms |
| `?page=4000` (deep offset) | 1,619 ms | 35 | 947 ms |
| `?sort=title` | 1,343 ms | 35 | 457 ms |
| `?q=java` | 1,405 ms | 35 | 589 ms |
| `?q=zzzznomatch` | 565 ms | 4 | 535 ms |
| `?author=Ursula` | 510 ms | 35 | 275 ms |
| `?language=de&format=epub` | 610 ms | 35 | 242 ms |
| `?genre=Genre 7&language=en` | 242 ms | 35 | 51 ms |
| `/library/` (home page, HTML) | 567 ms | 44 | 257 ms |

Three defects explain most of this. They are worth fixing now, independent of
the redesign (phase 0):

1. **N+1 on `formats_available`.** `_serialize_book` calls
   `book.file_variants.filter(processing_state=COMPLETE)`. The `.filter()`
   bypasses the `prefetch_related("file_variants")`, so every book on the page
   costs one more query: 26 of the 35 queries.
2. **`SELECT DISTINCT` over every column, including `description`, for both
   the page and the `COUNT(*)`.** The plan sorts 150,000 wide rows to disk
   ("external merge Disk: ~47 MB" across workers) just to remove duplicates
   that only the joins introduced. The count query alone took 387 ms.
3. **No index supports the sort.** `ORDER BY created_at DESC` was a parallel
   sequential scan plus a sort. A deep `OFFSET` must still produce and throw
   away every earlier row.

`icontains` on title, description and author name cannot use a B-tree index.
Every free-text search is a sequential scan over all books and their joins.

### 2.2 Proposed approaches, same data

The setup adds a denormalized `book_search` table: one row per book with a
weighted `tsvector`, a lowercased title, author text, and arrays of format,
author, genre and category ids. It also adds `pg_trgm` and GIN indexes and a
`(created_at DESC, id DESC)` B-tree index. The table plus its indexes is
**193 MB**, and the one-time build took about 19 s.

| Query | Time |
|---|---:|
| Keyset "newest", page 1 (`ORDER BY created_at DESC, id DESC LIMIT 25`) | **0.9 ms** |
| Keyset "newest", the row-100,000 position (`WHERE (created_at,id) < (…)`) | **0.3 ms** |
| Offset at the same position, even with the index | 217 ms |
| Exact `count(*)` of all books | 49 ms |
| Planner estimate (`pg_class.reltuples`) | 0.07 ms |
| FTS `q=java`: top 25 by `ts_rank` (1,718 hits) | 16 ms |
| FTS `q=java`: exact hit count | 3.5 ms |
| FTS `q=java`: language facet counts / format facet counts | 3.3 ms / 7.1 ms |
| Trigram substring `title LIKE '%java%'` | 0.5 ms |
| Trigram author `ILIKE '%ursula%'` | 2.7 ms |
| Trigram fuzzy title similarity (`%` operator) | 21 ms |
| Filter language=de, format=epub and genre, keyset newest | 31 ms |
| Genre facet counts within language=de and epub (19,941 rows) | 173 ms |
| *Global* facet counts with no filter (language / format / genre) | 33 / 235 / 233 ms |

Meilisearch v1.12 was loaded with the same 150,000 documents for comparison
(77 s to index, about 700 MB RSS):

| Meilisearch query | `processingTimeMs` |
|---|---:|
| `q=java` + facets (language, formats, genres) | 13 ms |
| Empty query, newest first, with facets (browse everything) | 10 ms |
| Filter de and epub, genre facets | 8 ms |
| Typo `anatomei` | 1 ms (matches "Anatomie"). PostgreSQL FTS: 0 hits. Trigram similarity finds it. |
| Deep page, `offset=99975` | 220 ms |

**Reading the numbers.**

- The current view is slow mostly because of fixable query-shape bugs, not
  because PostgreSQL is too slow.
- With a denormalized search row, PostgreSQL answers every interactive query in
  under 35 ms. The exception is facet counts over very large result sets
  (more than 20,000 rows, about 170 to 235 ms). Those are exactly the counts
  that can be cached, because they barely change between imports.
- Meilisearch is faster on large facet counts and has real typo tolerance. It
  costs a second datastore, a sync pipeline and about 700 MB of RAM.

## 3. Answers to the open questions

### Q1. What does "browse" mean at this scale? Search-first or browse-first?

**Recommendation: search-first with entry points for browsing.** `/library/`
becomes the landing page. It has these parts:

1. **A prominent search box** with free text over title, subtitle, authors,
   series and description, plus field prefixes (`author:`, `isbn:`).
2. **A facet sidebar** showing *global* counts: language, format, genre,
   category, and decade of publication. One click goes to a filtered result
   list.
3. **Shelves** ("Recently added", "Recently identified from review", and later
   curated or personal shelves). Each is a horizontal row of about 12 covers
   with a "see all" link to the filtered list.
4. **Browse indexes:** authors A to Z, series A to Z, and later subjects. These
   are *lists of entities*, each linking to a filtered book list. They are not
   a list of every book.

"All books" stays available as the result list with no query, sorted by
newest. It is something a member chooses, not the landing view.

Alternatives considered:

| Option | Verdict |
|---|---|
| Browse-first grid of everything (the status quo) | Unusable at 100k: page 4,000 of 6,000 has no meaning to a person. |
| Search-only, Google style | Members often don't know what to search for. "What's new" and "what's in German" are real browsing needs, and facets and shelves serve them. |
| **Search-first plus facets, shelves and entity indexes (recommended)** | Covers both "I know what I want" and "show me what's here". |

### Q2. Faceted filtering: PostgreSQL full text and trigram, or an external engine?

**Recommendation: PostgreSQL first, behind a `SearchBackend` interface.
Meilisearch is the documented upgrade path. OpenSearch is not recommended at
this scale.**

| | PostgreSQL FTS + `pg_trgm` | Meilisearch | OpenSearch / Elasticsearch |
|---|---|---|---|
| Ops cost | None: already the system of record | One more service and volume, plus a sync worker | A JVM cluster with heap tuning. Heavy for a hackerspace VM. |
| Consistency | Transactional with `Book` (same DB) | Eventually consistent. Needs reindex, retry and backfill. | Same as Meilisearch, plus a mapping-migration burden |
| Facet counts | `GROUP BY`/`unnest`. Fast on filtered sets, about 0.2 s for global counts at 150k, so cache those. | Native, about 10 ms | Native aggregations |
| Typos | No typo tolerance in FTS. Trigram similarity is a usable fallback ("did you mean"). | Built in | Fuzzy queries |
| Multilingual stemming | Per-language `regconfig` (German, English, …). Mixed-language books need the `simple` config plus trigram. | Language detection with tokenizers. Good for de and en. | Analyzers, very flexible |
| Permissions and filtering | Plain SQL. Reuses Django permission logic. | Filter expressions. Keep permission data in the index. | Same |
| Licence | PostgreSQL licence | MIT (Community Edition) | Apache-2.0 (OpenSearch) |
| Measured here | Under 35 ms for interactive queries; 0.2 s for uncached global facets | 8 to 13 ms including facets | not measured |

Why PostgreSQL first:

- It meets the latency budget with no new infrastructure.
- It keeps "Database identifiers are authoritative" literally true.
- It avoids a second consistency problem while intake and review are still
  changing.

The `SearchBackend` protocol has these methods:

- `search(query, filters, sort, cursor, limit) -> ResultPage`
- `facet_counts(query, filters, facets)`
- `index_book(book_id)`
- `delete_book(book_id)`
- `rebuild()`

The protocol makes a later switch a contained change. **Revisit triggers for
Meilisearch:** members report typo-related zero results often, uncached facet
p95 stays above 300 ms, or the collection grows past about 1 million variants.

### Q3. Query performance: indexing, pagination and denormalization

- **A denormalized `BookSearchDocument` row per Book**, maintained in the
  background:
  - an enqueue on `post_save`/`m2m_changed` of `Book`, `Author` links, genres,
    categories and `FileVariant` state;
  - a nightly full rebuild via `manage.py rebuild_search_index`.

  This puts the join and DISTINCT work on the write side, where 1 write per
  import is cheap, instead of the read side, with 25 joins per page view.
- **Pagination:**
  - **Keyset (cursor) pagination for every browse sort:** newest, oldest,
    title A to Z and author A to Z. The cursor is an opaque, signed
    `(sort_key, id)` token, and "next" and "previous" links work at any depth
    in about 1 ms.
  - **Ranked search results use offset, capped at 1,000 results (40 pages).**
    Relevance ranks are not stable keys, and nobody pages past result 1,000.
    Members refine the query instead.
  - The JSON API (`?page=`) keeps working for one release with a
    `Deprecation` header, then moves to `?cursor=`.
- **Counts:**
  - exact for filtered searches (3 to 7 ms with GIN);
  - for the unfiltered "all books" total, use `reltuples` or a cached exact
    count refreshed by the index job;
  - global facet counts cached (Redis or the Django cache) and invalidated or
    refreshed by the indexing job, with at most 5 minutes of staleness.
- **Indexes:**
  - GIN on the `tsvector`;
  - GIN `gin_trgm_ops` on the normalized title and author text;
  - GIN on the `formats`, `genre_ids`, `category_ids` and `author_ids` arrays;
  - B-tree on `(created_at DESC, book_id DESC)`, `(title_sort, book_id)` and
    `language`.

  Add these with `django.contrib.postgres` operations (`TrigramExtension`,
  `GinIndex`, `SearchVectorField`). This requires adding
  `django.contrib.postgres` to `INSTALLED_APPS`.
- **Text configuration.** The `simple` config with `unaccent` is the default,
  so that German and English titles both match literally. Stemming per
  `Book.language` is an option once #36 has normalized language codes. Weights:
  A for title and subtitle, B for authors and series, C for
  subjects/genres/categories, D for description.
- **SQLite.** The development default (`sqlite:///db.sqlite3`) has no FTS or
  trigram support. The `SearchBackend` gets a simple `icontains` fallback for
  SQLite, so local development and the unit tests keep working. PostgreSQL
  behaviour is covered by the existing infrastructure job
  (`scripts/run_infrastructure_tests.sh`).

### Q4. The default sort and landing view

**Recommendation:**

- The landing page (Q1) shows the "Recently added" shelf first.
- An empty search result list defaults to **newest first**.
- A search with a query defaults to **relevance**.
- Title A to Z and author A to Z are explicit choices.
- "Needs review" does *not* belong in the library browse. It is the review
  queue's job, and mixing unidentified imports into the library view would hide
  the line between identified and unidentified.

"Recently added" means *Book created*, not *FileVariant added*. Adding a fourth
format of an old book should not push it to the top. A secondary "new formats"
shelf can come later.

### Q5. Rendering cost: covers, lazy loading, virtualization

- **Thumbnails as derived artifacts.** A background job (on cover extraction
  and as a backfill command) creates a 240 px wide WebP thumbnail with Pillow,
  which is already used for covers and ScanTailor. It is stored through the
  storage abstraction under a cache `StorageLocation` on the application VM.
  It is recorded with `source` = the `CoverImage`, plus tool and version. The
  original covers stay untouched ("Preserve originals").
- **Cache-friendly URLs.** `/library/covers/<sha256>/thumb.webp` is
  content-addressed, so it is served with
  `Cache-Control: private, max-age=31536000, immutable`. Today covers go
  through Django with `max-age=3600` and full size, and each one occupies a
  Python worker. The view checks permission and then hands the file to nginx
  with `X-Accel-Redirect` (`deploy/nginx/book-scanner.conf` needs an
  `internal` location). This is "CDN-ish" without a CDN, and the cache stays
  `private` because the library sits behind login.
- **Page composition.**
  - 24 to 48 results per page;
  - `<img loading="lazy" decoding="async" width height>` with a fixed aspect
    ratio and a CSS placeholder (title initials), so the layout does not shift;
  - shelves render 12 covers.

  With cursor pagination the DOM stays small, so **no virtualization library**
  is needed. Infinite scroll is optional later, as progressive enhancement on
  the same cursor API.
- **Template fields.** List templates render title, authors, year, format
  badges and the thumbnail. They do not render the description. The current
  serializer sends the full description for every list item.

### Facets requested in the issue and AGENTS.md

| Facet | Status today | Needed |
|---|---|---|
| Author | M2M `Author`, `icontains` | Author index page with keyset A to Z plus `author_ids` facet. Author search through trigram. |
| Series | Not modelled. #35 records series only as evidence. | New `Series` model and `Book.series`/`series_index` (phase 3). Providers: Hardcover has series data, Open Library's is patchy. |
| Genre / Category | Models exist, but **nothing populates them** (#50) | Facets are built now and become useful once #50 decides the taxonomy. Hide facets with no values. |
| Language | Free text, codes not normalized (#36) | Normalize to ISO 639-1 in the search document. Display names via a small mapping. |
| Format | From complete `FileVariant`s | `formats` array in the search document. Also "convertible to" later, from the conversion graph. |
| Publication decade | `publication_year` (#38) | Derived integer in the search document. |
| Has cover / has scan | Derivable | Later. Useful for cleanup campaigns (#27). |

### Shelves

- **Phase 2:** system shelves defined in code: Recently added, Recently
  identified, and "New in <language>".
- **Phase 4:** a `Shelf` model with `owner` (null means public), `title`,
  `visibility` (private, members, public), and an ordered `ShelfItem(book,
  position)`. Personal "to read" lists are private by default. Public curated
  shelves ("Hackerspace classics", "Electronics") can be edited by admins and
  the shelf owner.

## 4. Data model changes (sketch)

```python
# INSTALLED_APPS += ["django.contrib.postgres"]

class BookSearchDocument(models.Model):
    """Denormalised, rebuildable search/facet row. Never a source of truth."""
    book = OneToOneField(Book, primary_key=True, on_delete=CASCADE, related_name="search_doc")
    document = SearchVectorField()
    title_sort = CharField(max_length=255)          # normalised, for A–Z keyset + trigram
    author_sort = CharField(max_length=255)         # first author "Last, First" normalised
    author_text = TextField()                       # all author names, trigram
    language = CharField(max_length=8, blank=True)  # normalised ISO 639-1
    formats = ArrayField(CharField(max_length=16), default=list)
    author_ids = ArrayField(BigIntegerField(), default=list)
    genre_ids = ArrayField(BigIntegerField(), default=list)
    category_ids = ArrayField(BigIntegerField(), default=list)
    publication_decade = SmallIntegerField(null=True)
    has_cover = BooleanField(default=False)
    thumbnail_key = CharField(max_length=64, blank=True)   # sha256 of thumbnail
    book_created_at = DateTimeField()               # copy, for keyset without join
    indexed_at = DateTimeField(auto_now=True)
    class Meta:
        indexes = [GinIndex(fields=["document"]),
                   GinIndex(fields=["title_sort"], opclasses=["gin_trgm_ops"], name=...),
                   GinIndex(fields=["author_text"], opclasses=["gin_trgm_ops"], name=...),
                   GinIndex(fields=["formats"]), GinIndex(fields=["genre_ids"]),
                   GinIndex(fields=["category_ids"]), GinIndex(fields=["author_ids"]),
                   Index(fields=["-book_created_at", "-book"]), Index(fields=["title_sort", "book"]),
                   Index(fields=["author_sort", "book"]), Index(fields=["language"])]

class CoverThumbnail(models.Model):       # derived artifact of a CoverImage
    cover = OneToOneField(CoverImage, on_delete=CASCADE, related_name="thumbnail")
    storage_location = FK(StorageLocation, PROTECT); storage_identifier = CharField(512)
    sha256 = CharField(64, unique=True); width = PositiveSmallIntegerField(); mime_type = CharField(32)
    tool = CharField(64); tool_version = CharField(32); created_at = DateTimeField(auto_now_add=True)

# Phase 3: class Series(name unique-ish, sort_name); Book.series FK null, Book.series_index Decimal null
# Phase 4: class Shelf(owner FK null, title, slug, visibility); class ShelfItem(shelf, book, position, added_by)
```

## 5. Risks

| Risk | Mitigation |
|---|---|
| Search document drifts from `Book` | Enqueue on every relevant change, a nightly rebuild, and an `indexed_at` lag metric. A `verify_search_index` command compares counts and samples. |
| Write amplification during bulk import | Index jobs are idempotent and coalesced (one job per book per minute). Bulk imports can defer to the nightly rebuild. |
| Facet counts on large sets slow (about 0.2 s) | Cache global counts. Use exact counts only on filtered sets. Revisit Meilisearch at the triggers above. |
| No typo tolerance | Trigram "did you mean" suggestions on zero results. Meilisearch upgrade path. |
| Thumbnail cache fills the VM disk (#7) | About 15 KB × 150k ≈ 2.3 GB. Configurable location, can be regenerated, and covered by the free-space checks. |
| Privacy | Search queries are not logged with user identity, only aggregate latency and zero-result counters. Shelves are private by default. |
| Licensing | All proposed components are PostgreSQL-licensed or MIT, Apache or BSD. Cover thumbnails are derived from covers already stored for internal use. |

## 6. Observability

- `ebook_server_search_request_duration_seconds{kind=browse|search|facets}`
  as a histogram.
- `ebook_server_search_zero_results_total`.
- `ebook_server_search_index_lag_seconds`, the oldest pending index job.
- `ebook_server_cover_thumbnails_missing`.
- Never log query strings together with a user id.

## 7. Test strategy

- **Unit tests:**
  - cursor encode/decode, including tamper rejection (signed token);
  - keyset boundaries with identical `created_at` values (the id is the tie
    breaker);
  - the 1,000-result cap for ranked search;
  - search-document building (authors, formats only from COMPLETE variants,
    language normalization);
  - facet query building and the SQLite fallback.
- **Query-count regression tests** with `django_assert_max_num_queries` for
  `/library/books/` and `/library/`. This locks in the phase 0 N+1 fix:
  expected 6 to 8 queries for any page size.
- **PostgreSQL integration tests** in the existing infrastructure job:
  - the GIN and trigram indexes exist after migrate;
  - `EXPLAIN` for the main browse and search queries shows index usage
    (assert on plan node types, not times);
  - FTS ranking puts a title match above a description match.
- **A performance smoke test.** A `manage.py seed_synthetic_library --books
  150000` command (development only, refuses to run in production) plus a
  scripted benchmark that prints the table in section 2. It runs by hand and
  in an optional nightly job, not on every PR.
- **Browser tests** (Playwright, which exists already):
  - search, facet click, next and previous cursor, and the shelf "see all"
    link;
  - images have `loading="lazy"` with width and height;
  - the thumbnail URL is cacheable (response headers).

## 8. MVP and later

- **MVP (phases 0 to 2):**
  - fix the N+1 and the DISTINCT;
  - the `BookSearchDocument` with PostgreSQL FTS and trigram;
  - keyset pagination;
  - facets for language, format, author and genre/category;
  - the search-first landing page with "Recently added";
  - cover thumbnails through nginx.
- **Later:**
  - series (phase 3);
  - shelves (phase 4);
  - infinite scroll, "did you mean", and per-language stemming;
  - a Meilisearch backend if the revisit triggers fire.

## 9. Phased implementation plan (issue-sized tasks)

0. **Fix the current list queries.**
   - Scope:
     - `Prefetch("file_variants", queryset=…COMPLETE…, to_attr="complete_variants")`
       and use it in `_serialize_book`;
     - replace join filters plus `.distinct()` with `Exists()` subqueries;
     - add an index on `Book(created_at, id)`;
     - leave `description` out of list payloads.
   - Acceptance:
     - `/library/books/` makes at most 8 queries for page sizes 25 and 100
       (regression test);
     - no `DISTINCT` in the generated SQL;
     - the JSON shape stays unchanged except for the documented removal of
       `description` from list items (or keep it behind
       `?fields=description`).
1. **`BookSearchDocument` and indexing jobs.**
   - Scope: `django.contrib.postgres`, the migration with extensions and
     indexes, an idempotent `index_book` RQ job, signals that enqueue it,
     `rebuild_search_index` and `verify_search_index`, and the lag metric.
   - Acceptance:
     - the document stays correct after create, update, variant completion and
       deletion (tests);
     - the rebuild covers 150k synthetic books in under 2 minutes on CI-class
       hardware;
     - the SQLite fallback works.
2. **`SearchBackend` and the new `/library/books/`.**
   - Scope: the PostgreSQL backend (FTS, trigram fallback on zero results),
     signed keyset cursors, the ranked-search cap, facet counts (exact when
     filtered, cached when global), the JSON API with `cursor`, and `page`
     deprecated.
   - Acceptance:
     - browse and search p95 under 150 ms server-side on the 150k synthetic set
       in the infrastructure job (measured by the benchmark command);
     - facet counts match a brute-force count in tests.
3. **Search-first landing page and facet UI.**
   - Scope: `/library/` with the search box, global facets, the "Recently
     added" and "Recently identified" shelves, and an author A to Z index.
   - Acceptance:
     - Playwright flows pass;
     - the page renders in at most 10 queries;
     - it is usable without JavaScript (server-rendered forms and links).
4. **Cover thumbnails.**
   - Scope: `CoverThumbnail`, the generation job (on extraction and as a
     backfill command), a content-addressed URL, and nginx `X-Accel-Redirect`
     with an `internal` location.
   - Acceptance:
     - thumbnails are at most 240 px wide WebP;
     - responses are `immutable`;
     - the permission check is still enforced (a test for a disabled user);
     - the nginx config passes `nginx -t` in CI.
5. **Series** *(depends on a provider decision)*.
   - Scope: the model, the migration, populating from #35 layout hints and from
     provider data where present, a series facet and index page, and series
     order on the book page.
   - Acceptance:
     - series hints become `Series` only through review or acceptance, never
       straight from filenames.
6. **Shelves.**
   - Scope: `Shelf` and `ShelfItem`, personal and curated shelves, permissions
     (normal users manage their own shelves, admins manage public ones), and
     shelves on the landing page.
   - Acceptance:
     - permission tests;
     - private shelves are never visible to others.
7. *(Later, only on the revisit triggers)* A Meilisearch `SearchBackend` with
   a sync worker and a backfill command.

## Decisions to confirm

1. A search-first landing page with facets, shelves and entity indexes. "All
   books" is the empty query sorted by newest (Q1, Q4).
2. PostgreSQL FTS and `pg_trgm` behind a `SearchBackend`. Meilisearch only if
   the revisit triggers fire. No OpenSearch (Q2).
3. Keyset pagination for browse, and ranked search capped at 1,000 results
   (Q3).
4. Phase 0 (the N+1 and DISTINCT fix) can be filed and done right away, ahead
   of the rest.
5. Cover thumbnails as derived artifacts served by nginx with `immutable`
   caching (Q5).

## Sources

- PostgreSQL 17 documentation, *Full Text Search*: <https://www.postgresql.org/docs/17/textsearch.html>
- PostgreSQL 17 documentation, *pg_trgm*: <https://www.postgresql.org/docs/17/pgtrgm.html>
- Django, *django.contrib.postgres* full-text search and indexes: <https://docs.djangoproject.com/en/5.2/ref/contrib/postgres/search/>
- Markus Winand, *We need tool support for keyset pagination* / "No offset": <https://use-the-index-luke.com/no-offset>
- Meilisearch, *Meilisearch vs PostgreSQL* (vendor comparison; read critically): <https://www.meilisearch.com/docs/resources/comparisons/postgresql>
- Meilisearch, typo tolerance settings: <https://www.meilisearch.com/docs/learn/relevancy/typo_tolerance_settings>
- MDN, lazy loading images: <https://developer.mozilla.org/en-US/docs/Web/Performance/Guides/Lazy_loading>
- nginx, `X-Accel-Redirect` / internal locations: <https://nginx.org/en/docs/http/ngx_http_core_module.html#internal>
- Measurements: synthetic data and benchmark run on 2026-11-17 against
  commit `65a78ba` (method in section 2).
