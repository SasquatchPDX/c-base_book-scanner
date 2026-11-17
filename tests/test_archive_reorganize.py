"""Admin-triggered, background, dry-run-first archive reorganize (#7)."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import Client

from ebook_server.core import archive_reorganize
from ebook_server.core.archive_reorganize import ReorganizeError, request_reorganize
from ebook_server.core.models import (
    Author,
    Book,
    CoverImage,
    FileVariant,
    ImportJob,
    ProcessingJob,
    SourceLocation,
    StorageLocation,
)
from ebook_server.core.storage_registry import default_archive_location


@pytest.fixture(autouse=True)
def _app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    # No worker queue: jobs stay QUEUED until run explicitly, like a fresh deployment.
    monkeypatch.setattr(archive_reorganize, "get_queue", lambda: None)
    return tmp_path


def _archive(tmp_path: Path) -> Path:
    return tmp_path / "library-artifacts"


def _book(title: str, author: str) -> Book:
    book = Book.objects.create(title=title)
    book.authors.add(Author.objects.get_or_create(full_name=author)[0])
    return book


def _variant(tmp_path: Path, book: Book, identifier: str, payload: bytes) -> FileVariant:
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


def _run_plan(**scope: object) -> ProcessingJob:
    job, dispatched = request_reorganize(**scope)  # type: ignore[arg-type]
    assert dispatched is False
    assert job.state == ProcessingJob.JobState.QUEUED
    archive_reorganize.process_reorganize_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.SUCCESS, job.details
    return job


def _run_apply(plan: ProcessingJob) -> ProcessingJob:
    job, _ = request_reorganize(apply_plan_job=plan)
    archive_reorganize.process_reorganize_job(str(job.job_id))
    job.refresh_from_db()
    return job


@pytest.mark.django_db(transaction=True)
def test_legacy_job_folders_are_planned_then_moved_with_verification(tmp_path: Path) -> None:
    book = _book("Emma", "Jane Austen")
    epub = _variant(tmp_path, book, "books/1111/emma.epub", b"epub bytes")
    pdf = _variant(tmp_path, book, "books/2222/emma.pdf", b"pdf bytes")
    cover_path = _archive(tmp_path) / f"books/{book.pk}/covers/abc-front.jpg"
    cover_path.parent.mkdir(parents=True)
    cover_path.write_bytes(b"jpeg")
    cover = CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.FRONT,
        storage_location=default_archive_location(),
        storage_identifier=f"books/{book.pk}/covers/abc-front.jpg",
        mime_type="image/jpeg",
        size_bytes=4,
        sha256=hashlib.sha256(b"jpeg").hexdigest(),
    )
    ImportJob.objects.create(
        uploader=get_user_model().objects.create(username="importer"),
        original_filename="emma.epub",
        source_path="/gone/emma.epub",
        storage_location=default_archive_location(),
        storage_identifier="books/1111/emma.epub",
        book=book,
        file_variant=epub,
    )

    plan = _run_plan(book_ids=[book.pk])
    entries = plan.details["plan"]
    assert {(e["kind"], e["to_folder"]) for e in entries} == {
        ("variant", "Jane Austen/Emma"),
        ("cover", "Jane Austen/Emma"),
    }
    assert len(entries) == 3
    # Dry run: nothing moved, no folder claimed.
    assert (_archive(tmp_path) / "books/1111/emma.epub").exists()
    book.refresh_from_db()
    assert book.archive_folder == ""

    applied = _run_apply(plan)
    assert applied.state == ProcessingJob.JobState.SUCCESS, applied.details
    assert applied.details["counts"] == {"moved": 3, "skipped": 0, "failed": 0}
    epub.refresh_from_db()
    pdf.refresh_from_db()
    cover.refresh_from_db()
    assert epub.storage_identifier == "Jane Austen/Emma/Emma - Jane Austen.epub"
    assert pdf.storage_identifier == "Jane Austen/Emma/Emma - Jane Austen.pdf"
    assert cover.storage_identifier == "Jane Austen/Emma/cover.jpg"
    assert (_archive(tmp_path) / epub.storage_identifier).read_bytes() == b"epub bytes"
    assert not (_archive(tmp_path) / "books").exists()  # old copies deleted, empty dirs pruned
    assert ImportJob.objects.get(book=book).storage_identifier == epub.storage_identifier
    book.refresh_from_db()
    assert book.archive_folder == "Jane Austen/Emma"

    with pytest.raises(ReorganizeError, match="already applied"):
        request_reorganize(apply_plan_job=plan)
    assert _run_plan(book_ids=[book.pk]).details["plan"] == []


@pytest.mark.django_db(transaction=True)
def test_corrected_title_moves_only_on_explicit_reorganize(tmp_path: Path) -> None:
    book = _book("Foundaton", "Isaac Asimov")
    Book.objects.filter(pk=book.pk).update(archive_folder="Isaac Asimov/Foundaton")
    variant = _variant(
        tmp_path, book, "Isaac Asimov/Foundaton/Foundaton - Isaac Asimov.epub", b"psychohistory"
    )
    book.title = "Foundation"
    book.save()

    plan = _run_plan(book_ids=[book.pk])
    assert plan.details["plan"][0]["to_folder"] == "Isaac Asimov/Foundation"
    _run_apply(plan)
    variant.refresh_from_db()
    assert variant.storage_identifier == "Isaac Asimov/Foundation/Foundation - Isaac Asimov.epub"
    assert not (_archive(tmp_path) / "Isaac Asimov/Foundaton").exists()


@pytest.mark.django_db(transaction=True)
def test_failed_verification_keeps_the_original(tmp_path: Path) -> None:
    book = _book("Dune", "Frank Herbert")
    variant = _variant(tmp_path, book, "books/9/dune.epub", b"spice")
    plan = _run_plan(book_ids=[book.pk])
    # The archived bytes no longer match the recorded hash.
    (_archive(tmp_path) / "books/9/dune.epub").write_bytes(b"SPICE")

    applied = _run_apply(plan)
    assert applied.state == ProcessingJob.JobState.FAILED
    assert applied.details["counts"]["failed"] == 1
    variant.refresh_from_db()
    assert variant.storage_identifier == "books/9/dune.epub"
    assert (_archive(tmp_path) / "books/9/dune.epub").read_bytes() == b"SPICE"
    assert not (_archive(tmp_path) / "Frank Herbert/Dune/Dune - Frank Herbert.epub").exists()


@pytest.mark.django_db(transaction=True)
def test_entry_changed_since_plan_is_skipped(tmp_path: Path) -> None:
    book = _book("Dune", "Frank Herbert")
    variant = _variant(tmp_path, book, "books/9/dune.epub", b"spice")
    plan = _run_plan(book_ids=[book.pk])
    FileVariant.objects.filter(pk=variant.pk).update(storage_identifier="elsewhere/dune.epub")
    applied = _run_apply(plan)
    assert applied.details["counts"] == {"moved": 0, "skipped": 1, "failed": 0}
    assert (_archive(tmp_path) / "books/9/dune.epub").exists()


@pytest.mark.django_db(transaction=True)
def test_read_only_references_are_never_touched(tmp_path: Path) -> None:
    share = tmp_path / "share"
    (share / "pile").mkdir(parents=True)
    (share / "pile/x.epub").write_bytes(b"shared")
    location = StorageLocation.objects.create(
        identifier="nas", base_path=str(share), read_only=True
    )
    book = _book("Shared", "Some One")
    FileVariant.objects.create(
        book=book,
        format="epub",
        mime_type="application/epub+zip",
        original_filename="x.epub",
        storage_location=location,
        storage_identifier="pile/x.epub",
        size_bytes=6,
        sha256=hashlib.sha256(b"shared").hexdigest(),
        source_type=FileVariant.SourceType.NETWORK_SOURCE,
    )
    plan = _run_plan(book_ids=[book.pk])
    assert plan.details["plan"] == []
    assert plan.details["read_only_skipped"] == 1
    assert (share / "pile/x.epub").read_bytes() == b"shared"


@pytest.mark.django_db
def test_index_in_place_sources_cannot_be_sorted(tmp_path: Path) -> None:
    index = SourceLocation.objects.create(
        name="nas-index",
        root_path=str(tmp_path),
        transfer_mode=SourceLocation.TransferMode.INDEX_IN_PLACE,
    )
    with pytest.raises(ReorganizeError, match="index-in-place"):
        request_reorganize(source_location_ids=[index.pk])
    assert not ProcessingJob.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_source_scope_selects_books_imported_from_that_pile(tmp_path: Path) -> None:
    source = SourceLocation.objects.create(
        name="pile", root_path=str(tmp_path / "pile"), transfer_mode="copy"
    )
    from_pile = _book("Kindred", "Octavia E. Butler")
    other = _book("Beloved", "Toni Morrison")
    variant = _variant(tmp_path, from_pile, "books/1/kindred.epub", b"kindred")
    _variant(tmp_path, other, "books/2/beloved.epub", b"beloved")
    ImportJob.objects.create(
        uploader=get_user_model().objects.create(username="importer"),
        original_filename="kindred.epub",
        source_path=str(tmp_path / "pile/kindred.epub"),
        source_location=source,
        book=from_pile,
        file_variant=variant,
    )
    plan = _run_plan(source_location_ids=[source.pk])
    assert [e["book_id"] for e in plan.details["plan"]] == [from_pile.pk]


@pytest.mark.django_db
def test_job_layout_has_no_tree_to_sort_into(settings) -> None:  # type: ignore[no-untyped-def]
    settings.BOOK_SCANNER_ARCHIVE_LAYOUT = "job"
    with pytest.raises(ReorganizeError, match="per-import"):
        request_reorganize(all_books=True)


@pytest.mark.django_db(transaction=True)
def test_admin_actions_only_queue_jobs(tmp_path: Path) -> None:
    admin = get_user_model().objects.create_superuser("root", "root@example.invalid", "pw")
    client = Client()
    client.force_login(admin)
    book = _book("Emma", "Jane Austen")
    _variant(tmp_path, book, "books/1/emma.epub", b"epub")

    response = client.post(
        "/admin/core/book/",
        {"action": "plan_archive_reorganize", "_selected_action": [book.pk]},
        follow=True,
    )
    assert response.status_code == 200
    plan = ProcessingJob.objects.get(job_type=ProcessingJob.JobType.ARCHIVE_REORGANIZE)
    assert plan.state == ProcessingJob.JobState.QUEUED  # nothing ran in the request
    assert "reorganize_archive --run-queued" in response.content.decode()

    call_command("reorganize_archive", "--run-queued")
    plan.refresh_from_db()
    assert plan.state == ProcessingJob.JobState.SUCCESS
    detail = client.get(f"/admin/core/processingjob/{plan.pk}/change/")
    assert "Jane Austen/Emma" in detail.content.decode()

    client.post(
        "/admin/core/processingjob/",
        {"action": "apply_reorganize_plan", "_selected_action": [plan.pk]},
        follow=True,
    )
    apply_job = ProcessingJob.objects.exclude(pk=plan.pk).get()
    assert apply_job.state == ProcessingJob.JobState.QUEUED
    assert (_archive(tmp_path) / "books/1/emma.epub").exists()  # still not moved


@pytest.mark.django_db(transaction=True)
def test_command_plans_by_default_and_applies_on_request(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    book = _book("Emma", "Jane Austen")
    _variant(tmp_path, book, "books/1/emma.epub", b"epub")
    call_command("reorganize_archive", "--book", str(book.pk))
    out = capsys.readouterr().out
    assert "books/1/emma.epub -> Jane Austen/Emma/Emma - Jane Austen.epub" in out
    assert (_archive(tmp_path) / "books/1/emma.epub").exists()
    plan = ProcessingJob.objects.get()
    call_command("reorganize_archive", "--apply", str(plan.job_id))
    assert (_archive(tmp_path) / "Jane Austen/Emma/Emma - Jane Austen.epub").exists()
    with pytest.raises(CommandError):
        call_command("reorganize_archive")
