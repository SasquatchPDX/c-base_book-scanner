"""Cover thumbnails (#28 phase 4): background job, backfill, serving and cache headers."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from datetime import timedelta
from io import BytesIO, StringIO
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.core.cache import cache
from django.core.management import call_command
from django.db import connection
from django.test import Client, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from PIL import Image

from ebook_server import jobs
from ebook_server.core import thumbnails
from ebook_server.core.models import (
    AppUserProfile,
    Book,
    CoverImage,
    CoverThumbnail,
    CoverThumbnailFile,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

OnCommit = Callable[..., AbstractContextManager[list[Callable[[], Any]]]]
HTML = {"Accept": "text/html"}


@pytest.fixture(autouse=True)
def _storage(tmp_path: Path) -> Iterator[Path]:
    original = django_settings.APP_DATA_DIR
    django_settings.APP_DATA_DIR = tmp_path
    cache.clear()
    yield tmp_path
    django_settings.APP_DATA_DIR = original
    cache.clear()


def _png(width: int = 600, height: int = 800, color: str = "#336699") -> bytes:
    output = BytesIO()
    Image.new("RGB", (width, height), color).save(output, format="PNG")
    return output.getvalue()


def _cover(book: Book, payload: bytes, *, name: str = "cover.png") -> CoverImage:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage = LocalFilesystemStorage(str(Path(django_settings.APP_DATA_DIR) / "library-artifacts"))
    identifier = f"books/{book.pk}/{name}"
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


def _client(username: str = "thumb-reader") -> tuple[Client, AppUserProfile]:
    profile = get_or_create_profile(username)
    client = Client()
    client.force_login(profile.user)
    return client, profile


def _thumbnail_path(thumbnail: CoverThumbnail) -> Path:
    root = Path(django_settings.APP_DATA_DIR) / thumbnail.storage_location.base_path
    return root / thumbnail.storage_identifier


# --------------------------------------------------------------------- job


@pytest.mark.django_db
def test_job_renders_small_and_medium_webp_with_provenance() -> None:
    book = Book.objects.create(title="Thumbnailed")
    cover = _cover(book, _png())

    assert thumbnails.generate_cover_thumbnails(cover.pk) == "created"

    rows = {row.size: row for row in CoverThumbnail.objects.filter(cover=cover)}
    assert set(rows) == {"small", "medium"}
    assert (rows["small"].width, rows["small"].height) == (120, 160)
    assert (rows["medium"].width, rows["medium"].height) == (240, 320)
    for row in rows.values():
        assert row.source_sha256 == cover.sha256
        assert row.mime_type == "image/webp"
        assert row.tool == "pillow-webp" and row.tool_version.startswith("Pillow ")
        assert row.storage_location.identifier == "cover-thumbnails"
        assert row.storage_identifier == f"{row.sha256[:2]}/{row.sha256}.webp"
        payload = _thumbnail_path(row).read_bytes()
        assert hashlib.sha256(payload).hexdigest() == row.sha256
        assert len(payload) == row.size_bytes
        with Image.open(BytesIO(payload)) as image:
            assert image.format == "WEBP"
            assert image.width <= 240
    # The original cover is untouched.
    assert CoverImage.objects.get(pk=cover.pk).sha256 == hashlib.sha256(_png()).hexdigest()


@pytest.mark.django_db
def test_job_is_idempotent_and_refreshes_when_the_cover_changes() -> None:
    book = Book.objects.create(title="Changing Cover")
    cover = _cover(book, _png(color="#112233"))
    thumbnails.generate_cover_thumbnails(cover.pk)
    old = {row.size: row for row in CoverThumbnail.objects.filter(cover=cover)}

    assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"

    replacement = _png(color="#ddeeff")
    storage_path = (
        Path(django_settings.APP_DATA_DIR) / "library-artifacts" / cover.storage_identifier
    )
    storage_path.write_bytes(replacement)
    CoverImage.objects.filter(pk=cover.pk).update(sha256=hashlib.sha256(replacement).hexdigest())

    assert thumbnails.generate_cover_thumbnails(cover.pk) == "created"
    new = {row.size: row for row in CoverThumbnail.objects.filter(cover=cover)}
    assert {row.sha256 for row in new.values()}.isdisjoint(row.sha256 for row in old.values())
    assert CoverThumbnail.objects.filter(cover=cover).count() == 2
    # Replaced files are never deleted inline: they are marked, and collection removes
    # them only after the grace period.
    assert all(_thumbnail_path(row).exists() for row in old.values())
    assert all(_thumbnail_path(row).exists() for row in new.values())
    old_files = CoverThumbnailFile.objects.filter(
        storage_identifier__in=[row.storage_identifier for row in old.values()]
    )
    assert old_files.count() == 2
    assert all(record.unreferenced_since is not None for record in old_files)
    assert thumbnails.collect_unreferenced_files().deleted == 0  # within the grace period
    assert thumbnails.collect_unreferenced_files(grace_seconds=0).deleted == 2
    assert not any(_thumbnail_path(row).exists() for row in old.values())
    assert all(_thumbnail_path(row).exists() for row in new.values())
    assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"


@pytest.mark.django_db
def test_unreadable_cover_fails_softly_and_missing_cover_is_reported() -> None:
    book = Book.objects.create(title="Broken")
    cover = _cover(book, b"not an image", name="broken.png")

    assert thumbnails.generate_cover_thumbnails(cover.pk) == "failed"
    assert not CoverThumbnail.objects.exists()
    assert thumbnails.generate_cover_thumbnails(999_999) == "missing"


@pytest.mark.django_db
def test_saving_a_front_cover_queues_the_job_after_commit(
    django_capture_on_commit_callbacks: OnCommit, monkeypatch: pytest.MonkeyPatch
) -> None:
    queued: list[tuple[Any, ...]] = []
    monkeypatch.setattr(jobs, "enqueue", lambda func, *args, **kwargs: queued.append(args))
    book = Book.objects.create(title="Queued")

    with django_capture_on_commit_callbacks(execute=True):
        cover = _cover(book, _png())
    assert queued == [(cover.pk,)]

    with django_capture_on_commit_callbacks(execute=True):
        CoverImage.objects.create(
            book=book,
            position=CoverImage.CoverPosition.BACK,
            storage_location=cover.storage_location,
            storage_identifier="back.png",
            mime_type="image/png",
            size_bytes=1,
            sha256="0" * 64,
        )
    assert queued == [(cover.pk,)]  # back covers do not appear in lists


@pytest.mark.django_db
def test_enqueue_failure_never_breaks_the_cover_write(
    django_capture_on_commit_callbacks: OnCommit, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken_queue(*args: Any, **kwargs: Any) -> None:
        raise ConnectionError("redis down")

    monkeypatch.setattr(jobs, "enqueue", broken_queue)
    book = Book.objects.create(title="Redis Down")
    with django_capture_on_commit_callbacks(execute=True):
        _cover(book, _png())
    assert CoverImage.objects.filter(book=book).exists()


@pytest.mark.django_db
def test_deleting_the_book_removes_thumbnail_rows_and_later_the_files(
    django_capture_on_commit_callbacks: OnCommit,
) -> None:
    book = Book.objects.create(title="Gone Soon")
    cover = _cover(book, _png())
    thumbnails.generate_cover_thumbnails(cover.pk)
    paths = [_thumbnail_path(row) for row in CoverThumbnail.objects.all()]
    assert all(path.exists() for path in paths)

    with django_capture_on_commit_callbacks(execute=True):
        book.delete()

    assert not CoverThumbnail.objects.exists()
    assert all(path.exists() for path in paths)  # kept for the grace period
    assert CoverThumbnailFile.objects.filter(unreferenced_since__isnull=False).count() == 2

    out = StringIO()
    call_command("collect_cover_thumbnails", "--grace-seconds", "0", stdout=out)
    assert "deleted: 2" in out.getvalue()
    assert not any(path.exists() for path in paths)
    assert not CoverThumbnailFile.objects.exists()


@pytest.mark.django_db
def test_backfill_command_generates_missing_thumbnails_once() -> None:
    covers = [_cover(Book.objects.create(title=f"Backfill {index}"), _png()) for index in range(3)]
    broken = _cover(Book.objects.create(title="Backfill broken"), b"nope", name="x.png")

    first = StringIO()
    call_command("generate_cover_thumbnails", stdout=first)
    assert "created: 3" in first.getvalue() and "failed: 1" in first.getvalue()
    assert CoverThumbnail.objects.filter(cover__in=covers).count() == 6

    second = StringIO()
    call_command("generate_cover_thumbnails", stdout=second)
    # Only the undecodable cover is still pending.
    assert "failed: 1" in second.getvalue() and "created" not in second.getvalue()
    assert list(thumbnails.covers_needing_thumbnails().values_list("pk", flat=True)) == [broken.pk]

    forced = StringIO()
    call_command("generate_cover_thumbnails", "--force", "--limit", "2", stdout=forced)
    assert "created: 2" in forced.getvalue()


# ------------------------------------------------------------------ serving


def _generated(title: str = "Served") -> tuple[Book, CoverThumbnail]:
    book = Book.objects.create(title=title)
    cover = _cover(book, _png())
    thumbnails.generate_cover_thumbnails(cover.pk)
    return book, CoverThumbnail.objects.get(cover=cover, size="medium")


@pytest.mark.django_db
def test_thumbnail_is_served_with_immutable_cache_headers_and_etag() -> None:
    _book, thumbnail = _generated()
    client, _profile = _client()
    url = thumbnails.thumbnail_url(thumbnail)
    assert url == f"/library/covers/{thumbnail.sha256}/medium.webp"

    response = client.get(url)

    assert response.status_code == 200
    assert response["Content-Type"] == "image/webp"
    assert response["Cache-Control"] == "private, max-age=31536000, immutable"
    assert response["ETag"] == f'"{thumbnail.sha256}"'
    assert response["X-Content-Type-Options"] == "nosniff"
    body = b"".join(response.streaming_content)  # type: ignore[attr-defined]
    assert hashlib.sha256(body).hexdigest() == thumbnail.sha256

    revalidated = client.get(url, headers={"If-None-Match": f'"{thumbnail.sha256}"'})
    assert revalidated.status_code == 304
    assert revalidated["Cache-Control"] == "private, max-age=31536000, immutable"


@pytest.mark.django_db
def test_thumbnail_x_accel_redirect_hands_the_file_to_nginx() -> None:
    _book, thumbnail = _generated()
    client, _profile = _client()

    with override_settings(
        BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX="/_protected/cover-thumbnails/"
    ):
        response = client.get(thumbnails.thumbnail_url(thumbnail))

    assert response.status_code == 200
    assert response["X-Accel-Redirect"] == (
        f"/_protected/cover-thumbnails/{thumbnail.storage_identifier}"
    )
    assert response["Cache-Control"] == "private, max-age=31536000, immutable"
    assert response["Content-Type"] == "image/webp"
    assert response.content == b""


@pytest.mark.django_db
def test_thumbnail_permissions_and_bad_urls() -> None:
    _book, thumbnail = _generated()
    url = thumbnails.thumbnail_url(thumbnail)

    anonymous = Client().get(url)
    assert anonymous.status_code == 302
    assert anonymous["Location"].startswith("/accounts/login/")

    client, profile = _client("thumb-disabled")
    profile.is_disabled = True
    profile.save(update_fields=["is_disabled"])
    assert client.get(url).status_code == 403

    member, _ = _client()
    assert member.get(f"/library/covers/{thumbnail.sha256}/huge.webp").status_code == 404
    assert member.get(f"/library/covers/{'0' * 64}/medium.webp").status_code == 404
    assert member.get("/library/covers/..%2F..%2Fetc/medium.webp").status_code == 404


@pytest.mark.django_db
def test_lists_and_shelves_use_lazy_thumbnails_with_dimensions() -> None:
    book, medium = _generated("Pictured")
    small = CoverThumbnail.objects.get(cover=medium.cover, size="small")
    Book.objects.create(title="No Picture")
    client, _profile = _client()

    for url in ("/library/books/?view=html", "/library/"):
        page = client.get(url, headers=HTML).content.decode()
        assert thumbnails.thumbnail_url(medium) in page, url
        assert f"{thumbnails.thumbnail_url(small)} 120w" in page, url
        assert f"{thumbnails.thumbnail_url(medium)} 240w" in page, url
        assert 'loading="lazy"' in page and 'decoding="async"' in page, url
        assert 'width="240" height="320"' in page, url
        assert f"/library/books/{book.pk}/covers/front/" not in page, url

    listing = client.get("/library/books/").json()
    pictured = next(item for item in listing["items"] if item["title"] == "Pictured")
    assert pictured["cover_front"]["thumbnails"] == {
        "small": thumbnails.thumbnail_url(small),
        "medium": thumbnails.thumbnail_url(medium),
    }


@pytest.mark.django_db
def test_browse_list_thumbnail_lookup_is_one_query_for_the_page() -> None:
    client, _profile = _client()
    _generated("First")
    client.get("/library/books/?view=html", headers=HTML)

    with CaptureQueriesContext(connection) as one:
        client.get("/library/books/?view=html", headers=HTML)
    for index in range(6):
        _generated(f"More {index}")
    with CaptureQueriesContext(connection) as seven:
        client.get("/library/books/?view=html", headers=HTML)

    # The HTML form adds one constant query: the genre datalist (#50).
    assert len(one) == len(seven) <= 11


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_METRICS_ENABLED=True, BOOK_SCANNER_METRICS_BEARER_TOKEN="t0k")
def test_metrics_report_missing_thumbnails_and_search_rows() -> None:
    _generated("Has Thumbnails")
    _cover(Book.objects.create(title="Waiting"), _png())

    body = Client().get("/metrics/", headers={"Authorization": "Bearer t0k"}).content.decode()

    assert 'ebook_server_library_derived_rows_missing{kind="cover_thumbnails"} 1.0' in body
    assert 'ebook_server_library_derived_rows_missing{kind="search_documents"}' in body
    assert "ebook_server_search_index_lag_seconds " in body


# ------------------------------------------------- deterministic interleavings


def _replace_cover_bytes(cover: CoverImage, payload: bytes) -> str:
    """Overwrite the stored cover and its hash, as a cover refresh does."""
    path = Path(django_settings.APP_DATA_DIR) / "library-artifacts" / cover.storage_identifier
    path.write_bytes(payload)
    sha256 = hashlib.sha256(payload).hexdigest()
    CoverImage.objects.filter(pk=cover.pk).update(sha256=sha256, size_bytes=len(payload))
    return sha256


def _assert_served(cover: CoverImage) -> None:
    client, _profile = _client("interleaving-reader")
    rows = list(CoverThumbnail.objects.filter(cover=cover))
    assert len(rows) == 2
    for row in rows:
        assert _thumbnail_path(row).exists()
        response = client.get(thumbnails.thumbnail_url(row))
        assert response.status_code == 200
        body = b"".join(response.streaming_content)  # type: ignore[attr-defined]
        assert hashlib.sha256(body).hexdigest() == row.sha256


@pytest.mark.django_db
def test_stale_worker_cannot_overwrite_a_newer_covers_thumbnails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe 1: old worker renders, a newer cover's job publishes, the old one resumes."""
    book = Book.objects.create(title="Racing Covers")
    cover = _cover(book, _png(color="#111111"))
    old_sha = cover.sha256
    original_render = thumbnails.render_thumbnails
    steps: list[str] = []

    def render_then_let_the_newer_job_finish(payload: bytes) -> list[Any]:
        rendered = original_render(payload)
        if not steps:
            steps.append("old worker rendered")
            new_sha = _replace_cover_bytes(cover, _png(color="#eeeeee"))
            steps.append(new_sha)
            assert thumbnails.generate_cover_thumbnails(cover.pk) == "created"
        return rendered

    monkeypatch.setattr(thumbnails, "render_thumbnails", render_then_let_the_newer_job_finish)

    assert thumbnails.generate_cover_thumbnails(cover.pk) == "stale"

    new_sha = steps[1]
    assert new_sha != old_sha
    assert set(
        CoverThumbnail.objects.filter(cover=cover).values_list("source_sha256", flat=True)
    ) == {new_sha}
    _assert_served(cover)
    assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"
    assert not thumbnails.covers_needing_thumbnails().filter(pk=cover.pk).exists()


