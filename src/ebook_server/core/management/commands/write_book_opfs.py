"""Queue regeneration of the generated ``metadata.opf`` in book folders (backfill)."""

from __future__ import annotations

from typing import Any

from django.core.management.base import BaseCommand, CommandError, CommandParser
from django.db import transaction

from ...book_opf import mark_book_opf_dirty, opf_writing_enabled
from ...models import Book


class Command(BaseCommand):
    help = (
        "Queue one background job per book to (re)write the Calibre-compatible "
        "metadata.opf in its archive folder. Use once after upgrading, or after "
        "restoring a share. Books without an archive folder are skipped; read-only "
        "(index-in-place) locations are never written. Nothing is written here: the "
        "worker writes each file."
    )

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--book",
            type=int,
            action="append",
            default=[],
            metavar="ID",
            help="Only this book (repeatable). Default: every book with an archive folder.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List the books that would be queued; queue nothing.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        if not opf_writing_enabled():
            raise CommandError(
                "metadata.opf writing is off: BOOK_SCANNER_WRITE_BOOK_OPF is false or "
                "BOOK_SCANNER_ARCHIVE_LAYOUT is the per-import 'job' layout",
            )
        books = Book.objects.exclude(archive_folder="").order_by("pk")
        if options["book"]:
            requested = sorted(set(options["book"]))
            books = books.filter(pk__in=requested)
            missing = set(requested) - set(
                Book.objects.filter(pk__in=requested).values_list("pk", flat=True)
            )
            if missing:
                raise CommandError(f"unknown book id(s): {sorted(missing)}")
        book_ids = list(books.values_list("pk", flat=True))
        if options["dry_run"]:
            for book_id, folder in books.values_list("pk", "archive_folder"):
                self.stdout.write(f"  book {book_id}: {folder}")
            self.stdout.write(f"dry run: {len(book_ids)} book(s) would be queued")
            return
        queued = 0
        # Batches keep each transaction (and its on-commit dispatch) small.
        for start in range(0, len(book_ids), 500):
            with transaction.atomic():
                queued += len(mark_book_opf_dirty(book_ids[start : start + 500], force=True))
        self.stdout.write(f"queued {queued} book(s) for metadata.opf regeneration")
