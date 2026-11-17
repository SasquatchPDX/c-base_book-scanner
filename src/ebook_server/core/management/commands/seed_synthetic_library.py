"""Fill a development database with a large synthetic library (#28 performance smoke test).

Development and benchmarking only: it refuses to run when
`BOOK_SCANNER_ENVIRONMENT=production`. Rows are bulk-inserted without files
(variants, covers and thumbnails point at storage identifiers that do not
exist), so pages render and queries run exactly as they would, but downloads
do not.
"""

from __future__ import annotations

import random
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction
from django.utils import timezone

from ebook_server.settings import settings as app_settings

from ...models import (
    Author,
    Book,
    Category,
    CoverImage,
    CoverThumbnail,
    FileVariant,
    Genre,
    ImportJob,
    ReviewTask,
    StorageLocation,
    get_or_create_profile,
)
from ...search import rebuild_search_index
from ...thumbnails import thumbnail_storage_location

WORDS = (
    "java python garden anatomy history machine learning design patterns network "
    "security linux kernel electronics radio circuit physics quantum chemistry biology "
    "economics philosophy ethics music theory art drawing photography cooking baking "
    "travel europe berlin hacker space science fiction fantasy dragon empire galaxy "
    "robot android mystery murder detective love story poetry ocean mountain river "
    "forest city winter summer night dawn shadow light fire water stone glass iron "
    "anatomie geschichte maschine garten kochbuch reise stadt nacht licht feuer"
).split()
FIRST = "Ada Alan Grace Ursula Linus Margaret Jürgen Anna Bob Claude Dennis Ken Hedy Emmy".split()
LAST = "Lovelace Turing Hopper Le-Guin Torvalds Hamilton Müller Schmidt Byte Shannon Ritchie Thompson Lamarr Noether".split()  # noqa: E501
FORMATS = ("epub", "pdf", "mobi", "azw3", "djvu", "cbz")
BATCH = 5000