@pytest.mark.django_db
def test_worker_fences_on_the_bytes_it_rendered_not_the_row_it_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The cover changes between reading its row and reading its bytes."""
    book = Book.objects.create(title="Swapped Bytes")
    cover = _cover(book, _png(color="#222222"))
    original_read = thumbnails._read_cover
    replaced: list[str] = []

    def replace_then_read(row: CoverImage) -> bytes:
        if not replaced:
            replaced.append(_replace_cover_bytes(cover, _png(color="#dddddd")))
        return original_read(row)

    monkeypatch.setattr(thumbnails, "_read_cover", replace_then_read)

    # The bytes rendered are the cover's current bytes, so they are published as such.
    assert thumbnails.generate_cover_thumbnails(cover.pk) == "created"
    assert set(
        CoverThumbnail.objects.filter(cover=cover).values_list("source_sha256", flat=True)
    ) == {replaced[0]}
    assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"


@pytest.mark.django_db
def test_shared_file_collected_between_reuse_and_publication_is_written_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe 2: B renders the bytes A's files already hold, A is refreshed and its old
    files collected, then B publishes. B's thumbnails must still point at files that
    exist."""
    shared = _png(color="#445566")
    cover_a = _cover(Book.objects.create(title="Cover A"), shared)
    cover_b = _cover(Book.objects.create(title="Cover B"), shared, name="b.png")
    assert thumbnails.generate_cover_thumbnails(cover_a.pk) == "created"
    shared_paths = [_thumbnail_path(row) for row in CoverThumbnail.objects.filter(cover=cover_a)]

    original_render = thumbnails.render_thumbnails
    steps: list[str] = []

    def render_then_interleave(payload: bytes) -> list[Any]:
        rendered = original_render(payload)
        if not steps:
            steps.append("B saw the shared file")
            assert all(path.exists() for path in shared_paths)
            # Cover A is refreshed: its rows move to new bytes; the shared files are
            # only marked, never deleted inline.
            _replace_cover_bytes(cover_a, _png(color="#998877"))
            assert thumbnails.generate_cover_thumbnails(cover_a.pk) == "created"
            assert all(path.exists() for path in shared_paths)
            # Worst case: the grace period has passed and collection runs now.
            assert thumbnails.collect_unreferenced_files(grace_seconds=0).deleted == 2
            assert not any(path.exists() for path in shared_paths)
            steps.append("shared files collected")
        return rendered

    monkeypatch.setattr(thumbnails, "render_thumbnails", render_then_interleave)

    assert thumbnails.generate_cover_thumbnails(cover_b.pk) == "created"

    assert steps == ["B saw the shared file", "shared files collected"]
    assert all(path.exists() for path in shared_paths)  # written again under the lock
    _assert_served(cover_b)
    _assert_served(cover_a)
    assert thumbnails.generate_cover_thumbnails(cover_b.pk) == "current"
    files = CoverThumbnailFile.objects.filter(
        storage_identifier__in=CoverThumbnail.objects.filter(cover=cover_b).values(
            "storage_identifier"
        )
    )
    assert files.count() == 2 and not files.filter(unreferenced_since__isnull=False).exists()
    # Nothing referenced is ever collected.
    assert thumbnails.collect_unreferenced_files(grace_seconds=0).deleted == 0
    _assert_served(cover_b)


