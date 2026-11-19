"""Derive the stratified ``cbase-ebooks-share-filetree-sample.csv`` from the full c-base
ebook share listing (88,028 rows, not committed: 16.9 MB).

Usage: python -I make_sample.py FULL_LISTING.csv OUT.csv

Deterministic (fixed seed). Sampling is by *structure*, not per row, so whole Calibre book
folders and whole scene-release / chapter-split sets stay intact.
"""

import collections
import csv
import random
import re
import sys

SEED = 20261118
FIELDS = ["relative_path", "folder", "filename", "extension", "size_bytes"]
NOISE_EXT = {"svn-base", "h", "cpp", "hpp", "bmp", "gif", "exe", "ds_store", "dsp", "lua"}
KEEP_EXT = {
    "epub", "pdf", "txt", "mobi", "azw", "azw3", "prc", "fb2", "djvu", "cbz", "cbr", "htm",
    "html", "rtf", "chm", "lit", "pdb", "lrf", "doc", "docx", "zip", "rar", "opf", "nfo",
    "diz", "7z", "gz", "iso", "uif", "ps", "mbp",
}  # fmt: skip
SERIES_EXT = {"pdf", "epub", "chm", "htm", "html", "ps", "txt"}
ISBN_RE = re.compile(r"isbn|97[89][- ]?\d[- ]?\d{2,7}[- ]?\d{1,7}[- ]?[\dX]|\[97[89]", re.I)
JUNK_AUTHOR_RE = re.compile(r"[\d\W_]+|Unbekannt|Diverse|Unknown|Various")
TITLE_IS_FILENAME_RE = re.compile(r"\.(jpg|tif|doc|pdf|epub|txt)( \(|\s|$)", re.I)

Row = dict[str, str]


def extension(row: Row) -> str:
    return row["extension"].lower()


def parts(row: Row) -> list[str]:
    return row["relative_path"].split("/")


def group_by_folder(rows: list[Row]) -> dict[str, list[Row]]:
    groups: dict[str, list[Row]] = collections.defaultdict(list)
    for row in rows:
        groups[row["folder"]].append(row)
    return groups


def calibre_book_id(folder: str) -> str | None:
    match = re.search(r"\((\d+)\)$", folder)
    return match.group(1) if match else None


def sample_calibre(rows: list[Row], rng: random.Random) -> list[Row]:
    """Whole Calibre book folders, oversampling the rare cases."""
    library = [row for row in rows if parts(row)[0] == "Sorted"]
    book_rows = [row for row in library if len(parts(row)) == 5]
    taken = [row for row in library if len(parts(row)) != 5]  # metadata.db, prefs, .DS_Store
    folders = group_by_folder(book_rows)
    names = sorted(folders)

    def formats(folder: str) -> set[str]:
        return {extension(row) for row in folders[folder]} - {"opf", "jpg"}

    def draw(candidates: list[str], count: int) -> set[str]:
        return set(rng.sample(candidates, min(count, len(candidates))))

    with_opf = [name for name in names if "opf" in {extension(r) for r in folders[name]}]
    opf_names = set(with_opf)
    chosen = draw(with_opf, 700)
    chosen |= draw([name for name in names if name not in opf_names], 60)
    chosen |= draw([name for name in names if len(formats(name)) > 1], 60)
    for fmt, count in (("lit", 25), ("azw3", 25), ("chm", 20), ("rtf", 10), ("txt", 15),
                       ("doc", 8), ("pdf", 40), ("mobi", 40)):  # fmt: skip
        chosen |= draw([name for name in names if fmt in formats(name)], count)
    odd_authors = [
        name
        for name in names
        if JUNK_AUTHOR_RE.fullmatch(name.split("/")[2]) or re.search(r"Hrsg|;", name.split("/")[2])
    ]
    chosen |= draw(odd_authors, 80)

    by_id: dict[str, list[str]] = collections.defaultdict(list)
    for name in names:
        book_id = calibre_book_id(name)
        if book_id:
            by_id[book_id].append(name)
    duplicate_ids = sorted(book_id for book_id, found in by_id.items() if len(found) > 1)
    for book_id in rng.sample(duplicate_ids, min(30, len(duplicate_ids))):
        chosen |= set(by_id[book_id])
    chosen |= {name for name in names if TITLE_IS_FILENAME_RE.search(name.split("/")[-1])}

    for name in sorted(chosen):
        taken.extend(folders[name])
    return taken


