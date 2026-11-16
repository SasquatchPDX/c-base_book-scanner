"""Tests for reading a Calibre-style metadata.opf sidecar during intake."""

from __future__ import annotations

from pathlib import Path

from ebook_server.core import intake

CALIBRE_OPF = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Phylogenesis</dc:title>
    <dc:creator opf:role="aut">Alan Dean Foster</dc:creator>
    <dc:creator opf:role="trl">Someone Translator</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Del Rey</dc:publisher>
    <dc:description>A founding-of-the-Commonwealth novel.</dc:description>
    <dc:identifier opf:scheme="ISBN">034549427X</dc:identifier>
    <dc:identifier opf:scheme="calibre">a1b2c3d4-uuid</dc:identifier>
  </metadata>
</package>
"""


def _book_folder(tmp_path: Path) -> Path:
    folder = tmp_path / "Alan Dean Foster" / "Phylogenesis (786)"
    folder.mkdir(parents=True)
    return folder


def test_sidecar_opf_overrides_sparse_embedded_metadata(tmp_path: Path) -> None:
    folder = _book_folder(tmp_path)
    (folder / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    txt_path = folder / "Phylogenesis.txt"
    txt_path.write_text("Some plain text with no metadata at all.", encoding="utf-8")

    metadata = intake.extract_metadata(str(txt_path), "txt", original_filename=txt_path.name)

    assert metadata["title"] == "Phylogenesis"
    assert metadata["authors"] == ["Alan Dean Foster"]
    assert metadata["language"] == "en"
    assert metadata["publisher"] == "Del Rey"
    assert metadata["description"] == "A founding-of-the-Commonwealth novel."
    assert metadata["identifier"] == "034549427X"


def test_sidecar_opf_excludes_non_author_roles(tmp_path: Path) -> None:
    folder = _book_folder(tmp_path)
    (folder / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    txt_path = folder / "Phylogenesis.txt"
    txt_path.write_text("text", encoding="utf-8")

    metadata = intake.extract_metadata(str(txt_path), "txt", original_filename=txt_path.name)

    assert "Someone Translator" not in metadata["authors"]


def test_sidecar_opf_lookup_is_case_insensitive(tmp_path: Path) -> None:
    folder = _book_folder(tmp_path)
    (folder / "METADATA.OPF").write_text(CALIBRE_OPF, encoding="utf-8")
    txt_path = folder / "Phylogenesis.txt"
    txt_path.write_text("text", encoding="utf-8")

    metadata = intake.extract_metadata(str(txt_path), "txt", original_filename=txt_path.name)

    assert metadata["title"] == "Phylogenesis"


def test_no_sidecar_opf_leaves_embedded_metadata_untouched(tmp_path: Path) -> None:
    html_path = tmp_path / "web-book.html"
    html_path.write_text(
        "<!doctype html><html><head><title>HTML Book</title></head><body></body></html>",
        encoding="utf-8",
    )

    metadata = intake.extract_metadata(str(html_path), "html", original_filename=html_path.name)

    assert metadata["title"] == "HTML Book"


def test_malformed_sidecar_opf_is_ignored(tmp_path: Path) -> None:
    folder = _book_folder(tmp_path)
    (folder / "metadata.opf").write_text("<not><valid", encoding="utf-8")
    txt_path = folder / "Phylogenesis.txt"
    txt_path.write_text("text", encoding="utf-8")

    metadata = intake.extract_metadata(str(txt_path), "txt", original_filename=txt_path.name)

    assert metadata["title"] == "Phylogenesis"  # falls back to the filename stem
