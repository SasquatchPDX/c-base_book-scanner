"""Generated Calibre-compatible metadata.opf in each book's archive folder (#7)."""

from __future__ import annotations

import datetime
import hashlib
import os
from pathlib import Path
from typing import Any

import pytest
from defusedxml import ElementTree as DefusedET
from django.core.management import CommandError, call_command
from django.db import transaction

from ebook_server.core import archive_reorganize, book_opf
from ebook_server.core.archive_reorganize import request_reorganize
from ebook_server.core.book_deletion import delete_book_safely
from ebook_server.core.intake import parse_opf_metadata
from ebook_server.core.models import (
    Author,
    Book,
    Category,
    CoverImage,
    FileVariant,
    Genre,
    Identifier,
    ProcessingJob,
    StorageLocation,
)
from ebook_server.core.storage_registry import default_archive_location
from ebook_server.storage import (
    LocalFilesystemStorage,
    ReadOnlyFilesystemStorage,
    ReadOnlyStorageError,
)

DC = "{http://purl.org/dc/elements/1.1/}"
OPF = "{http://www.idpf.org/2007/opf}"


@pytest.fixture(autouse=True)
def _opf_enabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_WRITE_BOOK_OPF", True)
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_ARCHIVE_LAYOUT", "author_title")
    monkeypatch.setattr(archive_reorganize, "get_queue", lambda: None)


def _archive(tmp_path: Path) -> Path:
    return tmp_path / "library-artifacts"


def _book(title: str, author: str, folder: str = "") -> Book:
    book = Book.objects.create(title=title, archive_folder=folder)
    book.authors.add(Author.objects.get_or_create(full_name=author)[0])
    return book


def _variant(tmp_path: Path, book: Book, identifier: str, payload: bytes = b"epub") -> FileVariant:
    path = _archive(tmp_path) / identifier
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return FileVariant.objects.create(
        book=book,
        format=Path(identifier).suffix.lstrip("."),
        mime_type="application/octet-stream",
        original_filename=Path(identifier).name,
        storage_location=default_archive_location(),
        storage_identifier=identifier,
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
    )


def _opf_path(tmp_path: Path, folder: str) -> Path:
    return _archive(tmp_path) / folder / "metadata.opf"


# --- content ------------------------------------------------------------------------


