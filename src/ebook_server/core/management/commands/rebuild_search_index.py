from __future__ import annotations

import time
from typing import Any

from django.core.management.base import BaseCommand

from ebook_server.core import search


class Command(BaseCommand):
    help = (
        "Rebuild the library search rows (BookSearchDocument/BookSearchFacet) for every "
        "book. Run once after migration 0032 and nightly; safe to repeat."
    )

    def handle(self, *args: Any, **options: Any) -> None:
        del args, options
        started = time.perf_counter()
        indexed = search.rebuild_search_index()
        elapsed = time.perf_counter() - started
        self.stdout.write(
            self.style.SUCCESS(f"Indexed {indexed} book(s) in {elapsed:.1f} s."),
        )
