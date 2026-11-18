"""Derive the stratified sample ``cbase-ebooks-share-filetree-sample.csv`` from the full
c-base ebook share listing (88,028 rows, not committed -- 16.9 MB).

Usage: python -I make_sample.py FULL_LISTING.csv OUT.csv

Deterministic (fixed seed). Sampling is by *structure*, not at random per row, so that
whole Calibre book folders and whole scene-release / chapter-split sets stay intact.
"""
import collections
import csv
import random
import re
import sys

SEED = 20261118
KEEP_EXT = {"epub", "pdf", "txt", "mobi", "azw", "azw3", "prc", "fb2", "djvu", "cbz", "cbr",
            "htm", "html", "rtf", "chm", "lit", "pdb", "lrf", "doc", "docx", "zip", "rar",
            "opf", "nfo", "diz", "7z", "gz", "iso", "uif", "ps", "mbp"}
NOISE_EXT = ("svn-base", "h", "cpp", "hpp", "bmp", "gif", "exe", "ds_store", "dsp", "lua")


def main(src, dst):
    rng = random.Random(SEED)
    rows = list(csv.DictReader(open(src, encoding="utf-8", errors="replace", newline="")))
    for r in rows:
        r["ext"] = r["extension"].lower()
        r["p"] = r["relative_path"].split("/")
    picked = {}  # relative_path -> row (dict keeps insertion order, dedups)

    def take(rs):
        for r in rs:
            picked[r["relative_path"]] = r

    # ---- Calibre library: whole book folders ------------------------------------------
    cal = collections.defaultdict(list)
    meta = []
    for r in rows:
        if r["p"][0] == "Sorted":
            if len(r["p"]) == 5:
                cal[r["folder"]].append(r)
            else:
                meta.append(r)  # metadata.db, prefs backup, .DS_Store at the library root
    take(meta)
    folders = sorted(cal)
    has_opf = lambda d: any(x["ext"] == "opf" for x in cal[d])
    books = lambda d: {x["ext"] for x in cal[d]} - {"opf", "jpg"}
    ident = lambda d: (re.search(r"\((\d+)\)$", d) or [None, None])[1]

    def pick_dirs(ds, n):
        ds = sorted(ds)
        return rng.sample(ds, min(n, len(ds)))

    chosen = set(pick_dirs([d for d in folders if has_opf(d)], 700))           # base
    chosen |= set(pick_dirs([d for d in folders if not has_opf(d)], 60))       # no OPF
    chosen |= set(pick_dirs([d for d in folders if len(books(d)) > 1], 60))    # multi-format
    for ext, n in (("lit", 25), ("azw3", 25), ("chm", 20), ("rtf", 10), ("txt", 15), ("doc", 8),
                   ("pdf", 40), ("mobi", 40)):
        chosen |= set(pick_dirs([d for d in folders if ext in books(d)], n))
    odd = [d for d in folders if re.fullmatch(r"[\d\W_]+|Unbekannt|Diverse|Unknown|Various", d.split("/")[2])
           or re.search(r"Hrsg|;", d.split("/")[2])]
    chosen |= set(pick_dirs(odd, 80))
    byid = collections.defaultdict(list)
    for d in folders:
        if ident(d):
            byid[ident(d)].append(d)
    dup = [i for i, v in byid.items() if len(v) > 1]
    for i in rng.sample(sorted(dup), min(30, len(dup))):                        # duplicate ids
        chosen |= set(byid[i])
    for d in folders:                                                           # filename-as-title imports
        if re.search(r"\.(jpg|tif|doc|pdf|epub|txt)( \(|\s|$)", d.split("/")[-1], re.I):
            chosen.add(d)
    for d in chosen:
        take(cal[d])

    # ---- everything outside the Calibre tree ------------------------------------------
    out = [r for r in rows if r["p"][0] != "Sorted"]
    keep = [r for r in out if r["ext"] in KEEP_EXT]
    noise = [r for r in out if r["ext"] in NOISE_EXT or ".svn" in r["relative_path"]]
    take(r for r in out if len(r["p"]) == 1)                                    # root-level files

    def by_dir(rs):
        d = collections.defaultdict(list)
        for r in rs:
            d[r["folder"]].append(r)
        return d

    # scene-release O'Reilly folders: whole folders
    sc = by_dir(r for r in keep if r["p"][:2] == ["knowledge", "[OReilly] Bookshelf"] and len(r["p"]) > 3)
    for d in rng.sample(sorted(sc), 25):
        take(sc[d])
    # numbered / chapter-split sets: whole directories
    series = by_dir(r for r in keep if r["ext"] in {"pdf", "epub", "chm", "htm", "html", "ps", "txt"})
    for d, l in series.items():
        if len(l) >= 6 and collections.Counter(re.sub(r"\d+", "#", x["filename"]) for x in l).most_common(1)[0][1] >= 6:
            take(l[:40])
    # ISBN-in-filename files
    isbn = [r for r in keep if re.search(r"isbn|97[89][- ]?\d[- ]?\d{2,7}[- ]?\d{1,7}[- ]?[\dX]|\[97[89]", r["filename"], re.I)]
    take(rng.sample(isbn, min(80, len(isbn))))
    # same name+size duplicates across trees
    nm = collections.defaultdict(list)
    for r in keep:
        if int(r["size_bytes"] or 0) > 100000:
            nm[(r["filename"].lower(), r["size_bytes"])].append(r)
    dups = [v for v in nm.values() if len(v) > 1]
    for v in rng.sample(dups, min(25, len(dups))):
        take(v)
    # per-tree proportional sample (min 4 per second-level folder)
    trees = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in keep:
        if len(r["p"]) > 1:
            trees[r["p"][0]][r["p"][1] if len(r["p"]) > 2 else "."].append(r)
    for tree, subs in sorted(trees.items()):
        for sub, l in sorted(subs.items()):
            take(rng.sample(l, min(len(l), max(4, len(l) // 12))))
    # 8.3 mangled directories
    take(rng.sample([r for r in keep if re.search(r"~\d", r["relative_path"])], 20))
    # a handful of each noise class, to keep it visible without bulk
    nz = collections.defaultdict(list)
    for r in noise:
        nz[r["ext"]].append(r)
    for ext, l in nz.items():
        take(rng.sample(l, min(4, len(l))))

    fields = ["relative_path", "folder", "filename", "extension", "size_bytes"]
    with open(dst, "w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        for path in sorted(picked):
            w.writerow({k: picked[path][k] for k in fields})
    print(len(rows), "->", len(picked))


if __name__ == "__main__":
    main(*sys.argv[1:3])
