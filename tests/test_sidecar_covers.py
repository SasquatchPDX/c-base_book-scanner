"""Sidecar ``cover.jpg`` (Calibre-style) handling for source-folder imports (issue #33)."""

from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from django.test import override_settings
from PIL import Image

from ebook_server.core import covers, intake
from ebook_server.core.models import CoverImage, ImportJob, SourceLocation, StorageLocation
from ebook_server.core.storage_registry import backend_for_location

SIDECAR_COLOR = (20, 40, 220)
EMBEDDED_COLOR = (220, 30, 20)


def _image_bytes(
    color: tuple[int, int, int],
    *,
    size: tuple[int, int] = (300, 450),
    image_format: str = "JPEG",
) -> bytes:
    stream = io.BytesIO()
    with Image.new("RGB", size, color) as image:
        image.save(stream, format=image_format)
    return stream.getvalue()


def _write_cbz(path: Path, color: tuple[int, int, int] = EMBEDDED_COLOR) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr("001.png", _image_bytes(color, image_format="PNG"))


def _stub_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    class StubProvider:
        provider_id = "stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, Any]]:
            del author, isbn, folder_hint
            return [{"title": title or "Unknown", "author_name": ["Sidecar Tester"]}]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.0)
    monkeypatch.setattr("ebook_server.core.intake.REVIEW_SCORE", 0.0)


def _source_location(root: Path, *, move: bool = False) -> SourceLocation:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return SourceLocation.objects.create(
        name=f"sidecar-{root.name}",
        root_path=str(root),
        transfer_mode=(
            SourceLocation.TransferMode.MOVE if move else SourceLocation.TransferMode.COPY
        ),
        recursive=True,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
        is_active=True,
    )


def _import_all(location: SourceLocation) -> list[ImportJob]:
    jobs = sorted(
        intake.discover_source_jobs_for_location(location),
        key=lambda job: job.source_path,
    )
    for job in jobs:
        intake.process_import_job(str(job.id))
    return [ImportJob.objects.get(pk=job.pk) for job in jobs]


def _front_cover(job: ImportJob) -> CoverImage:
    assert job.state == ImportJob.State.COMPLETE
    return CoverImage.objects.get(book=job.book, position=CoverImage.CoverPosition.FRONT)


def _cover_color(cover: CoverImage) -> tuple[int, ...]:
    storage = backend_for_location(cover.storage_location)
    with storage.open(cover.storage_identifier) as handle:
        payload = handle.read()
    with Image.open(io.BytesIO(payload)) as image:
        pixel = image.convert("RGB").getpixel((10, 10))
    assert isinstance(pixel, tuple)
    return pixel


def _close_to(actual: tuple[int, ...], expected: tuple[int, int, int]) -> bool:
    return all(abs(a - e) <= 12 for a, e in zip(actual, expected, strict=True))


@pytest.mark.django_db(transaction=True)
def test_single_book_folder_uses_sidecar_cover_and_keeps_source_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "calibre"
    book_dir = root / "Jane Doe" / "Some Book (12)"
    book_dir.mkdir(parents=True)
    _write_cbz(book_dir / "Some Book - Jane Doe.cbz")
    (book_dir / "metadata.opf").write_text("<package/>")
    sidecar = book_dir / "cover.jpg"
    sidecar.write_bytes(_image_bytes(SIDECAR_COLOR))
    sidecar_sha = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    _stub_provider(monkeypatch)

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        location = _source_location(root, move=True)
        (job,) = _import_all(location)
        cover = _front_cover(job)

        assert cover.extractor_tool == covers.SIDECAR_EXTRACTOR_TOOL
        assert cover.source_variant_id == job.file_variant_id
        assert "Pillow" in cover.extractor_version
        assert cover.mime_type == "image/jpeg"
        assert _close_to(_cover_color(cover), SIDECAR_COLOR)

    # MOVE removes only the verified book file; the sidecar is never moved or modified.
    assert not (book_dir / "Some Book - Jane Doe.cbz").exists()
    assert sidecar.exists()
    assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == sidecar_sha


@pytest.mark.django_db(transaction=True)
def test_multiple_formats_of_one_book_share_the_sidecar_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "formats"
    root.mkdir()
    _write_cbz(root / "Shared Title.cbz")
    (root / "Shared Title.txt").write_text("plain text edition")
    (root / "cover.png").write_bytes(_image_bytes(SIDECAR_COLOR, image_format="PNG"))
    _stub_provider(monkeypatch)

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        jobs = _import_all(_source_location(root))
        assert len(jobs) == 2
        for job in jobs:
            cover = _front_cover(job)
            assert cover.extractor_tool == covers.SIDECAR_EXTRACTOR_TOOL


