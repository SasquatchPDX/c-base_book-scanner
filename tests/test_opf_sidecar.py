"""Calibre-style ``metadata.opf`` sidecars during source-folder intake (issue #14)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from django.test import override_settings
from isbnlib import to_isbn13

from ebook_server.core import covers, intake
from ebook_server.core.models import ImportJob, ReviewTask, SourceLocation, StorageLocation

PHYLOGENESIS_ISBN = "034549427X"
OTHER_ISBN = "9780306406157"

CALIBRE_OPF = f"""<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"
            xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Phylogenesis</dc:title>
    <dc:creator opf:role="aut">Alan Dean Foster</dc:creator>
    <dc:creator opf:role="trl">Someone Translator</dc:creator>
    <dc:language>en</dc:language>
    <dc:publisher>Del Rey</dc:publisher>
    <dc:description>A founding-of-the-Commonwealth novel.</dc:description>
    <dc:identifier opf:scheme="calibre">a1b2c3d4-uuid</dc:identifier>
    <dc:identifier opf:scheme="ISBN">{PHYLOGENESIS_ISBN}</dc:identifier>
  </metadata>
</package>
"""

ENTITY_OPF = """<?xml version="1.0"?>
<!DOCTYPE package [<!ENTITY title "Phylogenesis">]>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>&title;</dc:title>
  <dc:identifier>034549427X</dc:identifier></metadata>