@pytest.mark.django_db
def test_collection_rechecks_references_under_the_lock() -> None:
    """A file marked unreferenced but referenced again before collection is kept."""
    book, thumbnail = _generated("Re-referenced")
    path = _thumbnail_path(thumbnail)
    CoverThumbnailFile.objects.filter(storage_identifier=thumbnail.storage_identifier).update(
        unreferenced_since=timezone.now() - timedelta(days=2)
    )

    report = thumbnails.collect_unreferenced_files(grace_seconds=0)

    assert report.deleted == 0 and report.kept == 1
    assert path.exists()
    assert (
        CoverThumbnailFile.objects.get(
            storage_identifier=thumbnail.storage_identifier
        ).unreferenced_since
        is None
    )
    assert book.front_cover is not None


@pytest.mark.django_db
def test_current_requires_the_files_to_exist() -> None:
    _book, thumbnail = _generated("Lost File")
    _thumbnail_path(thumbnail).unlink()  # lost outside the application

    assert thumbnails.generate_cover_thumbnails(thumbnail.cover_id) == "created"
    assert _thumbnail_path(thumbnail).exists()
    assert thumbnails.generate_cover_thumbnails(thumbnail.cover_id) == "current"


# ------------------------------------------- atomic writes and content checks


def _failing_copy(written: list[Path], *, check: Callable[[], None] | None = None) -> Any:
    """A `_copy_file` that writes 3 bytes and then fails, like a broken network write."""

    def copy(src: Path, dest: Path) -> None:
        written.append(dest)
        dest.write_bytes(src.read_bytes()[:3])
        if check is not None:
            check()
        raise OSError("simulated write failure after 3 bytes")

    return staticmethod(copy)


