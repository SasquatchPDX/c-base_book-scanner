"""Run the reassignment/rollback contract on real PostgreSQL, including FTS (#158)."""

import os
import threading
from collections.abc import Callable, Iterable
from typing import Any

import pytest
from django.db import connection, connections, transaction

from ebook_server.core import search
from ebook_server.core.models import Book, Identifier
from tests.test_search_reassignment import *  # noqa: F403

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires PostgreSQL infrastructure",
    ),
]


@pytest.mark.django_db
def test_postgresql_backend() -> None:
    assert connection.vendor == "postgresql"
    assert isinstance(search.get_search_backend(), search.PostgresSearchBackend)


@pytest.mark.django_db(transaction=True)
def test_overlapping_moves_refresh_the_intermediate_parent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    books = [Book.objects.create(title=title) for title in ("First", "Second", "Third")]
    child = make_child("identifier", books[0])  # noqa: F405
    first_saved = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    first_indexed = threading.Event()
    errors: list[BaseException] = []
    original = search.index_books

    def index_then_release(ids: Iterable[int]) -> int:
        result = original(ids)
        if threading.current_thread().name == "first-move":
            first_indexed.set()
        return result

    monkeypatch.setattr(search, "index_books", index_then_release)

    def first() -> None:
        with transaction.atomic():
            row = Identifier.objects.get(pk=child.pk)
            row.book = books[1]
            row.save(update_fields=["book"])
            first_saved.set()
            assert release_first.wait(15)

    def second() -> None:
        assert first_saved.wait(15)
        # Deliberately start from the stale A instance while A→B is uncommitted.
        row = Identifier.objects.get(pk=child.pk)
        assert row.book_id == books[0].pk

        def interleave(
            execute: Callable[..., Any], sql: str, params: Any, many: bool, context: Any
        ) -> Any:
            if "core_identifier" in sql:
                if sql.lstrip().startswith("UPDATE"):
                    # Ensure B's row really acquired the ISBN before C is saved;
                    # otherwise a fortunate reindex order can hide the defect.
                    assert first_indexed.wait(15)
                elif "FOR UPDATE" in sql:
                    second_started.set()  # the locking read now waits for A→B
            result = execute(sql, params, many, context)
            if "core_identifier" in sql and sql.lstrip().startswith("SELECT"):
                # An unfenced read must finish while A→B is still uncommitted.
                second_started.set()
            return result

        with connection.execute_wrapper(interleave):
            row.book = books[2]
            row.save(update_fields=["book"])

    def run(target: Callable[[], None]) -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    workers = [
        threading.Thread(target=run, args=(fn,), name=name)
        for fn, name in ((first, "first-move"), (second, "second-move"))
    ]
    try:
        for worker in workers:
            worker.start()
        assert second_started.wait(15)
    finally:
        release_first.set()
        for worker in workers:
            worker.join(20)
    assert not any(worker.is_alive() for worker in workers)
    assert errors == []
    assert matching_ids("identifier", child) == {books[2].pk}  # noqa: F405
    assert search.verify_search_index(sample=100).ok
