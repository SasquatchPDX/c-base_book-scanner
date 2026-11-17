from __future__ import annotations

import argparse
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from ebook_server.core import search


class Command(BaseCommand):
    help = (
        "Check that every book has a current library search row: reports missing and "
        "orphaned rows, and rows that drifted from their book in a random sample."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--sample",
            type=int,
            default=500,
            help="Number of indexed books to rebuild in memory and compare (default 500).",
        )
        parser.add_argument(
            "--fix",
            action="store_true",
            help="Reindex the missing and drifted books that were found.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        report = search.verify_search_index(sample=max(int(options["sample"]), 0))
        self.stdout.write(
            f"missing={len(report.missing_book_ids)} orphaned={report.orphan_count} "
            f"sampled={report.sampled} drifted={len(report.drifted_book_ids)}"
        )
        if report.ok:
            self.stdout.write(self.style.SUCCESS("Search index is consistent."))
            return
        if options["fix"]:
            fixed = search.index_books([*report.missing_book_ids, *report.drifted_book_ids])
            if report.orphan_count:
                search.rebuild_search_index()
            search.invalidate_cached_counts()
            self.stdout.write(self.style.SUCCESS(f"Reindexed {fixed} book(s)."))
            return
        raise CommandError(
            "Search index is out of date; run with --fix or `manage.py rebuild_search_index`."
        )