@pytest.mark.django_db
def test_opf_content_is_calibre_shaped_escaped_and_defusedxml_parsable() -> None:
    book = Book.objects.create(
        title='The <Hobbit> & "Friends" \x01',
        subtitle="There & Back Again",
        publisher="Allen & Unwin",
        publication_year=1937,
        language="en",
        description="<p>A hobbit's tale]]> with a \x0b control char.</p>",
    )
    for name in ["J. R. R. Tolkien", "Christopher Tolkien Jr."]:
        book.authors.add(Author.objects.create(full_name=name))
    Identifier.objects.create(book=book, kind="isbn10", value="0-261-10221-4")
    Identifier.objects.create(
        book=book, kind="isbn13", value="978-0-261-10221-7", normalized_value="9780261102217"
    )
    Identifier.objects.create(book=book, kind="doi", value="10.1000/hobbit")
    Identifier.objects.create(book=book, kind="other", value="mystery")
    book.genres.add(Genre.objects.create(name="Fantasy"))
    book.categories.add(
        Category.objects.create(name="fantasy"), Category.objects.create(name="Kids")
    )

    payload = book_opf.render_book_opf(book, cover_href="cover%20art.jpg")
    root = DefusedET.fromstring(payload)  # well-formed, no entities
    assert root.tag == f"{OPF}package"
    assert root.attrib["version"] == "2.0"
    metadata = root.find(f"{OPF}metadata")
    assert metadata is not None

    assert metadata.findtext(f"{DC}title") == 'The <Hobbit> & "Friends": There & Back Again'
    creators = metadata.findall(f"{DC}creator")
    assert [c.text for c in creators] == ["J. R. R. Tolkien", "Christopher Tolkien Jr."]
    assert [c.attrib[f"{OPF}file-as"] for c in creators] == [
        "Tolkien, J. R. R.",
        "Tolkien, Christopher Jr.",
    ]
    assert {c.attrib[f"{OPF}role"] for c in creators} == {"aut"}
    identifiers = [
        (i.attrib.get(f"{OPF}scheme"), i.text) for i in metadata.findall(f"{DC}identifier")
    ]
    assert identifiers == [
        ("uuid", book_opf.book_uuid(book.pk)),
        ("ISBN", "9780261102217"),
        ("ISBN", "0-261-10221-4"),
        ("DOI", "10.1000/hobbit"),
    ]
    assert root.attrib["unique-identifier"] == "uuid_id"
    assert metadata.findtext(f"{DC}date") == "1937"  # year only: no invented day
    assert metadata.findtext(f"{DC}publisher") == "Allen & Unwin"
    assert metadata.findtext(f"{DC}language") == "en"
    assert metadata.findtext(f"{DC}description") == (
        "<p>A hobbit's tale]]> with a  control char.</p>"
    )
    assert [s.text for s in metadata.findall(f"{DC}subject")] == ["Fantasy", "Kids"]
    metas = {m.attrib["name"]: m.attrib["content"] for m in metadata.findall(f"{OPF}meta")}
    assert metas["calibre:title_sort"] == '<Hobbit> & "Friends": There & Back Again, The'
    assert metas[book_opf.GENERATED_META_NAME] == "true"
    reference = root.find(f"{OPF}guide/{OPF}reference")
    assert reference is not None
    assert reference.attrib == {"type": "cover", "title": "Cover", "href": "cover%20art.jpg"}

    # Intake's own OPF parser reads it back.
    fields = parse_opf_metadata(payload)
    assert fields["authors"] == ["J. R. R. Tolkien", "Christopher Tolkien Jr."]
    assert fields["publisher"] == "Allen & Unwin"
    # Stable across regenerations.
    assert book_opf.render_book_opf(book, cover_href="cover%20art.jpg") == payload


@pytest.mark.django_db
def test_full_date_only_when_known_and_no_empty_elements() -> None:
    book = Book.objects.create(title="Solo", publication_date=datetime.date(2001, 9, 4))
    root = DefusedET.fromstring(book_opf.render_book_opf(book))
    metadata = root.find(f"{OPF}metadata")
    assert metadata is not None
    assert metadata.findtext(f"{DC}date") == "2001-09-04"
    for tag in ("creator", "publisher", "language", "description", "subject"):
        assert metadata.find(f"{DC}{tag}") is None
    assert root.find(f"{OPF}guide") is None


def test_sort_helpers() -> None:
    assert book_opf.title_sort("The Hobbit") == "Hobbit, The"
    assert book_opf.title_sort("A Wizard of Earthsea", "eng") == "Wizard of Earthsea, A"
    assert book_opf.title_sort("Der Hobbit", "de") == "Der Hobbit"
    assert book_opf.title_sort("Theory") == "Theory"
    assert book_opf.author_sort("Isaac Asimov") == "Asimov, Isaac"
    assert book_opf.author_sort("Asimov, Isaac") == "Asimov, Isaac"
    assert book_opf.author_sort("Plato") == "Plato"


@pytest.mark.django_db
def test_generated_opf_is_recognised_and_external_ones_are_not() -> None:
    """The intake sidecar reader must not take our own OPF for external evidence."""
    ours = book_opf.render_book_opf(Book.objects.create(title="Ours"))
    assert book_opf.is_generated_book_opf(ours)
    calibre = (
        b"<?xml version='1.0' encoding='utf-8'?>"
        b'<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        b'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        b"<dc:title>Theirs</dc:title>"
        b'<meta name="calibre:title_sort" content="Theirs"/></metadata></package>'
    )
    assert not book_opf.is_generated_book_opf(calibre)
    hostile = (
        b'<?xml version="1.0"?><!DOCTYPE p [<!ENTITY a "aaaa">]>'
        b'<package><meta name="ebook-server:generated" content="true"/>&a;</package>'
    )
    assert not book_opf.is_generated_book_opf(hostile)
    assert not book_opf.is_generated_book_opf(b"<not xml")


