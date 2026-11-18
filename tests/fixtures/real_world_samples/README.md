# Real-world library file listings

Two CSV files listing the intake-relevant files of two real-world ebook collections,
captured 2026-11-17 and trimmed for publication (see **Trimming** below). **Metadata
only** — no file content and no hashes, just path/name/extension/size.

> **Production data:** `c-base/` holds a sampled listing of the c-base ebook share, the
> actual environment this software will run against (all of it must be ingested). It is
> raw research data that still needs analysis before changes are derived from it. The two
> files below come from a personal NAS and are only representative. See `c-base/README.md`
> and `docs/c-base-ebook-share-analysis.md`.

## Columns

| Column | Meaning |
|---|---|
| `relative_path` | Full path relative to the share root |
| `folder` | `relative_path` without the filename |
| `filename` | Base filename, including extension |
| `extension` | Lowercased extension, no leading dot (empty if none) |
| `size_bytes` | File size in bytes |

## Files

- **`ebooks-share-filetree.csv`** — 1,825 files, 67.0 GiB. A loosely organised
  collection: some `<Author>/<Book>` folders, some flat dumps, some topic folders
  (`Cliff Notes/`, `Anatomie/`, `misc-01/`, …). Formats: 785 pdf, 505 txt, 147 opf,
  141 epub, 97 chm, 20 each rtf/zip/htm/jpg (`cover.jpg`), 19 mobi, 13 rar, 12 djvu,
  11 doc, 9 lit, 4 pdb, 1 each html/prc.
- **`calibre-ebooks-share-filetree.csv`** — 3,174 files, 1.69 GiB. A genuine Calibre
  library (`<Author>/<Title> (<id>)/` folders). Formats: 1,190 opf, 585 jpg
  (`cover.jpg`), 498 txt, 387 pdf, 247 epub, 106 mobi, 29 lrf, 21 zip, 17 each rtf/lit,
  14 pdb, 11 each azw/djvu, 9 each rar/prc, 8 doc, 5 docx, 4 each cbz/fb2, 2 azw3.

## Trimming

The original captures (10,867 and 3,180 files) were cut down before publishing:

- **Self-scanned books removed.** The `self/` tree (8,927 rows: FineReader OCR projects
  and raw page scans of personally scanned books) is gone entirely; none of it is
  intake-relevant and it was an inventory of private scans.
- **Only intake-relevant rows kept.** Rows are kept only for ebook/document formats
  (epub, pdf, txt, mobi/azw/azw3/prc, fb2, djvu, cbz/cbr, htm/html, rtf, chm, lit, pdb,
  lrf, doc/docx, zip/rar) and the `metadata.opf` / `cover.jpg` sidecars; other files
  (images, OCR data, system files) were dropped (121 rows).
- **Personal folder names neutralised.** Non-bibliographic personal top-level folder
  names were replaced with placeholders (`misc-01/`), keeping the nesting depth so
  folder-layout research still works.

## Suggested uses

- Filename-signal-parsing research/regression testing (#15) — a much larger, messier
  sample than the 107-file set used in this project's own test imports so far.
  `ebooks-share-filetree.csv`'s `filename` column alone is a good source of real
  "Title - Author", "Author - Title", "Title, The", series/volume-prefixed, and
  completely un-parseable filenames to batch-check any parser against.
- Format-coverage research (#23) — real counts for currently-unsupported formats
  (chm, lit, rtf, pdb, prc, azw/azw3, doc/docx, zip/rar) actually present in a real
  collection, instead of a guess.
- Sidecar-metadata research (#14, closed; #33, cover sidecar) — real counts of
  `metadata.opf`/`cover.jpg` sidecars and where they do/don't exist.
- Scale/practicality research (#13) — realistic file counts and size distribution for
  the intake-relevant part of a collection.

## Not included

File content and hashes were deliberately not captured — this is a filename/metadata
research sample, not a corpus for content-based testing (which would also raise real
copyright questions this listing, being just names and sizes, does not).
