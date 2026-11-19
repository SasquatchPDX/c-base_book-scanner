# c-base ebook share: structure analysis (2026-11-18)

> **Status: preliminary, descriptive only. Further analysis and engineering are required
> before any design or code change is derived from this.** This is a filename/size
> listing; nothing here was verified against file content, `metadata.db` or the OPFs. All
> counts are of the listing, and several heuristics below (ISBN patterns, "chapter split"
> detection, duplicate groups) are rough regexes, not validated classifiers.

**Why it matters.** The listing is of the exact environment the software will read from
and store to, and all of these books will need to be processed and taken in for
production. The two older fixtures in `tests/fixtures/real_world_samples/` come from a
personal NAS and are only representative. A sampled version of the listing is in
`tests/fixtures/real_world_samples/c-base/`.

Source: `cbo-ebooks.csv`, 88,028 rows, 228.3 GB, same 5-column schema as the fixtures.

## 1. Overall shape

| Top-level tree | Files | Size | What it is |
|---|---|---|---|
| `Sorted/Calibre-Windows` | 71,808 | 91 GB | Calibre library (81.6% of rows) |
| `Bücher` | 4,517 | 54 GB | German topic folders, O'Reilly dump |
| `Gentoomen Library` | 3,618 | 35 GB | English CS/programming library, 28 topic folders |
| `book`, `knowledge`, `ebooks`, `books`, `Fachliteratur` | ~6,500 | ~25 GB | mixed dumps |
| 14 small folders | ~900 | ~15 GB | `assembly`, `Springer Graduate Texts in Mathematics`, `sciencemadness`, `Spiele/Schach`, `LPIC`, `Datasheets`, `cat-v`, `weeb_shit`, ... |
| root | 9 | 10.9 GB | three archives, a few loose books |

- By bytes: pdf 145.6 GB (13,150 files, 64%), epub 39.8 GB (14,327).
- Other formats: 6,815 mobi, 1,731 chm, 841 lit, 222 azw3, 90 djvu, 85 rtf, 43 doc.
- Archives: 627 zip (4 GB), 29 rar, 13 gz, 8 cbr, 5 iso. At the root:
  `random_ebooks.7z` (7.4 GB), `Musik-ebooks.7z` (2.9 GB),
  `ebook-mathematics-compilation.tar.gz` (0.56 GB). Their contents are **not visible** in
  this listing (about 11 GB, ~5% of the volume).
- Not ebooks: 582 `.h`, 410 `.cpp`, 129 `.hpp`, 338 `.svn-base` (plus `.dsp/.dsw/.lua/.exe`)
  from two book-companion source trees; 169 bmp; 17 `._*`/`.DS_Store`.

## 2. The Calibre library

- Layout `Sorted/Calibre-Windows/<Author>/<Title> (<id>)/`, plus a `metadata.db`
  (41.7 MB) and `metadata_db_prefs_backup.json` at its root.
- 23,512 book folders, 9,997 author folders (7,268 authors have a single book);
  Calibre ids 3 to 25,991.
- 22,400 `metadata.opf` (median 2.7 KB), 23,472 `cover.jpg`, no other jpgs.
- 1,112 folders (4.7%) have a book but **no OPF**: 812 mobi, 254 epub, 166 pdf, median id
  3,395 vs 14,470 for folders with an OPF, i.e. likely early imports. 43 folders lack a cover.
- `metadata.db` presumably covers all 23.5k books; whether it does, and how it relates to
  the OPFs, is **unchecked**.
- Formats per folder: epub only 12,441; mobi only 4,700; pdf only 1,764; lit only 801;
  epub+pdf 725; epub+mobi 574; mobi+pdf 420; azw3+epub+mobi 104. About 2,130 folders (9%)
  hold more than one format. No folder holds more than one epub.
- Median sizes: epub 0.56 MB, mobi 0.65 MB, pdf 3.6 MB, lit 0.39 MB; 99 epubs under 20 KB.
- File names are `<Title truncated> - <Author>.<ext>`: titles are cut (about 10% sit at
  ~30 chars) and **no filename contains ä/ö/ü/ß**; special characters became `_`. The
  folder name keeps the full title.
- Authors: 825 `Last, First`, 410 containing `;`, 134 `Unbekannt`, 35 `-`, 18 with `Hrsg`,
  plus `Various`/`Diverse`/`Unknown` as separate buckets; some author folders are really
  publisher/ISBN junk (`6734081`, `(c) 2018 Spotlight Verlag GmbH`); 41 titles are
  really filenames (`... .tif (125 pages)`).
- 479 book ids occur under two author folders (e.g. `Silberband 026 ... (24555)` under
  both `-` and `Klaus Frick (Hrsg.)`): stale copies after an author rename, not two books.
- Roughly a quarter of titles carry a German stopword, the rest English; genre fiction
  dominates (Stephen King 186, Mike Resnick 153, Terry Pratchett 146, Perry Rhodan 111, ...).
  Magazines, comics (cbr) and a few non-book items (large jpg/zip "books") are mixed in.

## 3. Everything outside the Calibre tree

Hand-sorted, naming very inconsistent (closer to the personal-NAS `ebooks-share` fixture).
Only five Calibre-style folders with an OPF exist outside `Sorted` (three in
`ebooks/Cixin Liu [FreePaidBooks.online]`, two in `Fachliteratur/RaspberryPi`), so sidecar
metadata is effectively Calibre-only.