# --- writing and regeneration ---------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_written_into_book_folder_with_cover_and_regenerated_on_metadata_change(
    tmp_path: Path,
) -> None:
    book = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    _variant(tmp_path, book, "Jane Austen/Emma/Emma - Jane Austen.epub")
    opf = _opf_path(tmp_path, "Jane Austen/Emma")
    assert opf.exists()  # the new variant queued the first write
    assert parse_opf_metadata(opf.read_bytes())["title"] == "Emma"
    assert DefusedET.fromstring(opf.read_bytes()).find(f"{OPF}guide") is None

    cover_file = _archive(tmp_path) / "Jane Austen/Emma/cover.jpg"
    cover_file.write_bytes(b"jpeg")
    cover = CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.FRONT,
        storage_location=default_archive_location(),
        storage_identifier="Jane Austen/Emma/cover.jpg",
        mime_type="image/jpeg",
        size_bytes=4,
        sha256=hashlib.sha256(b"jpeg").hexdigest(),
    )
    book.front_cover = cover
    book.save(update_fields=["front_cover"])
    reference = DefusedET.fromstring(opf.read_bytes()).find(f"{OPF}guide/{OPF}reference")
    assert reference is not None and reference.attrib["href"] == "cover.jpg"

    book.title = "Emma (Annotated)"
    book.save()
    assert parse_opf_metadata(opf.read_bytes())["title"] == "Emma (Annotated)"
    book.authors.add(Author.objects.create(full_name="Fiona Stafford"))
    assert parse_opf_metadata(opf.read_bytes())["authors"] == ["Jane Austen", "Fiona Stafford"]
    Identifier.objects.create(book=book, kind="isbn13", value="9780141439587")
    assert parse_opf_metadata(opf.read_bytes())["identifier"] == "9780141439587"
    book.genres.add(Genre.objects.create(name="Romance"))
    assert parse_opf_metadata(opf.read_bytes())["subjects"] == ["Romance"]
    Author.objects.filter(full_name="Fiona Stafford").get().save()  # rename path: no crash
    book.refresh_from_db()
    assert book.opf_dirty_at is None
    # Only metadata.opf and the book's own files: no temporary files left behind.
    assert sorted(p.name for p in opf.parent.iterdir()) == [
        "Emma - Jane Austen.epub",
        "cover.jpg",
        "metadata.opf",
    ]


