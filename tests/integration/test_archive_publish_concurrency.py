"""Competing writers into one book folder: threads on separate PostgreSQL connections.

Each worker assigns (or reuses) the Book's archive folder under a row lock and then
publishes its output with ``publish_unique``. Every writer must end up with its own
file holding its own bytes; none may overwrite another. Deterministic single-process
versions of these cases are in tests/test_archive_layout.py.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from django.db import connections

from ebook_server.core import archive_layout
from ebook_server.core.models import Author, Book
from ebook_server.storage import LocalFilesystemStorage

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]

WAIT_SECONDS = 20


def _in_thread(target: Callable[[], Any], errors: list[BaseException]) -> threading.Thread:
    """Run ``target`` on its own thread, hence its own database connection."""

    def _run() -> None:
        try:
            target()
        except BaseException as exc:  # pragma: no cover - surfaced by the assertion
            errors.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


@pytest.mark.django_db(transaction=True)
def test_concurrent_derived_outputs_for_one_book_never_overwrite(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    book = Book.objects.create(title="Emma")
    book.authors.add(Author.objects.create(full_name="Jane Austen"))
    workers = 8
    barrier = threading.Barrier(workers)
    published: dict[int, str] = {}
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _writer(index: int) -> Callable[[], None]:
        def _run() -> None:
            source = tmp_path / f"output-{index}.pdf"
            source.write_bytes(f"conversion result {index}".encode())
            own_copy = Book.objects.get(pk=book.pk)  # every worker loads it unassigned
            barrier.wait(timeout=WAIT_SECONDS)
            identifier = archive_layout.publish_derived(
                storage,
                own_copy,
                str(source),
                "output.pdf",
                legacy_namespace=f"books/{book.pk}/converted",
            )
            with lock:
                published[index] = identifier

        return _run

    threads = [_in_thread(_writer(index), errors) for index in range(workers)]
    for thread in threads:
        thread.join(WAIT_SECONDS)

    assert not errors
    assert len(published) == workers
    assert len(set(published.values())) == workers
    book.refresh_from_db()
    assert book.archive_folder == "Jane Austen/Emma"
    for index, identifier in published.items():
        assert identifier.startswith("Jane Austen/Emma/output")
        assert storage.resolve_location(identifier).read_bytes() == (
            f"conversion result {index}".encode()
        )