</package>
"""


class StubProvider:
    """Returns an ISBN-exact Phylogenesis record for ISBN lookups, else echoes the title."""

    provider_id = "stub"

    def __init__(self) -> None:
        self.isbn_queries: list[str] = []

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        del folder_hint
        if isbn:
            self.isbn_queries.append(isbn)
            return [
                {
                    "key": f"isbn:{isbn}",
                    "title": "Phylogenesis",
                    "author_name": ["Alan Dean Foster"],
                    "isbn_13": [isbn if len(isbn) == 13 else to_isbn13(isbn)],
                }
            ]
        return [{"key": "title", "title": title or "Unknown", "author_name": [author or "?"]}]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> StubProvider:
    stub = StubProvider()
    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [stub],
    )
    return stub


def _source_location(root: Path) -> SourceLocation:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return SourceLocation.objects.create(
        name=f"opf-{root.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        recursive=True,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
        is_active=True,
    )


def _import_all(root: Path, tmp_path: Path) -> list[ImportJob]:
    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        jobs = sorted(
            intake.discover_source_jobs_for_location(_source_location(root)),
            key=lambda job: job.source_path,
        )
        for job in jobs:
            intake.process_import_job(str(job.id))
    return [ImportJob.objects.get(pk=job.pk) for job in jobs]


def _write_epub(
    path: Path,
    *,
    title: str,
    author: str,
    identifier: str | None = None,
) -> None:
    identifier_xml = (
        f'<dc:identifier opf:scheme="ISBN">{identifier}</dc:identifier>' if identifier else ""
    )
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        archive.writestr(
            "content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:opf="http://www.idpf.org/2007/opf">'
            f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>{identifier_xml}"
            "</metadata></package>",
        )


def _sidecar_evidence(job: ImportJob) -> dict[str, Any]:
    assert isinstance(job.evidence, dict)
    value = job.evidence[intake.SIDECAR_EVIDENCE_KEY]
    assert isinstance(value, dict)
    return value


@pytest.mark.django_db(transaction=True)
def test_single_book_folder_fills_gaps_from_sidecar_with_provenance(
    tmp_path: Path,
    provider: StubProvider,
) -> None:
    root = tmp_path / "library"
    book_dir = root / "Alan Dean Foster" / "Phylogenesis (786)"
    book_dir.mkdir(parents=True)
    (book_dir / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    (book_dir / "Phylogenesis - Alan Dean Foster.txt").write_text(
        "Plain text without any embedded metadata.", encoding="utf-8"
    )

    (job,) = _import_all(root, tmp_path)

    assert isinstance(job.evidence, dict)
    assert job.evidence["title"] == "Phylogenesis"
    # The txt has no embedded title: the stem fallback was replaced by the sidecar.
    assert job.evidence[intake.TITLE_SOURCE_KEY] == intake.TITLE_SOURCE_SIDECAR
    assert job.evidence["authors"] == ["Alan Dean Foster"]  # translator role excluded
    assert job.evidence["isbn"] == PHYLOGENESIS_ISBN
    assert job.evidence["publisher"] == "Del Rey"
    sidecar = _sidecar_evidence(job)
    assert sidecar["source"] == "metadata.opf"
    assert sidecar["conflicts"] == []
    assert set(sidecar["applied_fields"]) == {
        "title",
        "authors",
        "language",
        "description",
        "publisher",
        "isbn",
    }
    assert sidecar["fields"]["identifier"] == PHYLOGENESIS_ISBN
    assert provider.isbn_queries  # the sidecar ISBN reached external verification
    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert job.book.title == "Phylogenesis"


@pytest.mark.django_db(transaction=True)
def test_flat_multi_book_folder_ignores_stray_sidecar(
    tmp_path: Path,
    provider: StubProvider,
) -> None:
    """A stray metadata.opf must not turn every book in a flat folder into one ISBN."""
    root = tmp_path / "flat"
    root.mkdir()
    (root / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    (root / "Dune - Frank Herbert.txt").write_text("Dune text.", encoding="utf-8")
    (root / "Neuromancer - William Gibson.txt").write_text("Gibson text.", encoding="utf-8")

    jobs = _import_all(root, tmp_path)

    assert len(jobs) == 2
    for job in jobs:
        assert isinstance(job.evidence, dict)
        assert intake.SIDECAR_EVIDENCE_KEY not in job.evidence
        assert job.evidence["title"] != "Phylogenesis"
        assert "isbn" not in job.evidence
        assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert provider.isbn_queries == []


@pytest.mark.django_db(transaction=True)
def test_malicious_sidecar_is_ignored_without_failing_the_job(
    tmp_path: Path,
    provider: StubProvider,
) -> None:
    root = tmp_path / "entities"
    root.mkdir()
    (root / "metadata.opf").write_text(ENTITY_OPF, encoding="utf-8")
    (root / "Lonely Book.txt").write_text("text", encoding="utf-8")

    (job,) = _import_all(root, tmp_path)

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.last_error == ""
    assert isinstance(job.evidence, dict)
    assert intake.SIDECAR_EVIDENCE_KEY not in job.evidence
    assert job.evidence["title"] == "Lonely Book"
    assert provider.isbn_queries == []


@pytest.mark.django_db(transaction=True)
def test_sidecar_isbn_conflicting_with_embedded_title_does_not_auto_accept(
    tmp_path: Path,
    provider: StubProvider,
) -> None:
    """Merged, the sidecar ISBN alone would score 0.95 and auto-accept the wrong book."""
    root = tmp_path / "conflict-title"
    root.mkdir()
    (root / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    _write_epub(root / "Dune - Frank Herbert.epub", title="Dune", author="Frank Herbert")

    (job,) = _import_all(root, tmp_path)

    assert provider.isbn_queries == []
    assert isinstance(job.evidence, dict)
    assert job.evidence["title"] == "Dune"
    assert job.evidence["authors"] == ["Frank Herbert"]
    assert "isbn" not in job.evidence
    sidecar = _sidecar_evidence(job)
    assert set(sidecar["conflicts"]) == {"title", "authors"}
    assert sidecar["applied_fields"] == []
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert ReviewTask.objects.filter(import_job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_conflicting_sidecar_isbn_keeps_embedded_isbn_and_forces_review(
    tmp_path: Path,
    provider: StubProvider,
) -> None:
    root = tmp_path / "conflict-isbn"
    root.mkdir()
    (root / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    _write_epub(
        root / "Phylogenesis.epub",
        title="Phylogenesis",
        author="Alan Dean Foster",
        identifier=OTHER_ISBN,
    )

    (job,) = _import_all(root, tmp_path)

    assert isinstance(job.evidence, dict)
    assert job.evidence["identifier"] == OTHER_ISBN
    assert "isbn" not in job.evidence
    assert _sidecar_evidence(job)["conflicts"] == ["isbn"]
    # The embedded ISBN is still what external verification sees.
    assert provider.isbn_queries == [OTHER_ISBN]
    # The top candidate would clear the auto-accept threshold, but the conflict wins.
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert ReviewTask.objects.filter(import_job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_symlinked_sidecar_is_refused(tmp_path: Path, provider: StubProvider) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    root = tmp_path / "linked"
    root.mkdir()
    (root / "Lonely Book.txt").write_text("text", encoding="utf-8")
    try:
        os.symlink(outside / "metadata.opf", root / "metadata.opf")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform/account")

    (job,) = _import_all(root, tmp_path)

    assert isinstance(job.evidence, dict)
    assert intake.SIDECAR_EVIDENCE_KEY not in job.evidence
    assert provider.isbn_queries == []


@pytest.mark.django_db(transaction=True)
def test_sidecar_lookup_skips_uploads_disabled_setting_and_foreign_folders(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lookup"
    root.mkdir()
    book = root / "Phylogenesis.txt"
    book.write_text("text", encoding="utf-8")
    (root / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    location = _source_location(root)
    job = intake.create_source_import_job(book, location)

    sidecar = intake.read_sidecar_metadata(job)
    assert sidecar is not None
    assert sidecar["title"] == "Phylogenesis"

    with override_settings(BOOK_SCANNER_SIDECAR_METADATA_ENABLED=False):
        assert intake.read_sidecar_metadata(job) is None

    upload_job = ImportJob(source_type=ImportJob.SourceType.UPLOAD, source_path=str(book))
    assert intake.read_sidecar_metadata(upload_job) is None

    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    location.root_path = str(other_root)
    assert intake.read_sidecar_metadata(job) is None


@pytest.mark.django_db(transaction=True)
def test_cover_and_metadata_sidecars_agree_about_a_folder(tmp_path: Path) -> None:
    """Both sidecars use the one single-book-folder rule."""
    root = tmp_path / "agree"
    root.mkdir()
    (root / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    (root / "cover.jpg").write_bytes(b"not inspected by the lookup")
    first = root / "Phylogenesis.txt"
    first.write_text("text", encoding="utf-8")
    location = _source_location(root)
    job = intake.create_source_import_job(first, location)
    assert intake.read_sidecar_metadata(job) is not None
    assert covers.find_sidecar_cover(job) is not None

    (root / "Phylogenesis.epub").write_bytes(b"same stem: still one book")
    assert intake.read_sidecar_metadata(job) is not None
    assert covers.find_sidecar_cover(job) is not None

    (root / "Another Book.txt").write_text("text", encoding="utf-8")
    assert intake.read_sidecar_metadata(job) is None
    assert covers.find_sidecar_cover(job) is None


def test_apply_sidecar_metadata_never_overwrites_embedded_values() -> None:
    embedded: dict[str, Any] = {
        "title": "Phylogenesis: Founding of the Commonwealth",
        "authors": ["Alan Dean Foster"],
        "identifier": "urn:uuid:1234",
        "language": None,
        "publisher": "Embedded House",
    }
    sidecar = {
        "title": "Phylogenesis",
        "authors": ["Alan Dean Foster", "Second Author"],
        "language": "en",
        "publisher": "Del Rey",
        "identifier": PHYLOGENESIS_ISBN,
    }

    merged = intake.apply_sidecar_metadata(embedded, sidecar)

    assert merged["title"] == embedded["title"]
    assert merged["authors"] == ["Alan Dean Foster"]
    assert merged["publisher"] == "Embedded House"
    assert merged["identifier"] == "urn:uuid:1234"
    assert merged["language"] == "en"
    assert merged["isbn"] == PHYLOGENESIS_ISBN
    evidence = merged[intake.SIDECAR_EVIDENCE_KEY]
    assert evidence["applied_fields"] == ["language", "isbn"]
    assert evidence["fields"]["publisher"] == "Del Rey"
    assert "sidecar_metadata" not in embedded  # input left untouched


@pytest.mark.django_db(transaction=True)
def test_stale_sidecar_cannot_replace_embedded_title_equal_to_filename_stem(
    tmp_path: Path,
    provider: StubProvider,
) -> None:
    """PR #95 review probe: Midworld.epub embeds "Midworld"; a stale OPF says Phylogenesis.

    The embedded title equals the filename stem, but it is still the file's own
    dc:title. It must survive, the stale sidecar ISBN must not reach verification,
    and the contradiction must be recorded and sent to review, even though the
    authors agree.
    """
    root = tmp_path / "library"
    book_dir = root / "Alan Dean Foster" / "Midworld"
    book_dir.mkdir(parents=True)
    (book_dir / "metadata.opf").write_text(CALIBRE_OPF, encoding="utf-8")
    _write_epub(book_dir / "Midworld.epub", title="Midworld", author="Alan Dean Foster")

    (job,) = _import_all(root, tmp_path)

    assert isinstance(job.evidence, dict)
    assert job.evidence["title"] == "Midworld"
    assert job.evidence[intake.TITLE_SOURCE_KEY] == intake.TITLE_SOURCE_EMBEDDED
    assert job.evidence["authors"] == ["Alan Dean Foster"]
    assert "isbn" not in job.evidence
    sidecar = _sidecar_evidence(job)
    assert sidecar["conflicts"] == ["title"]
    assert sidecar["applied_fields"] == []
    assert sidecar["fields"]["title"] == "Phylogenesis"
    assert provider.isbn_queries == []  # PHYLOGENESIS_ISBN never verified
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book is None
    assert ReviewTask.objects.filter(import_job=job).exists()


def test_extractor_records_title_provenance(tmp_path: Path) -> None:
    embedded = tmp_path / "Midworld.epub"
    _write_epub(embedded, title="Midworld", author="Alan Dean Foster")
    plain = tmp_path / "Midworld.txt"
    plain.write_text("No metadata here.", encoding="utf-8")

    from_epub = intake.extract_metadata(str(embedded), "epub", original_filename=embedded.name)
    from_txt = intake.extract_metadata(str(plain), "txt", original_filename=plain.name)

    assert from_epub["title"] == "Midworld"
    assert from_epub[intake.TITLE_SOURCE_KEY] == intake.TITLE_SOURCE_EMBEDDED
    assert from_txt["title"] == "Midworld"
    assert from_txt[intake.TITLE_SOURCE_KEY] == intake.TITLE_SOURCE_FILENAME


@pytest.mark.parametrize(
    ("title_source", "expected_conflicts", "expected_title"),
    [
        (intake.TITLE_SOURCE_EMBEDDED, ["title"], "Midworld"),
        (intake.TITLE_SOURCE_FILENAME, [], "Phylogenesis"),
    ],
)
def test_apply_sidecar_metadata_uses_title_source_not_filename_comparison(
    title_source: str,
    expected_conflicts: list[str],
    expected_title: str,
) -> None:
    metadata: dict[str, Any] = {
        "title": "Midworld",
        intake.TITLE_SOURCE_KEY: title_source,
        "authors": ["Alan Dean Foster"],
    }
    sidecar = {
        "title": "Phylogenesis",
        "authors": ["Alan Dean Foster"],
        "identifier": PHYLOGENESIS_ISBN,
    }

    merged = intake.apply_sidecar_metadata(metadata, sidecar)

    assert merged[intake.SIDECAR_EVIDENCE_KEY]["conflicts"] == expected_conflicts
    assert merged["title"] == expected_title
    assert ("isbn" in merged) is not expected_conflicts


GENERATED_OPF = CALIBRE_OPF.replace(
    "  </metadata>",
    '    <meta name="calibre:title_sort" content="Phylogenesis"/>\n'
    '    <meta name="ebook-server:generated" content="true"/>\n'
    '    <meta name="ebook-server:book_id" content="42"/>\n'
    "  </metadata>",
)


@pytest.mark.django_db(transaction=True)
def test_app_generated_metadata_opf_is_not_sidecar_evidence(
    tmp_path: Path,
    provider: StubProvider,
) -> None:
    """A metadata.opf carrying our own generated marker (#119) is treated as absent."""
    root = tmp_path / "generated"
    book_dir = root / "Alan Dean Foster" / "Phylogenesis (786)"
    book_dir.mkdir(parents=True)
    (book_dir / "metadata.opf").write_text(GENERATED_OPF, encoding="utf-8")
    (book_dir / "Phylogenesis - Alan Dean Foster.txt").write_text("text", encoding="utf-8")

    (job,) = _import_all(root, tmp_path)

    assert isinstance(job.evidence, dict)
    assert intake.SIDECAR_EVIDENCE_KEY not in job.evidence
    assert "isbn" not in job.evidence
    assert job.evidence[intake.TITLE_SOURCE_KEY] == intake.TITLE_SOURCE_FILENAME
    assert provider.isbn_queries == []


def test_generated_marker_detection() -> None:
    assert intake.opf_is_app_generated(GENERATED_OPF.encode("utf-8"))
    assert not intake.opf_is_app_generated(CALIBRE_OPF.encode("utf-8"))
    assert not intake.opf_is_app_generated(
        GENERATED_OPF.replace('content="true"', 'content="false"').encode("utf-8")
    )