def _partial_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.name.endswith(".partial")]


def test_store_atomic_failure_leaves_the_destination_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = LocalFilesystemStorage(str(tmp_path / "store"))
    source = tmp_path / "new.bin"
    source.write_bytes(b"new complete content")
    destination = storage.resolve_location("ab/abc.webp")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old complete content")

    def reader_sees_old_bytes() -> None:
        assert destination.read_bytes() == b"old complete content"

    written: list[Path] = []
    monkeypatch.setattr(
        LocalFilesystemStorage, "_copy_file", _failing_copy(written, check=reader_sees_old_bytes)
    )
    with pytest.raises(OSError, match="simulated"):
        storage.store_atomic(str(source), "ab/abc.webp")

    assert written and written[0] != destination  # staged beside it, never in place
    assert destination.read_bytes() == b"old complete content"
    assert _partial_files(tmp_path) == []

    monkeypatch.undo()
    storage.store_atomic(str(source), "ab/abc.webp")
    assert destination.read_bytes() == b"new complete content"
    assert _partial_files(tmp_path) == []


@pytest.mark.django_db
def test_competing_failed_write_cannot_corrupt_a_published_shared_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe 3 (review of 8b03dd9): worker 1 is past its own checks when worker 2
    publishes the same content-addressed files; every write worker 1 attempts after
    that fails after 3 bytes. The published bytes must stay intact."""
    shared = _png(color="#5a5a5a")
    cover_1 = _cover(Book.objects.create(title="Worker 1"), shared)
    cover_2 = _cover(Book.objects.create(title="Worker 2"), shared, name="two.png")
    original_render = thumbnails.render_thumbnails
    written: list[Path] = []
    steps: list[str] = []

    def render_then_let_worker_2_publish(payload: bytes) -> list[Any]:
        rendered = original_render(payload)
        if not steps:
            steps.append("worker 1 rendered")
            assert thumbnails.generate_cover_thumbnails(cover_2.pk) == "created"
            steps.append("worker 2 published")
            monkeypatch.setattr(LocalFilesystemStorage, "_copy_file", _failing_copy(written))
        return rendered

    monkeypatch.setattr(thumbnails, "render_thumbnails", render_then_let_worker_2_publish)

    # The complete files are verified and reused: worker 1 writes nothing.
    assert thumbnails.generate_cover_thumbnails(cover_1.pk) == "created"

    assert steps == ["worker 1 rendered", "worker 2 published"]
    assert written == []
    for cover in (cover_1, cover_2):
        _assert_served(cover)
        assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"
    row = CoverThumbnail.objects.filter(cover=cover_1).first()
    assert row is not None
    assert _partial_files(_thumbnail_path(row).parents[1]) == []


@pytest.mark.django_db
def test_failed_write_publishes_nothing_and_the_retry_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cover = _cover(Book.objects.create(title="Broken Share"), _png(color="#7b7b7b"))
    written: list[Path] = []
    monkeypatch.setattr(LocalFilesystemStorage, "_copy_file", _failing_copy(written))

    with pytest.raises(OSError, match="simulated"):  # raised, so the queue retries
        thumbnails.generate_cover_thumbnails(cover.pk)

    assert written
    assert not CoverThumbnail.objects.filter(cover=cover).exists()
    assert not CoverThumbnailFile.objects.exists()
    root = Path(django_settings.APP_DATA_DIR) / thumbnails.thumbnail_storage_location().base_path
    assert [path for path in root.rglob("*.webp")] == []  # no partial file at a final name
    assert _partial_files(root) == []

    monkeypatch.undo()
    assert thumbnails.generate_cover_thumbnails(cover.pk) == "created"
    _assert_served(cover)
    assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"


@pytest.mark.django_db
def test_damaged_shared_file_is_not_current_and_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The state probe 3 left behind on 8b03dd9: a live row, a file with wrong bytes."""
    shared = _png(color="#3c3c3c")
    cover_1 = _cover(Book.objects.create(title="Damaged 1"), shared)
    cover_2 = _cover(Book.objects.create(title="Damaged 2"), shared, name="two.png")
    assert thumbnails.generate_cover_thumbnails(cover_1.pk) == "created"
    assert thumbnails.generate_cover_thumbnails(cover_2.pk) == "created"
    row = CoverThumbnail.objects.get(cover=cover_1, size="medium")
    path = _thumbnail_path(row)
    complete = path.read_bytes()

    # A failed repair attempt leaves the damaged file as it was, never a new partial.
    path.write_bytes(complete[:3])
    written: list[Path] = []
    monkeypatch.setattr(LocalFilesystemStorage, "_copy_file", _failing_copy(written))
    with pytest.raises(OSError, match="simulated"):
        thumbnails.generate_cover_thumbnails(cover_2.pk)
    assert written and path not in written
    assert path.read_bytes() == complete[:3]
    monkeypatch.undo()

    # Existence alone is not "current": the retry replaces the bytes.
    assert thumbnails.generate_cover_thumbnails(cover_2.pk) == "created"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == row.sha256
    for cover in (cover_1, cover_2):
        _assert_served(cover)
        assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"
    record = CoverThumbnailFile.objects.get(storage_identifier=row.storage_identifier)
    assert (record.sha256, record.size_bytes) == (row.sha256, len(complete))