**ISBNs in filenames (about 341 files).** Springer GTM, 197 pdf:
`Author - Title (Year) [978-1-4612-9874-8].pdf`. Gentoomen, about 127:
`Title - ISBN 0471346136.pdf` or glued on (`...Mar.2005.ISBN0764598805.pdf`). A few in
Calibre (`_ISBN 978-3-8689-4102-9_`), two bare (`1558608680.chm`). Vendor part numbers
(`24592_APM_v1.pdf`, `253665.pdf` in `assembly/`) look like ISBNs but are not.

**O'Reilly (2,189 files, 7.5 GB with "OReilly" in the path)**, in three forms:
1. `knowledge/[OReilly] Bookshelf/`: 202 scene-release folders such as
   `OReilly.Mac.OS.X.Panther.in.a.Nutshell.2nd.Edition.Jun.2004.eBook-DDU/`: 178 contain
   exactly `file_id.diz` + group `.nfo` (`lib.nfo` x90, `ddu.nfo` x48, `snd.nfo` x40) +
   numbered zips of about 1.46 MB each (`d-omx202.zip`, ...), i.e. one book split into
   floppy-sized volumes. 3 to 11 files per book. Group tags and dates encode publisher,
   edition and month.
2. `Bücher/OReilly/`: 374 files, whole `OReilly_-_<Title>.chm/.pdf`, plus chapter sets
   (`UML in a Nutshell` 18 `ch#.pdf` and a web-save `_files/` folder; `Linux Device
   Drivers 3rd` 19 `ch#.pdf`).
3. About 340 more in Calibre and about 290 in `Gentoomen Library/Programming`.

**Multi-part books.** The only multi-epub set is
`weeb_shit/Ascendance of a Bookworm (J-Novel Club) [Kinoworm]` (28 epubs, `Part # - v##`,
plus `Fanbook/` and `Specials/`). Chapter splits elsewhere are PDF/HTML/PS, not epub:
`ch#.pdf`, `ccnab-module#.pdf`, `Mellor_ACTITC_#.pdf`, `chapter#.htm`, `chapter#.ps`.
The O'Reilly zips are volumes of a book, not separate books.

**Naming styles observed:** `Title - Author`; `Author - Title (Year) [ISBN]`;
`Title - ISBN nnnn`; `Title - Authors, et al., (Publisher, Year) WW.pdf`;
`Last, First - Title.pdf`; dot-separated scene names with no spaces (4,234 files);
`Was.Ist.Was..-..036..-..Polargebiete.pdf`; numeric prefixes (`21 - Embedded Robotics - ...`);
opaque names (`chapter3.ps`, `table3-10.html`). 265 names are 100+ characters (max 220).

## 4. Duplicates and noise

- Same name + size: 324 groups, 331 redundant copies, 2.9 GB (Gentoomen 227, Calibre 60,
  Fachliteratur 24).
- Same size, different name (files over 1 MB): 1,408 groups. These are probably the real
  cross-tree duplicates, but this listing has no hashes, so they are unverified.
- `.svn` 377 rows; `._*`/`.DS_Store` 17; two DOS 8.3 directories
  (`_UJSHA~2`, `_2J6HW~1`, a mirror of a "Henrys Bibliothek" CD with an `Autoren/` tree).
- 456 non-Calibre names with umlauts intact; 1 mojibake name.
- Large non-book content: `.iso`, `.uif`, 0.8 GB video-training zips.

## 5. Compared with the personal-NAS fixtures

| | calibre fixture | this share's Calibre tree |
|---|---|---|
| Books | ~1,190 | 23,512 |
| Formats | epub, pdf, mobi, lrf, pdb, azw, cbz, fb2, docx, ... | epub, mobi, pdf, lit (841), azw3 (222); **no** lrf/pdb/azw/cbz/fb2/docx in this listing |
| OPF coverage | all | 95% |

Not represented in the fixtures: scene-release O'Reilly packaging, ISBN-in-filename
conventions, chapter-split PDFs, duplicate ids after an author rename, the truncated
ASCII-folded Calibre filenames, and the unreadable top-level archives.

## 6. Observations (not decisions)

1. About two thirds of the books are in the Calibre tree, where `metadata.db` / OPFs may
   already provide metadata; the rest (about 12k files in 20 hand-sorted trees) would have
   to be identified from filenames and folder context.
2. In the Calibre tree the folder name holds the full title; the filename does not.
3. ISBN extraction looks cheap for Springer/Gentoomen but needs guarding against numeric
   part numbers.
4. A zip or a `ch#` file is often a part of a book, not a book. Grouping would need design.
5. The 11 GB of top-level archives may contain many more books than the listing shows.

## 7. Open questions / required follow-up before engineering

- Read `metadata.db` and a sample of OPFs: how complete, consistent and trustworthy are
  they, and how do they disagree with folder/file names?
- Hash or content-fingerprint the share to confirm the duplicate groups (and the
  same-size-different-name groups) before any dedup logic is designed.
- Inspect the contents of the three root archives and of a sample of zips.
- Check how the intake design handles the formats present (lit, chm, azw3, rar, 7z, iso,
  djvu) given the volumes above.
- Validate the ISBN, chapter-split and scene-release heuristics against real files and
  hand-labelled cases before relying on them.
- Treat the sample as structural illustration; do not tune parsers to it without a held-out set.

The analysis scripts were ad hoc and are not committed; the counts above can be
re-derived from the full listing.
