from __future__ import annotations

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError

from ebook_server.core.models import (
    Author,
    Book,
    CoverImage,
    FileVariant,
    StorageLocation,
)


@pytest.mark.django_db(transaction=True)
def test_book_can_exist_without_isbn() -> None:
    book = Book.objects.create(title="No ISBN Yet")
    assert book.title == "No ISBN Yet"
    assert book.identifiers.count() == 0


@pytest.mark.django_db(transaction=True)
def test_multiple_authors_can_attach_to_one_book() -> None:
    book = Book.objects.create(title="Multi-author", subtitle="")
    first = Author.objects.create(full_name="Ada Lovelace")
    second = Author.objects.create(full_name="Alan Turing")

    book.authors.set([first, second])

    assert book.authors.count() == 2
    assert set(Author.objects.filter(books=book)) == {first, second}


@pytest.mark.django_db(transaction=True)
def test_multiple_file_variants_can_attach_to_same_book() -> None:
    book = Book.objects.create(title="Same book")
    location = StorageLocation.objects.create(
        identifier="default-store",
        base_path="/srv/books",
    )
    uploader = get_user_model().objects.create_user(username="scanner")

    pdf = FileVariant.objects.create(
        book=book,
        format=FileVariant.VariantFormat.PDF,
        mime_type="application/pdf",
        original_filename="book.pdf",
        storage_location=location,
        storage_identifier="b1/first.pdf",
        size_bytes=123,
        sha256="a" * 64,
        source_type=FileVariant.SourceType.UPLOAD,
        uploader=uploader,
    )
    epub = FileVariant.objects.create(
        book=book,
        format=FileVariant.VariantFormat.EPUB,
        mime_type="application/epub+zip",
        original_filename="book.epub",
        storage_location=location,
        storage_identifier="b1/second.epub",
        size_bytes=234,
        sha256="b" * 64,
        source_type=FileVariant.SourceType.CONVERTED,
        uploader=uploader,
        source_variant=pdf,
    )

    assert set(book.file_variants.all()) == {pdf, epub}
    assert epub.source_variant == pdf


@pytest.mark.django_db(transaction=True)
def test_cover_front_back_is_enforced_per_book() -> None:
    book = Book.objects.create(title="Cover Book")
    location = StorageLocation.objects.create(identifier="cover-store", base_path="/srv/covers")

    CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.FRONT,
        storage_location=location,
        storage_identifier="cover/front.jpg",
        mime_type="image/jpeg",
        size_bytes=10_000,
        sha256="c" * 64,
    )

    CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.BACK,
        storage_location=location,
        storage_identifier="cover/back.jpg",
        mime_type="image/jpeg",
        size_bytes=12_000,
        sha256="d" * 64,
    )

    with pytest.raises(IntegrityError):
        CoverImage.objects.create(
            book=book,
            position=CoverImage.CoverPosition.FRONT,
            storage_location=location,
            storage_identifier="cover/front2.jpg",
            mime_type="image/jpeg",
            size_bytes=11_000,
            sha256="e" * 64,
        )


@pytest.mark.django_db(transaction=True)
def test_filevariant_storage_is_identifier_based() -> None:
    book = Book.objects.create(title="Storage identity")
    location = StorageLocation.objects.create(
        identifier="logical-store",
        base_path="/srv/storage",
    )
    uploader = get_user_model().objects.create_user(username="alice")

    variant = FileVariant.objects.create(
        book=book,
        format=FileVariant.VariantFormat.HTML,
        mime_type="text/html",
        original_filename="index.html",
        storage_location=location,
        storage_identifier="logical/b1/idx.html",
        size_bytes=10,
        sha256="f" * 64,
        source_type=FileVariant.SourceType.GENERATED,
        uploader=uploader,
    )

    assert variant.storage_location.identifier == "logical-store"
    assert variant.storage_identifier == "logical/b1/idx.html"
    assert not variant.storage_identifier.startswith("/")