@pytest.mark.django_db
def test_backfill_verify_repairs_damaged_files_that_look_current() -> None:
    cover = _cover(Book.objects.create(title="Verify Me"), _png(color="#246824"))
    intact = _cover(Book.objects.create(title="Intact"), _png(color="#682424"), name="i.png")
    for item in (cover, intact):
        assert thumbnails.generate_cover_thumbnails(item.pk) == "created"
    row = CoverThumbnail.objects.get(cover=cover, size="small")
    _thumbnail_path(row).write_bytes(b"\x00" * row.size_bytes)  # right size, wrong bytes

    plain = StringIO()
    call_command("generate_cover_thumbnails", stdout=plain)
    assert "nothing to do" in plain.getvalue()  # the rows alone look current

    verified = StringIO()
    call_command("generate_cover_thumbnails", "--verify", stdout=verified)
    assert "created: 1" in verified.getvalue() and "current: 1" in verified.getvalue()
    assert hashlib.sha256(_thumbnail_path(row).read_bytes()).hexdigest() == row.sha256
    _assert_served(cover)


@pytest.mark.django_db
def test_writer_failing_after_a_competitor_published_leaves_the_complete_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Probe 3 at the verifier's exact point: worker 1 has found the file absent and
    is about to write it; worker 2 writes the same file; worker 1's write then fails
    after 3 bytes. (On one SQLite connection worker 2 runs inside worker 1's
    transaction, so its rows roll back with worker 1's; PostgreSQL, where worker 2
    waits for the lock, is in tests/integration/test_cover_thumbnails_postgres.py.)"""
    shared = _png(color="#8a2be2")
    cover_1 = _cover(Book.objects.create(title="Writer 1"), shared)
    cover_2 = _cover(Book.objects.create(title="Writer 2"), shared, name="two.png")
    original_matches = thumbnails._file_matches
    written: list[Path] = []
    published: dict[str, bytes] = {}
    interleaved: list[bool] = []

    def absent_then_interleave(storage: Any, identifier: str, sha256: str, size: int) -> bool:
        result = original_matches(storage, identifier, sha256, size)
        if not interleaved:
            interleaved.append(True)
            assert result is False  # worker 1 found nothing and will write
            assert thumbnails.generate_cover_thumbnails(cover_2.pk) == "created"
            for row in CoverThumbnail.objects.filter(cover=cover_2):
                published[row.storage_identifier] = _thumbnail_path(row).read_bytes()
            monkeypatch.setattr(LocalFilesystemStorage, "_copy_file", _failing_copy(written))
        return result

    monkeypatch.setattr(thumbnails, "_file_matches", absent_then_interleave)

    with pytest.raises(OSError, match="simulated"):
        thumbnails.generate_cover_thumbnails(cover_1.pk)

    assert written and len(published) == 2
    root = Path(django_settings.APP_DATA_DIR) / thumbnails.thumbnail_storage_location().base_path
    for identifier, payload in published.items():
        assert (root / identifier).read_bytes() == payload  # never truncated
        assert hashlib.sha256(payload).hexdigest() in identifier
    assert _partial_files(root) == []

    monkeypatch.undo()
    for cover in (cover_1, cover_2):
        assert thumbnails.generate_cover_thumbnails(cover.pk) == "created"
        _assert_served(cover)
        assert thumbnails.generate_cover_thumbnails(cover.pk) == "current"
