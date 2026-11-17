"""Audio files are recognised and skipped, never ingested (issue #32)."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client, override_settings

from ebook_server.core import intake
from ebook_server.core.models import ImportJob, SourceLocation, get_or_create_profile


def _source(root: Path, *, transfer_mode: str = SourceLocation.TransferMode.COPY) -> SourceLocation:
    return SourceLocation.objects.create(
        name=f"audio-{root.name}",
        root_path=str(root),
        transfer_mode=transfer_mode,
        recursive=True,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
        is_active=True,
    )


@pytest.mark.parametrize(
    "name",
    ["book.m4b", "Chapter 01.MP3", "x.m4a", "x.aax", "x.aaxc", "x.opus", "x.flac", "x.wma"],
)
def test_audio_extensions_are_classified_as_audio(name: str) -> None:
    assert intake.is_audio_filename(name)


@pytest.mark.parametrize("name", ["book.epub", "book.pdf", "notes.txt", "scan.djvu", "x.docx"])
def test_book_and_unknown_extensions_are_not_audio(name: str) -> None:
    assert not intake.is_audio_filename(name)


def test_audio_mime_only_counts_for_non_book_extensions() -> None:
    assert intake.is_audio_filename("mystery.bin", "audio/mpeg")
    assert not intake.is_audio_filename("book.epub", "audio/mpeg")


def test_audible_files_are_flagged_drm_protected() -> None:
    assert intake.is_drm_audio_filename("Book.AAX")
    assert intake.is_drm_audio_filename("book.aaxc")
    assert not intake.is_drm_audio_filename("book.m4b")
    assert "DRM" in intake.audio_skip_reason("book.aax")
    assert intake.audio_skip_reason("book.mp3") == intake.AUDIO_SKIP_REASON


@pytest.mark.django_db
def test_preview_lists_audio_as_a_distinct_skipped_category(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    (root / "Some Audiobook").mkdir(parents=True)
    book = root / "book.pdf"
    book.write_bytes(b"%PDF-1.4")
    m4b = root / "Some Audiobook" / "book.m4b"
    m4b.write_bytes(b"\x00\x00\x00\x20ftypM4B ")
    aax = root / "audible.aax"
    aax.write_bytes(b"not really aax")
    docx = root / "ignore.docx"
    docx.write_text("unrelated")
    location = _source(root)

    stdout = StringIO()
    call_command(
        "ingest_sources", "--source-id", str(location.id), "--preview", "--json", stdout=stdout
    )
    payload = json.loads(stdout.getvalue())

    by_path = {row["path"]: row for row in payload if "path" in row}
    assert set(by_path) == {str(book), str(m4b), str(aax), str(docx)}
    assert "skipped" not in by_path[str(book)]
    assert by_path[str(book)]["status"] == "importable"
    # Unsupported formats are a separate skipped category (#23), not audio.
    assert by_path[str(docx)]["skipped"] == "unsupported"
    assert by_path[str(m4b)]["status"] == "skipped"
    assert by_path[str(m4b)]["skipped"] == "audio"
    assert by_path[str(m4b)]["reason"] == "audio (not managed here)"
    assert by_path[str(m4b)]["drm_protected"] is False
    assert "source_type" not in by_path[str(m4b)]
    assert by_path[str(aax)]["drm_protected"] is True

    text = StringIO()
    call_command("ingest_sources", "--source-id", str(location.id), "--preview", stdout=text)
    output = text.getvalue()
    assert f"skipped [audio (not managed here)] => {m4b}" in output
    assert "skipped_audio=2" in output
    assert f"SKIPPED (unsupported format: .docx) => {docx}" in output
    summary = next(row["summary"] for row in payload if "summary" in row)
    assert summary["importable"] == 1
    assert summary["skipped"] == 3
    assert summary["skipped_audio"] == 2
    assert ImportJob.objects.count() == 0


@pytest.mark.django_db
def test_discovery_never_creates_import_jobs_for_audio(tmp_path: Path) -> None:
    root = tmp_path / "incoming-move"
    root.mkdir()
    files = []
    for name in ("a.mp3", "b.m4b", "c.aaxc", "d.ogg"):
        path = root / name
        path.write_bytes(b"audio")
        files.append(path)
    location = _source(root, transfer_mode=SourceLocation.TransferMode.MOVE)

    counts: list[int] = []
    assert intake.discover_source_jobs_for_location(location, skipped_audio=counts) == []
    assert counts == [4]

    stdout = StringIO()
    with override_settings(BOOK_SCANNER_STORAGE_MIN_FREE_BYTES=0):
        call_command("ingest_sources", "--source-id", str(location.id), stdout=stdout)
    assert "ingest queued=0" in stdout.getvalue()
    assert "skipped_audio=4" in stdout.getvalue()

    assert ImportJob.objects.count() == 0
    assert all(path.exists() for path in files)  # MOVE never touches skipped audio


@pytest.mark.django_db
def test_create_source_import_job_refuses_audio(tmp_path: Path) -> None:
    root = tmp_path / "incoming"
    root.mkdir()
    path = root / "book.aax"
    path.write_bytes(b"audio")
    location = _source(root)

    with pytest.raises(intake.IntakeError, match="DRM-protected"):
        intake.create_source_import_job(path, location)
    assert ImportJob.objects.count() == 0


def _upload(client: Client, name: str, content_type: str) -> Any:
    return client.post(
        "/imports/upload/",
        {"file": SimpleUploadedFile(name, b"ID3\x03\x00audio", content_type=content_type)},
    )


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_AUDIOBOOK_SERVICE_URL="https://audiobooks.example.org/")
def test_audio_upload_is_rejected_with_pointer_to_audiobook_service() -> None:
    client = Client()
    client.force_login(get_or_create_profile("listener").user)

    response = _upload(client, "Some Book.m4b", "audio/x-m4b")

    assert response.status_code == 415
    body = response.content.decode()
    assert "not managed by this e-book library" in body
    assert "https://audiobooks.example.org/" in body
    assert ImportJob.objects.count() == 0


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_AUDIOBOOK_SERVICE_URL="")
def test_audio_upload_without_service_url_gets_generic_message() -> None:
    client = Client()
    client.force_login(get_or_create_profile("listener").user)

    response = _upload(client, "chapter01.mp3", "audio/mpeg")

    assert response.status_code == 415
    body = response.content.decode()
    assert "ask the library admins" in body
    assert "http" not in body
    assert ImportJob.objects.count() == 0


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_AUDIOBOOK_SERVICE_URL="https://audiobooks.example.org/")
def test_drm_audible_upload_is_rejected_as_drm() -> None:
    client = Client()
    client.force_login(get_or_create_profile("listener").user)

    response = _upload(client, "Book.aax", "application/octet-stream")

    assert response.status_code == 415
    assert "DRM-protected" in response.content.decode()
    assert ImportJob.objects.count() == 0


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_AUDIOBOOK_SERVICE_URL="javascript:alert(1)")
def test_non_http_service_url_is_ignored() -> None:
    assert intake.audiobook_service_url() == ""
    assert "javascript" not in intake.audio_upload_rejection_message("a.mp3")