def sample_elsewhere(rows: list[Row], rng: random.Random) -> list[Row]:
    """Everything outside the Calibre tree."""
    outside = [row for row in rows if parts(row)[0] != "Sorted"]
    keep = [row for row in outside if extension(row) in KEEP_EXT]
    noise = [
        row for row in outside if extension(row) in NOISE_EXT or ".svn" in row["relative_path"]
    ]
    taken = [row for row in outside if len(parts(row)) == 1]  # root-level files

    # scene-release O'Reilly folders: whole folders
    releases = group_by_folder(
        [
            row
            for row in keep
            if parts(row)[:2] == ["knowledge", "[OReilly] Bookshelf"] and len(parts(row)) > 3
        ]
    )
    for folder in rng.sample(sorted(releases), 25):
        taken.extend(releases[folder])

    # numbered / chapter-split sets: whole directories (capped)
    for files in group_by_folder([row for row in keep if extension(row) in SERIES_EXT]).values():
        shapes = collections.Counter(re.sub(r"\d+", "#", row["filename"]) for row in files)
        if len(files) >= 6 and shapes.most_common(1)[0][1] >= 6:
            taken.extend(files[:40])

    # ISBN-in-filename files
    isbn_files = [row for row in keep if ISBN_RE.search(row["filename"])]
    taken.extend(rng.sample(isbn_files, min(80, len(isbn_files))))

    # same name + size duplicates across trees
    same: dict[tuple[str, str], list[Row]] = collections.defaultdict(list)
    for row in keep:
        if int(row["size_bytes"] or 0) > 100_000:
            same[(row["filename"].lower(), row["size_bytes"])].append(row)
    duplicate_groups = [found for found in same.values() if len(found) > 1]
    for found in rng.sample(duplicate_groups, min(25, len(duplicate_groups))):
        taken.extend(found)

    # proportional slice of every tree (at least 4 per second-level folder)
    trees: dict[str, dict[str, list[Row]]] = collections.defaultdict(
        lambda: collections.defaultdict(list)
    )
    for row in keep:
        path = parts(row)
        if len(path) > 1:
            trees[path[0]][path[1] if len(path) > 2 else "."].append(row)
    for tree in sorted(trees):
        for sub in sorted(trees[tree]):
            files = trees[tree][sub]
            taken.extend(rng.sample(files, min(len(files), max(4, len(files) // 12))))

    # DOS 8.3 mangled directories
    mangled = [row for row in keep if re.search(r"~\d", row["relative_path"])]
    taken.extend(rng.sample(mangled, min(20, len(mangled))))

    # a few rows of each noise class, so it stays visible without bulk
    by_noise_ext: dict[str, list[Row]] = collections.defaultdict(list)
    for row in noise:
        by_noise_ext[extension(row)].append(row)
    for ext in sorted(by_noise_ext):
        found = by_noise_ext[ext]
        taken.extend(rng.sample(found, min(4, len(found))))
    return taken


def main(src: str, dst: str) -> None:
    rng = random.Random(SEED)
    with open(src, encoding="utf-8", errors="replace", newline="") as handle:
        rows = list(csv.DictReader(handle))
    picked: dict[str, Row] = {}
    for row in sample_calibre(rows, rng) + sample_elsewhere(rows, rng):
        picked[row["relative_path"]] = row
    with open(dst, "w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        for path in sorted(picked):
            writer.writerow({key: picked[path][key] for key in FIELDS})
    print(len(rows), "->", len(picked))


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
