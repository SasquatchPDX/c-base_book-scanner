"""Configurable Calibre-style archive layout for archived and derived files (#7)."""

from __future__ import annotations

from pathlib import Path

import pytest
from django.core.exceptions import ImproperlyConfigured
from django.db import IntegrityError, transaction

from ebook_server.core import archive_layout, identification, intake
from ebook_server.core.models import (
    Author,
    Book,
    ImportJob,
    ReviewTask,
    SourceLocation,
)
from ebook_server.storage import (
    LocalFilesystemStorage,
    ReadOnlyFilesystemStorage,
    ReadOnlyStorageError,
    safe_filename,
    safe_path_component,
)


def _candidate(title: str, authors: list[str]) -> identification.IdentificationCandidate:
    return identification.IdentificationCandidate(
        provider_id="stub",
        provider_ref=f"stub:{title}",
        title=title,
        authors=authors,
        isbn10=None,
        isbn13=None,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=0.99,
        raw={},
    )


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalFilesystemStorage:
    storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: storage)
    return storage


def _source_job(tmp_path: Path, name: str, body: str) -> ImportJob:
    root = tmp_path / "source"
    root.mkdir(exist_ok=True)
    path = root / name
    path.write_text(body)
    location, _ = SourceLocation.objects.get_or_create(
        name="src",
        defaults={"root_path": str(root), "transfer_mode": SourceLocation.TransferMode.COPY},
    )
    return intake.create_source_import_job(path, location)


def _book(title: str, *authors: str) -> Book:
    book = Book.objects.create(title=title)
    for name in authors:
        book.authors.add(Author.objects.get_or_create(full_name=name)[0])
    return book


# --- sanitization -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("../../etc/passwd", "_.._etc_passwd"),
        ("..", "Unknown"),
        ("  .hidden. ", "hidden"),
        ('Why? A "Study": <1/2> | *', "Why_ A _Study__ _1_2_ _ _"),
        ("CON", "_CON"),
        ("nul.txt", "_nul.txt"),
        ("Tab\there\x00​", "Tab here"),
        ("", "Unknown"),
    ],
)
def test_safe_path_component_neutralizes_hostile_text(raw: str, expected: str) -> None:
    assert safe_path_component(raw) == expected


def test_safe_path_component_normalizes_unicode_and_caps_bytes() -> None:
    decomposed = "Café"  # "Café" as e + combining accent
    assert safe_path_component(decomposed) == "Café"
    long_value = "ä" * 400  # two UTF-8 bytes each
    capped = safe_path_component(long_value, max_bytes=101)
    assert len(capped.encode("utf-8")) <= 101
    assert capped == "ä" * 50


def test_safe_filename_keeps_extension_and_drops_odd_suffixes() -> None:
    assert safe_filename("My Book.EPUB") == "My Book.epub"
    assert safe_filename("../x/evil.pdf") == "evil.pdf"
    assert safe_filename("archive.tar.gz;rm -rf") == "archive.tar.gz;rm -rf"
    assert safe_filename("PRN.pdf") == "_PRN.pdf"


# --- storage generate_destination ----------------------------------------------------


def test_unique_destination_never_reuses_an_existing_file(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    first = storage.generate_destination("A/B", "Title - Author.epub", unique=True)
    assert first == "A/B/Title - Author.epub"
    (tmp_path / "A" / "B").mkdir(parents=True)
    (tmp_path / first).write_bytes(b"one")
    second = storage.generate_destination("A/B", "Title - Author.epub", unique=True)
    assert second != first
    assert second.startswith("A/B/Title - Author-") and second.endswith(".epub")


@pytest.mark.parametrize("namespace", ["../escape", "/abs", "a/../../b", "a//b"])
def test_generate_destination_rejects_unsafe_namespaces(tmp_path: Path, namespace: str) -> None:
    with pytest.raises(ValueError):
        LocalFilesystemStorage(str(tmp_path)).generate_destination(namespace, "x.txt")


def test_read_only_backend_refuses_unique_destinations_and_pruning(tmp_path: Path) -> None:
    backend = ReadOnlyFilesystemStorage(str(tmp_path))
    with pytest.raises(ReadOnlyStorageError):
        backend.generate_destination("a", "x.txt", unique=True)
    with pytest.raises(ReadOnlyStorageError):
        backend.prune_empty_parents("a/x.txt")


# --- layout configuration ------------------------------------------------------------


@pytest.mark.parametrize(
    "template",
    [
        "/abs/{title}",
        "{author}/../{title}",
        "{author}//{title}",
        "{nope}/{title}",
        "{title!r}",
        "{title:>10}",
        "static/folder",
        "{job_id}/{title}",
        "_unsorted/{title}",
        "a\\{title}",
    ],
)
def test_invalid_layout_templates_are_rejected(settings, template: str) -> None:  # type: ignore[no-untyped-def]
    settings.BOOK_SCANNER_ARCHIVE_LAYOUT = template
    with pytest.raises(ImproperlyConfigured):
        archive_layout.configured_layout()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("layout", "expected"),
    [
        ("author_title", "Isaac Asimov/Foundation"),
        ("calibre", "Isaac Asimov/Foundation ({id})"),
        ("{language}/{year}/{author} - {title}", "en/1951/Isaac Asimov - Foundation"),
    ],
)
def test_book_folder_follows_configured_layout(  # type: ignore[no-untyped-def]
    settings, tmp_path: Path, layout: str, expected: str
) -> None:
    settings.BOOK_SCANNER_ARCHIVE_LAYOUT = layout
    book = _book("Foundation", "Isaac Asimov", "Someone Else")
    book.language = "en"
    book.publication_year = 1951
    book.save()
    folder = archive_layout.book_folder(book, LocalFilesystemStorage(str(tmp_path)))
    assert folder == expected.format(id=book.pk)
    book.refresh_from_db()
    assert book.archive_folder == folder


