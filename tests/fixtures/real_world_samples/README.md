# Real-world library file listings

Two CSV files, each a flat recursive listing of every file in one real-world personal
ebook collection (owner's own NAS shares), captured 2026-11-17. **Metadata only** — no
file content and no hashes, just path/name/extension/size. Filenames and folder names are
real book titles, authors and personal folder names (e.g. `1337-shit/`, `Cliff Notes/`),
already referenced in several of this project's own GitHub issues; nothing here is more
sensitive than that.

## Columns

| Column | Meaning |
|---|---|
| `relative_path` | Full path relative to the share root |
| `folder` | `relative_path` without the filename |
| `filename` | Base filename, including extension |
| `extension` | Lowercased extension, no leading dot (empty if none) |
| `size_bytes` | File size in bytes |

## Files

- **`ebooks-share-filetree.csv`** — 10,867 files, 69.4 GiB. A loosely organised personal
  collection: some `<Author>/<Book>` folders, some flat dumps, some non-author folders
  (`Cliff Notes/`, `1337-shit/`, `Anatomie/`, …). Dominated by FineReader OCR projects
  (`.frdat`/`.dat`, ~5,300 files) and raw scan images (`.tiff`/`.png`/`.jpg`, ~2,500
  files) — i.e. mostly *not* intake-relevant on their own. Ebook-relevant formats:
  785 pdf, 505 txt, 147 opf, 141 epub, 97 chm, 20 each rtf/zip/htm, 19 mobi, 13 rar,
  12 each doc/djvu, 9 lit.
- **`calibre-ebooks-share-filetree.csv`** — 3,180 files, 1.70 GiB. A genuine Calibre
  library (`<Author>/<Title> (<id>)/` folders). Ebook-relevant formats: 1,190 opf,
  585 jpg (almost all `cover.jpg`), 498 txt, 387 pdf, 247 epub, 106 mobi, 29 lrf,
  21 zip, 17 each rtf/lit, 14 pdb, 11 each azw/djvu, 9 each rar/prc, 8 doc, 5 docx,
  4 each cbz/fb2, 2 azw3.

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
- Scale/practicality research (#13) — real file counts and a realistic size
  distribution for estimating a bulk import's actual cost.

## Not included

File content and hashes were deliberately not captured — this is a filename/metadata
research sample, not a corpus for content-based testing (which would also raise real
copyright questions this listing, being just names and sizes, does not).