@pytest.mark.django_db(transaction=True)
def test_multi_book_folder_never_uses_folder_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "mixed"
    root.mkdir()
    _write_cbz(root / "First Book.cbz")
    _write_cbz(root / "Second Book.cbz")
    (root / "cover.jpg").write_bytes(_image_bytes(SIDECAR_COLOR))
    _stub_provider(monkeypatch)

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        jobs = _import_all(_source_location(root))
        assert len(jobs) == 2
        for job in jobs:
            cover = _front_cover(job)
            assert cover.extractor_tool == "zipfile+pillow"
            assert _close_to(_cover_color(cover), EMBEDDED_COLOR)


@pytest.mark.django_db(transaction=True)
def test_move_source_siblings_already_archived_still_count_as_other_books(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With MOVE, the first book is gone before the second is processed."""
    root = tmp_path / "moved"
    root.mkdir()
    _write_cbz(root / "First Book.cbz")
    _write_cbz(root / "Second Book.cbz")
    (root / "cover.jpg").write_bytes(_image_bytes(SIDECAR_COLOR))
    _stub_provider(monkeypatch)

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        first, second = _import_all(_source_location(root, move=True))
        assert not (root / "First Book.cbz").exists()
        assert not (root / "Second Book.cbz").exists()
        assert covers.find_sidecar_cover(second) is None
        assert _front_cover(second).extractor_tool == "zipfile+pillow"
        assert _front_cover(first).extractor_tool == "zipfile+pillow"
    assert (root / "cover.jpg").exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("problem", ["corrupt", "oversized", "too_many_pixels"])
def test_invalid_sidecar_cover_falls_back_to_format_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    problem: str,
) -> None:
    root = tmp_path / f"invalid-{problem}"
    root.mkdir()
    _write_cbz(root / "Lonely Book.cbz")
    sidecar = root / "cover.jpg"
    if problem == "corrupt":
        sidecar.write_bytes(b"\xff\xd8\xff\xe0 definitely not a jpeg")
    else:
        sidecar.write_bytes(_image_bytes(SIDECAR_COLOR))
    if problem == "oversized":
        monkeypatch.setattr(covers, "MAX_SIDECAR_COVER_BYTES", 64)
    if problem == "too_many_pixels":
        monkeypatch.setattr(covers, "MAX_SIDECAR_COVER_PIXELS", 100)
    _stub_provider(monkeypatch)

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        (job,) = _import_all(_source_location(root))
        cover = _front_cover(job)
        assert cover.extractor_tool == "zipfile+pillow"
        assert _close_to(_cover_color(cover), EMBEDDED_COLOR)
    assert sidecar.exists()


@pytest.mark.django_db(transaction=True)
def test_decompression_bomb_sidecar_is_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "bomb"
    root.mkdir()
    (root / "Bomb.txt").write_text("text edition without an embedded cover")
    (root / "cover.jpg").write_bytes(_image_bytes(SIDECAR_COLOR))
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 100)
    _stub_provider(monkeypatch)

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        (job,) = _import_all(_source_location(root))
        assert job.state == ImportJob.State.COMPLETE
        assert not CoverImage.objects.filter(book=job.book).exists()


@pytest.mark.django_db(transaction=True)
def test_symlinked_sidecar_cover_is_ignored(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "secret.jpg"
    target.write_bytes(_image_bytes(SIDECAR_COLOR))
    root = tmp_path / "linked"
    root.mkdir()
    _write_cbz(root / "Linked Book.cbz")
    try:
        os.symlink(target, root / "cover.jpg")
    except (OSError, NotImplementedError):
        pytest.skip("symlinks are not available on this platform/account")
    _stub_provider(monkeypatch)

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        (job,) = _import_all(_source_location(root))
        assert covers.find_sidecar_cover(job) is None
        assert _front_cover(job).extractor_tool == "zipfile+pillow"


@pytest.mark.django_db(transaction=True)
def test_sidecar_lookup_skips_uploads_disabled_setting_and_foreign_folders(
    tmp_path: Path,
) -> None:
    root = tmp_path / "lookup"
    root.mkdir()
    book = root / "Only Book.txt"
    book.write_text("text")
    (root / "cover.jpg").write_bytes(_image_bytes(SIDECAR_COLOR))
    location = _source_location(root)
    job = intake.create_source_import_job(book, location)

    assert covers.find_sidecar_cover(job) == (root / "cover.jpg").resolve()

    with override_settings(BOOK_SCANNER_SIDECAR_COVERS_ENABLED=False):
        assert covers.find_sidecar_cover(job) is None

    upload_job = ImportJob(source_type=ImportJob.SourceType.UPLOAD, source_path=str(book))
    assert covers.find_sidecar_cover(upload_job) is None

    other_root = tmp_path / "elsewhere"
    other_root.mkdir()
    location.root_path = str(other_root)
    assert covers.find_sidecar_cover(job) is None