@pytest.mark.django_db(transaction=True)
def test_irrelevant_saves_do_not_queue_and_bursts_coalesce(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    _variant(tmp_path, book, "Jane Austen/Emma/Emma - Jane Austen.epub")
    queued: list[int] = []
    monkeypatch.setattr("ebook_server.jobs.enqueue", lambda func, book_id: queued.append(book_id))

    book.save(update_fields=["updated_at"])
    assert queued == []

    with transaction.atomic():
        book.title = "Emma!"
        book.save()
        book.authors.add(Author.objects.create(full_name="Second Author"))
        Identifier.objects.create(book=book, kind="isbn13", value="9780141439587")
        book.genres.add(Genre.objects.create(name="Classics"))
        assert queued == []  # nothing before commit
    assert queued == [book.pk]  # one job for the whole burst

    # Still dirty (job not run yet): further edits only move the marker forward.
    book.subtitle = "A Novel"
    book.save()
    assert queued == [book.pk]
    book.refresh_from_db()
    assert book.opf_dirty_at is not None

    assert book_opf.write_book_opf(book.pk) == "written"
    book.refresh_from_db()
    assert book.opf_dirty_at is None
    assert parse_opf_metadata(_opf_path(tmp_path, "Jane Austen/Emma").read_bytes())["title"] == (
        "Emma!: A Novel"
    )


@pytest.mark.django_db(transaction=True)
def test_edit_during_write_is_picked_up_by_another_pass(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    book = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    _variant(tmp_path, book, "Jane Austen/Emma/Emma - Jane Austen.epub")
    real_write_once = book_opf._write_once
    passes: list[str] = []

    def write_once_with_concurrent_edit(book_id: int) -> str:
        outcome = real_write_once(book_id)
        if not passes:
            edited = Book.objects.get(pk=book_id)
            edited.title = "Persuasion"
            edited.save()  # marker moves; the queued job is this one
        passes.append(outcome)
        return outcome

    monkeypatch.setattr(book_opf, "_write_once", write_once_with_concurrent_edit)
    monkeypatch.setattr("ebook_server.jobs.enqueue", lambda func, book_id: None)
    Book.objects.filter(pk=book.pk).update(opf_dirty_at=None)
    book_opf.write_book_opf(book.pk)
    assert len(passes) == 2
    assert parse_opf_metadata(_opf_path(tmp_path, "Jane Austen/Emma").read_bytes())["title"] == (
        "Persuasion"
    )
    assert Book.objects.get(pk=book.pk).opf_dirty_at is None


def test_atomic_overwrite_replaces_whole_file_and_keeps_old_one_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    storage.write_atomic("A/B/metadata.opf", b"old contents that are longer")
    storage.write_atomic("A/B/metadata.opf", b"new")
    assert (tmp_path / "A/B/metadata.opf").read_bytes() == b"new"
    assert os.listdir(tmp_path / "A/B") == ["metadata.opf"]

    def failing_replace(src: str, dst: str) -> None:
        raise OSError("disk went away")

    monkeypatch.setattr("ebook_server.storage.os.replace", failing_replace)
    with pytest.raises(OSError, match="disk went away"):
        storage.write_atomic("A/B/metadata.opf", b"half")
    assert (tmp_path / "A/B/metadata.opf").read_bytes() == b"new"
    assert os.listdir(tmp_path / "A/B") == ["metadata.opf"]  # temp file removed

    with pytest.raises(ValueError, match="escapes"):
        storage.write_atomic("../outside.opf", b"x")
    with pytest.raises(ReadOnlyStorageError):
        ReadOnlyFilesystemStorage(str(tmp_path)).write_atomic("A/B/metadata.opf", b"x")


@pytest.mark.django_db(transaction=True)
def test_foreign_metadata_opf_is_never_overwritten(tmp_path: Path) -> None:
    book = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    foreign = _opf_path(tmp_path, "Jane Austen/Emma")
    foreign.parent.mkdir(parents=True)
    foreign.write_bytes(b"<package><metadata>hand made</metadata></package>")
    _variant(tmp_path, book, "Jane Austen/Emma/Emma - Jane Austen.epub")
    assert book_opf.write_book_opf(book.pk) == "foreign_kept"
    assert foreign.read_bytes() == b"<package><metadata>hand made</metadata></package>"


@pytest.mark.django_db(transaction=True)
def test_read_only_index_in_place_folders_are_never_written(tmp_path: Path) -> None:
    share = tmp_path / "share"
    (share / "Jane Austen/Emma").mkdir(parents=True)
    (share / "Jane Austen/Emma/emma.epub").write_bytes(b"shared")
    location = StorageLocation.objects.create(
        identifier="nas", base_path=str(share), read_only=True
    )
    book = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    FileVariant.objects.create(
        book=book,
        format="epub",
        mime_type="application/epub+zip",
        original_filename="emma.epub",
        storage_location=location,
        storage_identifier="Jane Austen/Emma/emma.epub",
        size_bytes=6,
        sha256=hashlib.sha256(b"shared").hexdigest(),
        source_type=FileVariant.SourceType.NETWORK_SOURCE,
    )
    assert book_opf.write_book_opf(book.pk) == "no_target"
    assert sorted(os.listdir(share / "Jane Austen/Emma")) == ["emma.epub"]
    assert not _opf_path(tmp_path, "Jane Austen/Emma").exists()
    assert Book.objects.get(pk=book.pk).opf_dirty_at is None


@pytest.mark.django_db(transaction=True)
def test_disabled_setting_and_job_layout_write_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_WRITE_BOOK_OPF", False)
    book = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    _variant(tmp_path, book, "Jane Austen/Emma/Emma - Jane Austen.epub")
    assert book_opf.write_book_opf(book.pk) == "disabled"
    assert not _opf_path(tmp_path, "Jane Austen/Emma").exists()

    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_WRITE_BOOK_OPF", True)
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_ARCHIVE_LAYOUT", "job")
    book.title = "Changed"
    book.save()
    assert Book.objects.get(pk=book.pk).opf_dirty_at is None
    assert not _opf_path(tmp_path, "Jane Austen/Emma").exists()
    with pytest.raises(CommandError, match="off"):
        call_command("write_book_opfs")


# --- backfill, reorganize and deletion ------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_backfill_command_dry_run_then_queues_jobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_WRITE_BOOK_OPF", False)
    emma = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    _variant(tmp_path, emma, "Jane Austen/Emma/Emma - Jane Austen.epub")
    persuasion = _book("Persuasion", "Jane Austen", folder="Jane Austen/Persuasion")
    _variant(tmp_path, persuasion, "Jane Austen/Persuasion/Persuasion - Jane Austen.epub")
    Book.objects.create(title="No folder yet")
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_WRITE_BOOK_OPF", True)

    call_command("write_book_opfs", "--dry-run")
    out = capsys.readouterr().out
    assert "2 book(s) would be queued" in out
    assert "Jane Austen/Persuasion" in out
    assert not _opf_path(tmp_path, "Jane Austen/Emma").exists()

    call_command("write_book_opfs", "--book", str(emma.pk))
    assert "queued 1 book(s)" in capsys.readouterr().out
    assert _opf_path(tmp_path, "Jane Austen/Emma").exists()  # inline queue in tests
    assert not _opf_path(tmp_path, "Jane Austen/Persuasion").exists()

    call_command("write_book_opfs")
    assert "queued 2 book(s)" in capsys.readouterr().out
    assert _opf_path(tmp_path, "Jane Austen/Persuasion").exists()

    with pytest.raises(CommandError, match="unknown book"):
        call_command("write_book_opfs", "--book", "999999")


@pytest.mark.django_db(transaction=True)
def test_reorganize_moves_opf_with_the_book_and_deletion_removes_it(tmp_path: Path) -> None:
    book = _book("Emma", "Jane Austen", folder="Jane Austen/Emma")
    _variant(tmp_path, book, "Jane Austen/Emma/Emma - Jane Austen.epub")
    assert _opf_path(tmp_path, "Jane Austen/Emma").exists()
    book.title = "Persuasion"
    book.save()

    plan, _ = request_reorganize(book_ids=[book.pk])
    archive_reorganize.process_reorganize_job(str(plan.job_id))
    plan.refresh_from_db()
    apply, _ = request_reorganize(apply_plan_job=plan)
    archive_reorganize.process_reorganize_job(str(apply.job_id))
    apply.refresh_from_db()
    assert apply.state == ProcessingJob.JobState.SUCCESS, apply.details

    new_opf = _opf_path(tmp_path, "Jane Austen/Persuasion")
    assert parse_opf_metadata(new_opf.read_bytes())["title"] == "Persuasion"
    # The old folder held only our OPF after the move: removed and pruned.
    assert not (_archive(tmp_path) / "Jane Austen/Emma").exists()

    delete_book_safely(Book.objects.get(pk=book.pk))
    assert not (_archive(tmp_path) / "Jane Austen/Persuasion").exists()
