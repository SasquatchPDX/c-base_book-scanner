"""Reparenting indexed records refreshes both books, only after commit (#158)."""

from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.db import connection, transaction
from django.test.utils import CaptureQueriesContext

from ebook_server.core import search, thumbnails
from ebook_server.core.models import (
    Book,
    BookGenre,
    BookSearchDocument,
    CoverImage,
    FileVariant,
    Genre,
    Identifier,
    ScanJob,
    StorageLocation,
)

ISBN = "9783161484100"
KINDS = ("identifier", "variant", "cover", "genre", "scan_job")


@pytest.fixture(autouse=True)
def clean_reassignment_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    cache.clear()
    search._pending.book_ids.clear()
    monkeypatch.setattr(thumbnails, "_enqueue_after_commit", lambda cover_id: None)
    yield
    search._pending.book_ids.clear()
    cache.clear()


def make_child(kind: str, book: Book) -> Any:
    if kind == "identifier":
        return Identifier.objects.create(book=book, kind="isbn13", value=ISBN)
    if kind == "genre":
        return BookGenre.objects.create(book=book, genre=Genre.objects.create(name="Solarpunk"))
    if kind == "scan_job":
        return ScanJob.objects.create(book=book, state=ScanJob.State.CAPTURED)
    location = StorageLocation.objects.create(identifier="reassignment", base_path="archive")
    if kind == "cover":
        return CoverImage.objects.create(
            book=book,
            position="front",
            storage_location=location,
            storage_identifier="cover.jpg",
            mime_type="image/jpeg",
            size_bytes=1,
            sha256="a" * 64,
        )
    return FileVariant.objects.create(
        book=book,
        format="epub",
        mime_type="application/epub+zip",
        original_filename="source.epub",
        storage_location=location,
        storage_identifier="source.epub",
        size_bytes=1,
        sha256="b" * 64,
        source_type=FileVariant.SourceType.UPLOAD,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )


def matching_ids(kind: str, child: Any) -> set[int]:
    # The portable legacy backend only searches titles/authors; ISBN full-text
    # search belongs to PostgreSQL. Its index is checked below by the verifier.
    if kind == "identifier" and connection.vendor != "postgresql":
        return set(Identifier.objects.filter(value=ISBN).values_list("book_id", flat=True))
    request = {
        "identifier": search.SearchRequest(query=ISBN),
        "variant": search.SearchRequest(book_format="epub"),
        "genre": search.SearchRequest(genre_id=getattr(child, "genre_id", None)),
    }.get(kind, search.SearchRequest())
    return set(search.get_search_backend().search(request).book_ids)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", KINDS)
def test_reassignment_indexes_both_parents_after_commit(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = Book.objects.create(title="First")
    second = Book.objects.create(title="Second")
    child = make_child(kind, first)
    before = matching_ids(kind, child)
    assert before == (
        {second.pk}
        if kind == "scan_job"
        else {first.pk, second.pk}
        if kind == "cover"
        else {first.pk}
    )
    indexed: list[set[int]] = []
    original = search.index_books

    def recording(ids: Any) -> int:
        indexed.append(set(ids))
        return original(ids)

    monkeypatch.setattr(search, "index_books", recording)
    documents = list(BookSearchDocument.objects.order_by("pk").values())
    with transaction.atomic():
        child.book = second
        child.save(update_fields=["book"])
        assert indexed == []
        assert list(BookSearchDocument.objects.order_by("pk").values()) == documents
    assert indexed == [{first.pk, second.pk}]
    assert matching_ids(kind, child) == (
        {first.pk}
        if kind == "scan_job"
        else {first.pk, second.pk}
        if kind == "cover"
        else {second.pk}
    )
    assert search.verify_search_index(sample=100).ok


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("kind", KINDS)
def test_rolled_back_reassignment_keeps_original_index(
    kind: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = Book.objects.create(title="First")
    second = Book.objects.create(title="Second")
    child = make_child(kind, first)
    before = list(BookSearchDocument.objects.order_by("pk").values())
    calls: list[set[int]] = []
    original = search.index_books

    def recording(ids: Any) -> int:
        calls.append(set(ids))
        return original(ids)

    monkeypatch.setattr(search, "index_books", recording)
    with pytest.raises(RuntimeError, match="rollback"):
        with transaction.atomic():
            child.book = second
            child.save(update_fields=["book"])
            raise RuntimeError("rollback")
    child.refresh_from_db()
    assert child.book_id == first.pk
    assert calls == []
    assert list(BookSearchDocument.objects.order_by("pk").values()) == before
    assert search.verify_search_index(sample=100).ok
    if kind == "identifier":
        assert matching_ids(kind, child) == {first.pk}
    # Reuse the same instance after rollback: previous-parent tracking must be fresh.
    child.book = second
    child.save(update_fields=["book"])
    assert calls == [{first.pk, second.pk}]
    assert search.verify_search_index(sample=100).ok


@pytest.mark.django_db(transaction=True)
def test_two_moves_in_one_transaction_refresh_every_parent_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    books = [Book.objects.create(title=title) for title in ("First", "Second", "Third")]
    child = make_child("identifier", books[0])
    calls: list[set[int]] = []
    original = search.index_books

    def recording(ids: Any) -> int:
        calls.append(set(ids))
        return original(ids)

    monkeypatch.setattr(search, "index_books", recording)
    with transaction.atomic():
        for book in books[1:]:
            child.book = book
            child.save(update_fields=["book"])
    assert calls == [{book.pk for book in books}]
    assert matching_ids("identifier", child) == {books[2].pk}
    assert search.verify_search_index(sample=100).ok


@pytest.mark.django_db(transaction=True)
def test_detaching_and_reattaching_a_scan_job_updates_library_visibility() -> None:
    book = Book.objects.create(title="Unfinished scan")
    job = make_child("scan_job", book)
    assert not BookSearchDocument.objects.filter(book=book).exists()
    job.book = None
    job.save(update_fields=["book"])
    assert BookSearchDocument.objects.filter(book=book).exists()
    assert search.verify_search_index(sample=100).ok
    job.book = book
    job.save(update_fields=["book"])
    assert not BookSearchDocument.objects.filter(book=book).exists()
    assert search.verify_search_index(sample=100).ok


@pytest.mark.django_db(transaction=True)
def test_saving_unrelated_fields_does_not_read_the_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    book = Book.objects.create(title="Unchanged parent")
    link = make_child("genre", book)
    monkeypatch.setattr(search, "index_books", lambda ids: 0)
    link.source = BookGenre.Source.REVIEWER
    with CaptureQueriesContext(connection) as queries:
        link.save(update_fields=iter(["source"]))
    assert len(queries) == 1
    assert queries[0]["sql"].lstrip().startswith("UPDATE")
    link.refresh_from_db()
    assert link.source == BookGenre.Source.REVIEWER