class Command(BaseCommand):
    help = "Insert N synthetic books (with authors, variants, covers) for benchmarks."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument("--books", type=int, default=150_000)
        parser.add_argument("--seed", type=int, default=28)
        parser.add_argument(
            "--no-index", action="store_true", help="Skip rebuild_search_index at the end."
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if app_settings.environment == "production" or not settings.DEBUG and _looks_live():
            raise CommandError("seed_synthetic_library refuses to run against production data")
        rng = random.Random(options["seed"])
        total = options["books"]
        location, _ = StorageLocation.objects.get_or_create(
            identifier="default",
            defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
        )
        thumbs = thumbnail_storage_location()
        genres = [Genre.objects.get_or_create(name=f"Genre {i}")[0] for i in range(60)]
        categories = [Category.objects.get_or_create(name=f"Category {i}")[0] for i in range(40)]
        offset = Author.objects.count()
        author_count = max(total // 4, 1)
        Author.objects.bulk_create(
            [
                Author(
                    full_name=f"{rng.choice(FIRST)} {rng.choice(LAST)} {offset + i}",
                )
                for i in range(author_count)
            ],
            batch_size=BATCH,
            ignore_conflicts=True,
        )
        author_ids = list(Author.objects.values_list("pk", flat=True))
        now = timezone.now()
        done = 0
        while done < total:
            size = min(BATCH, total - done)
            with transaction.atomic():
                self._batch(rng, size, done, now, location, thumbs, author_ids, genres, categories)
            done += size
            self.stdout.write(f"{done}/{total} books")
        self._reviews(rng, now)
        if not options["no_index"]:
            self.stdout.write(f"indexed {rebuild_search_index()} books")

    def _batch(
        self,
        rng: random.Random,
        size: int,
        start: int,
        now: Any,
        location: StorageLocation,
        thumbs: StorageLocation,
        author_ids: list[int],
        genres: list[Genre],
        categories: list[Category],
    ) -> None:
        languages = rng.choices(["en", "de", "fr", "es", "it"], [60, 30, 4, 3, 3], k=size)
        books = Book.objects.bulk_create(
            [
                Book(
                    title=" ".join(rng.choices(WORDS, k=rng.randint(1, 5))).title(),
                    language=languages[i],
                    publication_year=rng.randint(1900, 2025) if rng.random() < 0.9 else None,
                    description=" ".join(rng.choices(WORDS, k=40)),
                )
                for i in range(size)
            ]
        )
        for index, book in enumerate(books):
            # Spread creation times over two years, newest last.
            book.created_at = now - timedelta(minutes=(BATCH * 100 - start - index) * 7)
        Book.objects.bulk_update(books, ["created_at"], batch_size=BATCH)
        author_links: list[Any] = []
        genre_links: list[Any] = []
        category_links: list[Any] = []
        variants: list[FileVariant] = []
        covers: list[CoverImage] = []
        for book in books:
            for author_id in set(rng.choices(author_ids, k=rng.choice((1, 1, 1, 2, 2, 3)))):
                author_links.append(Book.authors.through(book_id=book.pk, author_id=author_id))
            if rng.random() < 0.7:
                genre_links.append(Book.genres.through(book=book, genre=rng.choice(genres)))
            if rng.random() < 0.5:
                category_links.append(
                    Book.categories.through(book=book, category=rng.choice(categories))
                )
            for fmt in set(rng.choices(FORMATS, [40, 30, 15, 5, 5, 5], k=rng.choice((1, 1, 2, 2)))):
                variants.append(
                    FileVariant(
                        book=book,
                        format=fmt,
                        mime_type="application/octet-stream",
                        original_filename=f"synthetic-{book.pk}.{fmt}",
                        storage_location=location,
                        storage_identifier=f"synthetic/{book.pk}.{fmt}",
                        size_bytes=1,
                        sha256=f"{book.pk:032x}{len(variants):032x}",
                        source_type=FileVariant.SourceType.UPLOAD,
                        processing_state=FileVariant.ProcessingState.COMPLETE,
                    )
                )
            if rng.random() < 0.8:
                covers.append(
                    CoverImage(
                        book=book,
                        position=CoverImage.CoverPosition.FRONT,
                        storage_location=location,
                        storage_identifier=f"synthetic/{book.pk}/cover.jpg",
                        mime_type="image/jpeg",
                        size_bytes=1,
                        sha256=f"{book.pk:064x}",
                    )
                )
        Book.authors.through.objects.bulk_create(author_links, batch_size=BATCH)
        Book.genres.through.objects.bulk_create(genre_links, batch_size=BATCH)
        Book.categories.through.objects.bulk_create(category_links, batch_size=BATCH)
        FileVariant.objects.bulk_create(variants, batch_size=BATCH)
        CoverImage.objects.bulk_create(covers, batch_size=BATCH)
        by_book = {cover.book_id: cover for cover in covers}
        for book in books:
            cover = by_book.get(book.pk)
            if cover is not None:
                book.front_cover = cover
        Book.objects.bulk_update(
            [book for book in books if book.pk in by_book], ["front_cover"], batch_size=BATCH
        )
        CoverThumbnail.objects.bulk_create(
            [
                CoverThumbnail(
                    cover=cover,
                    size=size_name,
                    storage_location=thumbs,
                    storage_identifier=f"synthetic/{cover.pk}-{size_name}.webp",
                    sha256=f"{cover.pk:060x}{width:04d}",
                    source_sha256=cover.sha256,
                    width=width,
                    height=width * 4 // 3,
                    size_bytes=1,
                    tool="synthetic",
                    tool_version="0",
                )
                for cover in covers
                for size_name, width in (("small", 120), ("medium", 240))
            ],
            batch_size=BATCH,
        )

    def _reviews(self, rng: random.Random, now: Any) -> None:
        uploader = get_or_create_profile("synthetic-seeder").user
        book_ids = list(Book.objects.order_by("?").values_list("pk", flat=True)[:200])
        for book_id in book_ids:
            job = ImportJob.objects.create(
                source_type=ImportJob.SourceType.UPLOAD,
                state=ImportJob.State.COMPLETE,
                uploader=uploader,
                source_path="/synthetic/review.epub",
                original_filename="review.epub",
                normalized_filename="review.epub",
                detected_format="epub",
                mime_type="application/epub+zip",
            )
            ReviewTask.objects.create(
                import_job=job,
                source_filename="review.epub",
                state=ReviewTask.State.RESOLVED,
                decision=ReviewTask.Decision.ACCEPT_CANDIDATE,
                resolved_book_id=book_id,
                decided_at=now - timedelta(minutes=rng.randint(1, 100_000)),
            )


def _looks_live() -> bool:
    """Extra guard outside DEBUG: refuse when the library already holds real imports."""
    return ImportJob.objects.exclude(source_path__startswith="/synthetic/").exists()