@pytest.mark.django_db
def test_hostile_metadata_cannot_escape_the_book_folder(tmp_path: Path) -> None:
    book = _book("../../../../etc/cron.d/x", "..")
    folder = archive_layout.book_folder(book, LocalFilesystemStorage(str(tmp_path)))
    assert folder == "Unknown author/_.._.._.._etc_cron.d_x"
    assert ".." not in Path(folder).parts


@pytest.mark.django_db
def test_same_author_and_title_get_distinct_folders(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    first = _book("Foundation", "Isaac Asimov")
    second = _book("FOUNDATION", "isaac asimov")  # same folder on a case-insensitive share
    assert archive_layout.book_folder(first, storage) == "Isaac Asimov/Foundation"
    assert archive_layout.book_folder(second, storage) == f"isaac asimov/FOUNDATION ({second.pk})"


@pytest.mark.django_db
def test_existing_unowned_folder_is_not_mixed_into(tmp_path: Path) -> None:
    (tmp_path / "Isaac Asimov" / "Foundation").mkdir(parents=True)
    book = _book("Foundation", "Isaac Asimov")
    folder = archive_layout.book_folder(book, LocalFilesystemStorage(str(tmp_path)))
    assert folder == f"Isaac Asimov/Foundation ({book.pk})"


@pytest.mark.django_db
def test_archive_folder_unique_constraint_is_case_insensitive() -> None:
    _book("A").__class__.objects.filter(title="A").update(archive_folder="X/Y")
    with pytest.raises(IntegrityError), transaction.atomic():
        Book.objects.create(title="B", archive_folder="x/y")


@pytest.mark.django_db
def test_title_correction_keeps_the_assigned_folder(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    book = _book("Foundaton", "Isaac Asimov")
    assert archive_layout.book_folder(book, storage) == "Isaac Asimov/Foundaton"
    book.title = "Foundation"
    book.save()
    # New files keep joining the existing folder; only a reorganize moves them.
    assert archive_layout.book_folder(book, storage) == "Isaac Asimov/Foundaton"
    assert archive_layout.render_book_folder(book) == "Isaac Asimov/Foundation"


# --- intake ---------------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_matched_import_lands_in_author_title_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: LocalFilesystemStorage
) -> None:
    monkeypatch.setattr(
        identification,
        "resolve_candidates",
        lambda _signals: [_candidate("The Left Hand of Darkness", ["Ursula K. Le Guin"])],
    )
    job = _source_job(tmp_path, "lhod.txt", "Genly Ai arrives on Gethen.")
    intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.COMPLETE
    variant = job.file_variant
    assert variant is not None
    assert variant.storage_identifier == (
        "Ursula K. Le Guin/The Left Hand of Darkness/"
        "The Left Hand of Darkness - Ursula K. Le Guin.txt"
    )
    assert archive.resolve_location(variant.storage_identifier).read_text() == (
        "Genly Ai arrives on Gethen."
    )
    # The staging folder under _unsorted/ is cleaned up, the source is preserved (COPY).
    assert not (Path(archive.root) / archive_layout.PENDING_NAMESPACE).exists()
    assert (tmp_path / "source" / "lhod.txt").exists()


@pytest.mark.django_db(transaction=True)
def test_second_format_of_same_book_joins_its_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: LocalFilesystemStorage
) -> None:
    monkeypatch.setattr(
        identification,
        "resolve_candidates",
        lambda _signals: [_candidate("Dune", ["Frank Herbert"])],
    )
    first = _source_job(tmp_path, "dune.txt", "Arrakis, first edition.")
    second = _source_job(tmp_path, "dune-2.txt", "Arrakis, another text file.")
    intake.process_import_job(str(first.id))
    intake.process_import_job(str(second.id))

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.book_id == second.book_id
    assert first.file_variant is not None and second.file_variant is not None
    one = first.file_variant.storage_identifier
    two = second.file_variant.storage_identifier
    assert one == "Frank Herbert/Dune/Dune - Frank Herbert.txt"
    assert two.startswith("Frank Herbert/Dune/Dune - Frank Herbert-")
    assert archive.resolve_location(one).read_text() == "Arrakis, first edition."
    assert archive.resolve_location(two).read_text() == "Arrakis, another text file."


