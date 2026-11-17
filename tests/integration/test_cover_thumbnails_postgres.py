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


@pytest.mark.django_db(transaction=True)
def test_failed_write_never_exposes_partial_bytes_and_the_waiting_publisher_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe 3: worker 1 holds the file lock and is part way through writing a shared
    file when worker 2 publishes the same bytes. Worker 2 waits for the lock; readers
    never see the 3 bytes worker 1 wrote; worker 1's failure leaves nothing behind and
    worker 2 then writes and publishes complete files."""
    shared = _png("#2f4f4f")
    cover_1 = _cover("Writer 1", shared)
    cover_2 = _cover("Writer 2", shared)
    copying = threading.Event()
    fail_copy = threading.Event()
    original_copy = LocalFilesystemStorage._copy_file
    staged: list[Path] = []

    def pausing_copy(src: Path, dest: Path) -> None:
        if threading.current_thread().name != "worker-1":
            original_copy(src, dest)
            return
        staged.append(dest)
        dest.write_bytes(src.read_bytes()[:3])
        copying.set()
        assert fail_copy.wait(WAIT_SECONDS)
        raise OSError("simulated write failure after 3 bytes")

    monkeypatch.setattr(LocalFilesystemStorage, "_copy_file", staticmethod(pausing_copy))
    results_1: list[Any] = []
    worker_1 = _in_thread(
        lambda: thumbnails.generate_cover_thumbnails(cover_1.pk), results_1, name="worker-1"
    )
    assert copying.wait(WAIT_SECONDS)

    root = Path(django_settings.APP_DATA_DIR) / thumbnails.thumbnail_storage_location().base_path
    assert list(root.rglob("*.webp")) == []  # the 3 bytes are only in a temp file

    results_2: list[Any] = []
    worker_2 = _in_thread(
        lambda: thumbnails.generate_cover_thumbnails(cover_2.pk), results_2, name="worker-2"
    )
    _wait_for_lock_waiter()  # worker 2 waits for worker 1's file row
    assert list(root.rglob("*.webp")) == []
    fail_copy.set()
    worker_1.join(WAIT_SECONDS)
    worker_2.join(WAIT_SECONDS)

    assert len(results_1) == 1 and isinstance(results_1[0], OSError)
    assert results_2 == ["created"]
    assert staged and not any(path.exists() for path in staged)
    assert not CoverThumbnail.objects.filter(cover=cover_1).exists()
    _assert_served(cover_2)
    for row in CoverThumbnail.objects.filter(cover=cover_2).select_related("storage_location"):
        assert hashlib.sha256(_path(row).read_bytes()).hexdigest() == row.sha256

    # The queue's retry of worker 1 reuses the verified files.
    monkeypatch.setattr(LocalFilesystemStorage, "_copy_file", staticmethod(original_copy))
    assert thumbnails.generate_cover_thumbnails(cover_1.pk) == "created"
    _assert_served(cover_1)
    for cover in (cover_1, cover_2):
        assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"
