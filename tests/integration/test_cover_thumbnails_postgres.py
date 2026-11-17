"""Cover thumbnail publication and collection under real concurrency (#28, PR #121).

Threads on separate PostgreSQL connections, paused at chosen points with events
so each interleaving is deterministic. Single-connection versions of the same
probes are in tests/test_cover_thumbnails.py.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from collections.abc import Callable, Iterator
from io import BytesIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.db import connection, connections
from django.test import Client
from PIL import Image

from ebook_server.core import thumbnails
from ebook_server.core.models import (
    Book,
    CoverImage,
    CoverThumbnail,
    CoverThumbnailFile,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]

WAIT_SECONDS = 20


@pytest.fixture(autouse=True)
def _storage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    monkeypatch.setattr(django_settings, "APP_DATA_DIR", tmp_path)
    # Run every job explicitly: no automatic job for each saved cover.
    monkeypatch.setattr(thumbnails, "_enqueue_after_commit", lambda cover_id: None)
    yield tmp_path


def _in_thread(
    target: Callable[[], Any], results: list[Any], *, name: str = "worker"
) -> threading.Thread:
    """Run ``target`` on its own thread, hence its own database connection."""

    def _run() -> None:
        try:
            results.append(target())
        except BaseException as exc:  # surfaced by the assertions
            results.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=_run, name=name, daemon=True)
    thread.start()
    return thread


def _png(color: str) -> bytes:
    output = BytesIO()
    Image.new("RGB", (600, 800), color).save(output, format="PNG")
    return output.getvalue()


def _cover(title: str, payload: bytes) -> CoverImage:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    book = Book.objects.create(title=title)
    storage = LocalFilesystemStorage(str(Path(django_settings.APP_DATA_DIR) / "library-artifacts"))
    identifier = f"books/{book.pk}/cover.png"
    path = storage.resolve_location(identifier)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    cover = CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.FRONT,
        storage_location=location,
        storage_identifier=identifier,
        mime_type="image/png",
        size_bytes=len(payload),
        sha256=hashlib.sha256(payload).hexdigest(),
    )
    book.front_cover = cover
    book.save(update_fields=["front_cover"])
    return cover


def _replace_cover_bytes(cover: CoverImage, payload: bytes) -> str:
    path = Path(django_settings.APP_DATA_DIR) / "library-artifacts" / cover.storage_identifier
    path.write_bytes(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    CoverImage.objects.filter(pk=cover.pk).update(sha256=sha256, size_bytes=len(payload))
    return sha256


def _path(row: CoverThumbnail) -> Path:
    return (
        Path(django_settings.APP_DATA_DIR) / row.storage_location.base_path / row.storage_identifier
    )


def _assert_served(cover: CoverImage) -> None:
    profile = get_or_create_profile("pg-thumbnail-reader")
    client = Client()
    client.force_login(profile.user)
    rows = list(CoverThumbnail.objects.filter(cover=cover).select_related("storage_location"))
    assert len(rows) == 2
    for row in rows:
        assert _path(row).exists()
        response = client.get(thumbnails.thumbnail_url(row))
        assert response.status_code == 200
        body = b"".join(response.streaming_content)  # type: ignore[attr-defined]
        assert hashlib.sha256(body).hexdigest() == row.sha256


def _wait_for_lock_waiter() -> None:
    """Block until another connection of this database waits on a row lock."""
    deadline = time.monotonic() + WAIT_SECONDS
    while time.monotonic() < deadline:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
            if cursor.fetchone()[0]:
                return
        time.sleep(0.05)
    raise AssertionError("no connection started waiting for the lock")


@pytest.mark.django_db(transaction=True)
def test_old_worker_on_another_connection_publishes_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe 1: the old worker renders, the newer cover's job publishes, the old resumes."""
    cover = _cover("Racing Covers", _png("#111111"))
    rendered = threading.Event()
    resume = threading.Event()
    original_render = thumbnails.render_thumbnails

    def pausing_render(payload: bytes) -> list[Any]:
        result = original_render(payload)
        if threading.current_thread().name == "old-worker":
            rendered.set()
            assert resume.wait(WAIT_SECONDS)
        return result

    monkeypatch.setattr(thumbnails, "render_thumbnails", pausing_render)
    results: list[Any] = []
    old_worker = _in_thread(
        lambda: thumbnails.generate_cover_thumbnails(cover.pk), results, name="old-worker"
    )
    assert rendered.wait(WAIT_SECONDS)

    new_sha = _replace_cover_bytes(cover, _png("#eeeeee"))
    assert thumbnails.generate_cover_thumbnails(cover.pk) == "created"
    resume.set()
    old_worker.join(WAIT_SECONDS)

    assert results == ["stale"]
    assert set(
        CoverThumbnail.objects.filter(cover=cover).values_list("source_sha256", flat=True)
    ) == {new_sha}
    _assert_served(cover)


@pytest.mark.django_db(transaction=True)
def test_publisher_waits_for_collection_and_rewrites_the_collected_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe 2: collection holds a shared file's lock while B publishes a reference to it."""
    shared = _png("#445566")
    cover_a = _cover("Cover A", shared)
    cover_b = _cover("Cover B", shared)
    assert thumbnails.generate_cover_thumbnails(cover_a.pk) == "created"
    shared_paths = [_path(row) for row in CoverThumbnail.objects.filter(cover=cover_a)]
    # Cover A moves to new bytes; the shared files are marked, not deleted.
    _replace_cover_bytes(cover_a, _png("#998877"))
    assert thumbnails.generate_cover_thumbnails(cover_a.pk) == "created"
    assert all(path.exists() for path in shared_paths)

    collector_holds_lock = threading.Event()
    release_collector = threading.Event()
    original_references = thumbnails._references
    paused: list[bool] = []

    def pausing_references(location_id: Any, identifier: Any) -> Any:
        if threading.current_thread().name == "collector" and not paused:
            paused.append(True)
            collector_holds_lock.set()
            assert release_collector.wait(WAIT_SECONDS)
        return original_references(location_id, identifier)

    monkeypatch.setattr(thumbnails, "_references", pausing_references)
    collector_results: list[Any] = []
    collector = _in_thread(
        lambda: thumbnails.collect_unreferenced_files(grace_seconds=0),
        collector_results,
        name="collector",
    )
    assert collector_holds_lock.wait(WAIT_SECONDS)

    publisher_results: list[Any] = []
    publisher = _in_thread(
        lambda: thumbnails.generate_cover_thumbnails(cover_b.pk), publisher_results
    )
    _wait_for_lock_waiter()  # B blocks on the file row the collector holds
    release_collector.set()
    collector.join(WAIT_SECONDS)
    publisher.join(WAIT_SECONDS)

    assert publisher_results == ["created"]
    report = collector_results[0]
    assert isinstance(report, thumbnails.CollectionReport)
    assert report.deleted >= 1  # it deleted at least the file it held
    assert all(path.exists() for path in shared_paths)  # B wrote them again
    _assert_served(cover_b)
    _assert_served(cover_a)
    assert thumbnails.generate_cover_thumbnails(cover_b.pk) == "current"
    assert not CoverThumbnailFile.objects.filter(
        storage_identifier__in=CoverThumbnail.objects.values("storage_identifier"),
        unreferenced_since__isnull=False,
    ).exists()
    assert thumbnails.collect_unreferenced_files(grace_seconds=0).deleted == 0