@pytest.mark.django_db(transaction=True)
def test_reviewed_import_waits_in_unsorted_then_moves_to_book_folder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: LocalFilesystemStorage
) -> None:
    monkeypatch.setattr(identification, "resolve_candidates", lambda _signals: [])
    job = _source_job(tmp_path, "mystery.txt", "An unidentified manuscript.")
    intake.process_import_job(str(job.id))
    job.refresh_from_db()
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.storage_identifier == f"_unsorted/{job.id}/mystery.txt"
    assert archive.exists(job.storage_identifier)

    variant = intake.resolve_import_with_review_data(
        job=job,
        review_task=ReviewTask.objects.get(import_job=job),
        book=_book("The Manuscript", "Anon Ymous"),
    )

    assert variant.storage_identifier == (
        "Anon Ymous/The Manuscript/The Manuscript - Anon Ymous.txt"
    )
    assert archive.resolve_location(variant.storage_identifier).read_text() == (
        "An unidentified manuscript."
    )
    assert not archive.exists(f"_unsorted/{job.id}")


@pytest.mark.django_db(transaction=True)
def test_job_layout_keeps_original_per_import_folders(  # type: ignore[no-untyped-def]
    settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: LocalFilesystemStorage
) -> None:
    settings.BOOK_SCANNER_ARCHIVE_LAYOUT = "job"
    monkeypatch.setattr(
        identification, "resolve_candidates", lambda _signals: [_candidate("Emma", ["Austen"])]
    )
    job = _source_job(tmp_path, "emma.txt", "Emma Woodhouse, handsome, clever, and rich.")
    intake.process_import_job(str(job.id))
    job.refresh_from_db()
    assert job.file_variant is not None
    assert job.file_variant.storage_identifier == f"books/{job.id}/emma.txt"
    assert job.file_variant.book.archive_folder == ""


@pytest.mark.django_db
def test_derived_files_go_into_the_book_folder(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    book = _book("Emma", "Jane Austen")
    cover = archive_layout.derived_destination(
        storage, book, "cover.jpg", legacy_namespace=f"books/{book.pk}/covers"
    )
    converted = archive_layout.derived_destination(
        storage,
        book,
        f"{archive_layout.book_file_stem(book)}.epub",
        legacy_namespace=f"books/{book.pk}/converted",
    )
    assert cover == "Jane Austen/Emma/cover.jpg"
    assert converted == "Jane Austen/Emma/Emma - Jane Austen.epub"


@pytest.mark.django_db
def test_derived_files_keep_legacy_namespace_under_job_layout(  # type: ignore[no-untyped-def]
    settings, tmp_path: Path
) -> None:
    settings.BOOK_SCANNER_ARCHIVE_LAYOUT = "job"
    book = _book("Emma", "Jane Austen")
    destination = archive_layout.derived_destination(
        LocalFilesystemStorage(str(tmp_path)),
        book,
        "cover.jpg",
        legacy_namespace=f"books/{book.pk}/covers",
        legacy_filename="abc-front.jpg",
    )
    assert destination == f"books/{book.pk}/covers/abc-front.jpg"


@pytest.mark.django_db(transaction=True)
def test_pending_move_survives_a_rolled_back_resolution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: LocalFilesystemStorage
) -> None:
    monkeypatch.setattr(identification, "resolve_candidates", lambda _signals: [])
    job = _source_job(tmp_path, "draft.txt", "Pending text.")
    intake.process_import_job(str(job.id))
    job.refresh_from_db()
    pending = job.storage_identifier
    book = _book("Draft", "Writer")

    class Boom(RuntimeError):
        pass

    with pytest.raises(Boom), transaction.atomic():
        archive_layout.place_pending_original(job, book, archive)
        raise Boom

    # The recorded _unsorted path still resolves; only an extra link was added.
    assert archive.resolve_location(pending).read_text() == "Pending text."
    job.refresh_from_db()
    assert job.storage_identifier == pending
