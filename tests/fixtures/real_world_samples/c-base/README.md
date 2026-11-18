# c-base production ebook share: sampled listing

> **Status: raw research data. Needs further analysis and engineering before any
> change is derived from it.** See `docs/c-base-ebook-share-analysis.md`.

`cbase-ebooks-share-filetree-sample.csv` is a stratified sample of the file listing of the
**c-base ebook storage — the exact environment this software will store and read ebooks
from. Every book on it will have to be processed and taken in for production.**

This is different from the two listings one directory up (`../ebooks-share-filetree.csv`,
`../calibre-ebooks-share-filetree.csv`), which come from a personal NAS and are only
representative. Treat this one as the ground truth for scale, structure and mess; treat the
other two as supplementary.

Same columns as the sibling files: `relative_path, folder, filename, extension, size_bytes`.
Metadata only: no content, no hashes. Listing captured 2026-11-18.

## What the sample is

5,465 of the 88,028 rows (6.2%), 1.1 MB, sampled by **structure** rather than per row so
that Calibre book folders and release/chapter sets stay whole:

- ~1,000 complete Calibre book folders (book file(s), `metadata.opf`, `cover.jpg`),
  oversampling the rare cases: folders without OPF, multi-format books, `lit`/`azw3`/`chm`/
  `rtf`/`doc`, junk author folders (`Unbekannt`, `-`, numeric, `Hrsg`), duplicate-id pairs,
  and titles that are really filenames. Plus the library's `metadata.db` row.
- 25 whole scene-release O'Reilly folders (`file_id.diz` + `.nfo` + split zips).
- All numbered/chapter-split directories (`ch#.pdf`, `chapter#.htm`, ...), capped at 40 rows.
- 80 files with an ISBN in the filename, 25 same-name-same-size duplicate groups, the
  8.3-mangled directories, and all root-level files (including the three large archives).
- A proportional slice (at least 4) of every other folder; a few rows of each noise class
  (`.svn`, C/C++ sources, `.DS_Store`) so they stay visible without bulk.

**Because of oversampling the sample does not preserve proportions.** Do not compute
statistics from it; the numbers in the analysis document come from the full listing.

`make_sample.py` regenerates the CSV deterministically (fixed seed) from the full listing,
which is not committed (16.9 MB).

## Not trimmed for privacy

Unlike the personal-NAS listings, nothing here was neutralised: it is a shared community
library and the folder names are bibliographic or topical. Review before adding any
listing from a different source.

## Test impact

`tests/test_filename_signals_real_world.py` globs `real_world_samples/*.csv` only
(non-recursive), so this subdirectory is not picked up by existing tests. Using it for
parser regression is a deliberate follow-up, not part of this change.
