"""Regression tests for the scanner capture HTTP API (real-client behaviour)."""

from __future__ import annotations

import io
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from django.db import connection
from django.test import Client
from PIL import Image

from ebook_server.core import views
from ebook_server.core.models import ScanJob, ScanPage, ScanPageVersion, StorageLocation
from ebook_server.storage import LocalFilesystemStorage
from tests.test_scan_jobs import (
    _create_scan_job,
    _create_scan_job_with_book,
    _page_sha,
    _png_for,
    _put_scan_page,
    _upload_scan_page,
)


@pytest.fixture(autouse=True)
def _app_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    return tmp_path


def _post_page(client: Client, job: ScanJob, payload: bytes, **fields: str) -> Any:
    from django.core.files.uploadedfile import SimpleUploadedFile

    data = {
        "logical_sequence_number": "1",
        "checksum_sha256": _page_sha(payload),
        **fields,
        "file": SimpleUploadedFile("p.png", payload, content_type="image/png"),
    }
    return client.post(f"/scan/jobs/{job.id}/pages/", data=data)


def _stored_files(root: Path) -> list[Path]:
    base = root / "scan-artifacts" / "scan-jobs"
    return [path for path in base.rglob("*") if path.is_file()] if base.exists() else []


@pytest.mark.django_db(transaction=True)
def test_multipart_put_replaces_page_and_rejects_missing_file() -> None:
    client, job = _create_scan_job("capture-put")
    assert (
        _upload_scan_page(
            client, str(job.id), 1, "single", _png_for(b"a"), filename="a.png"
        ).status_code
        == 201
    )
    page = ScanPage.objects.get(scan_job=job)

    replaced = _put_scan_page(client, page.id, _png_for(b"b"))
    assert replaced.status_code == 201
    assert page.versions.count() == 2

    again = _put_scan_page(client, page.id, _png_for(b"b"))
    assert again.status_code == 200
    assert page.versions.count() == 2

    no_file = client.put(
        f"/scan/pages/{page.id}/",
        data='{"checksum_sha256": "x"}',
        content_type="application/json",
    )
    assert no_file.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_side_is_validated_and_left_right_pair_coexists() -> None:
    client, job = _create_scan_job("capture-side")
    assert _post_page(client, job, _png_for(b"l"), side="L").status_code == 201
    assert _post_page(client, job, _png_for(b"r"), side="Right").status_code == 201
    assert set(ScanPage.objects.filter(scan_job=job).values_list("side", flat=True)) == {
        "left",
        "right",
    }
    for bad in ("verso", "both", "x"):
        assert _post_page(client, job, _png_for(b"z"), side=bad).status_code == 400
    assert _post_page(client, job, _png_for(b"s")).status_code == 201
    assert ScanPage.objects.filter(scan_job=job, side="single").count() == 1


@pytest.mark.django_db(transaction=True)
def test_unauthenticated_api_gets_401_json_but_browsers_redirect() -> None:
    anonymous = Client()
    response = anonymous.get("/scan/jobs/", HTTP_ACCEPT="application/json")
    assert response.status_code == 401
    assert response.json() == {"error": "authentication required"}
    assert anonymous.post("/scan/jobs/", data={}).status_code == 401
    assert anonymous.get("/scan/jobs/", HTTP_ACCEPT="*/*").status_code == 401
    browser = anonymous.get("/scan/jobs/", HTTP_ACCEPT="text/html,application/xhtml+xml")
    assert browser.status_code == 302
    assert browser["Location"].startswith("/accounts/login/")


@pytest.mark.django_db(transaction=True)
def test_finish_with_raw_pages_returns_json_hint() -> None:
    from ebook_server.core.models import Book

    book = Book.objects.create(title="Raw Book")
    client, job = _create_scan_job_with_book("capture-finish", book_id=book.id)
    assert _post_page(client, job, _png_for(b"raw")).status_code == 201
    response = client.post(f"/scan/jobs/{job.id}/finish/", data={})
    assert response.status_code == 400
    body = response.json()
    assert "missing processed output" in body["error"]
    assert body["missing_pages"] == [str(ScanPage.objects.get(scan_job=job).id)]
    assert "allow_raw_pages=true" in body["hint"]


@pytest.mark.django_db(transaction=True)
def test_bytes_are_copied_outside_the_job_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    client, job = _create_scan_job("capture-lock")
    observed: list[bool] = []
    original = LocalFilesystemStorage.store

    def spy(self: LocalFilesystemStorage, source: str, destination: str) -> str:
        observed.append(connection.in_atomic_block)
        return original(self, source, destination)

    monkeypatch.setattr(LocalFilesystemStorage, "store", spy)
    assert _post_page(client, job, _png_for(b"lock")).status_code == 201
    assert observed == [False]


@pytest.mark.django_db(transaction=True)
def test_duplicate_and_failed_uploads_leave_no_orphan_files(
    _app_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, job = _create_scan_job("capture-orphans")
    payload = _png_for(b"dup")
    assert _post_page(client, job, payload).status_code == 201
    assert len(_stored_files(_app_data_dir)) == 1
    assert _post_page(client, job, payload).status_code == 200
    assert len(_stored_files(_app_data_dir)) == 1

    def boom(**_: Any) -> None:
        raise RuntimeError("db down")

    monkeypatch.setattr(views, "_append_version", boom)
    client.raise_request_exception = False
    failed = _post_page(client, job, _png_for(b"other"))
    assert failed.status_code == 500
    assert len(_stored_files(_app_data_dir)) == 1


@pytest.mark.django_db(transaction=True)
def test_first_upload_moves_open_to_capturing_and_transitions_are_idempotent() -> None:
    client, job = _create_scan_job("capture-state")
    assert job.state == ScanJob.State.OPEN
    assert _post_page(client, job, _png_for(b"s")).status_code == 201
    job.refresh_from_db()
    assert job.state == ScanJob.State.CAPTURING

    for _ in range(2):
        paused = client.post(f"/scan/jobs/{job.id}/pause/")
        assert paused.status_code == 200
        assert paused.json()["state"] == "paused"
    for _ in range(2):
        resumed = client.post(f"/scan/jobs/{job.id}/resume/")
        assert resumed.status_code == 200
        assert resumed.json()["state"] == "capturing"
    for _ in range(2):
        cancelled = client.post(f"/scan/jobs/{job.id}/cancel/")
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"


@pytest.mark.django_db(transaction=True)
def test_pause_cannot_overwrite_finalizing() -> None:
    client, job = _create_scan_job("capture-finalizing")
    ScanJob.objects.filter(pk=job.pk).update(state=ScanJob.State.FINALIZING)
    assert client.post(f"/scan/jobs/{job.id}/pause/").status_code == 400
    job.refresh_from_db()
    assert job.state == ScanJob.State.FINALIZING


@pytest.mark.django_db(transaction=True)
def test_filename_extension_and_mime_come_from_client_name_and_sniffed_format() -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    client, job = _create_scan_job("capture-names")
    payload = _png_for(b"name")
    response = client.post(
        f"/scan/jobs/{job.id}/pages/",
        data={
            "logical_sequence_number": "1",
            "file": SimpleUploadedFile(
                "../My Scan 1.PNG", payload, content_type="application/octet-stream"
            ),
        },
    )
    assert response.status_code == 201
    version = ScanPageVersion.objects.get()
    assert version.original_filename == "My_Scan_1.PNG"
    assert version.storage_identifier.endswith("My_Scan_1.PNG")
    assert ".tmp" not in version.storage_identifier
    assert version.mime_type == "image/png"

    response = client.post(
        f"/scan/jobs/{job.id}/pages/",
        data={
            "logical_sequence_number": "2",
            "file": SimpleUploadedFile("capture", _png_for(b"n2"), content_type="text/plain"),
        },
    )
    assert response.status_code == 201
    second = ScanPageVersion.objects.get(scan_page__logical_sequence_number=2)
    assert second.original_filename == "capture.png"
    assert second.storage_identifier.endswith(".png")


@pytest.mark.django_db(transaction=True)
def test_non_image_upload_is_rejected(_app_data_dir: Path) -> None:
    client, job = _create_scan_job("capture-invalid")
    response = _post_page(client, job, b"definitely not an image")
    assert response.status_code == 400
    assert b"invalid image" in response.content
    assert not ScanPage.objects.filter(scan_job=job, versions__isnull=False).exists()
    assert _stored_files(_app_data_dir) == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("1700000000", datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        (1700000000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        ("1700000000000", datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        ("1700000000.5", datetime(2023, 11, 14, 22, 13, 20, 500000, tzinfo=UTC)),
        ("2024-01-02T03:04:05", datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("2024-01-02T03:04:05Z", datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)),
        (None, None),
        ("", None),
    ],
)
def test_parse_capture_ts(raw: Any, expected: datetime | None) -> None:
    assert views._parse_capture_ts(raw) == expected


@pytest.mark.parametrize("raw", ["yesterday", "1e999", "nan", True])
def test_parse_capture_ts_rejects_garbage(raw: Any) -> None:
    with pytest.raises(ValueError):
        views._parse_capture_ts(raw)


@pytest.mark.django_db(transaction=True)
def test_invalid_capture_timestamp_is_400() -> None:
    client, job = _create_scan_job("capture-ts")
    assert _post_page(client, job, _png_for(b"t"), capture_timestamp="nope").status_code == 400
    ok = _post_page(client, job, _png_for(b"t"), capture_timestamp="1700000000")
    assert ok.status_code == 201
    assert ScanPageVersion.objects.get().capture_timestamp is not None


def _tiff_bytes(mode: str, size: tuple[int, int]) -> bytes:
    image = Image.new(mode, size)
    stream = io.BytesIO()
    image.save(stream, format="TIFF")
    return stream.getvalue()


@pytest.mark.django_db(transaction=True)
def test_thumbnail_is_downscaled_jpeg_cached_and_uses_version_storage(
    _app_data_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, job = _create_scan_job("capture-thumb")
    payload = _tiff_bytes("I;16", (3200, 2400))
    assert _post_page(client, job, payload).status_code == 201
    page = ScanPage.objects.get(scan_job=job)
    version = page.active_version()
    assert version is not None
    assert version.mime_type == "image/tiff"

    # Move the file to a non-default storage location; the view must follow it.
    other_root = _app_data_dir / "elsewhere"
    default_root = _app_data_dir / "scan-artifacts"
    (other_root).mkdir()
    source = default_root / version.storage_identifier
    (other_root / source.name).write_bytes(source.read_bytes())
    source.unlink()
    location = StorageLocation.objects.create(
        identifier="elsewhere", backend=StorageLocation.BackendType.LOCAL, base_path=str(other_root)
    )
    ScanPageVersion.objects.filter(pk=version.pk).update(
        storage_location=location, storage_identifier=source.name
    )

    response = client.get(f"/scan/pages/{page.id}/image/")
    assert response.status_code == 200
    assert response["Content-Type"] == "image/jpeg"
    assert "max-age" in response["Cache-Control"]
    body = b"".join(response.streaming_content)  # type: ignore[attr-defined]
    with Image.open(io.BytesIO(body)) as thumb:
        assert thumb.format == "JPEG"
        assert thumb.mode == "RGB"
        assert max(thumb.size) == 1600

    etag = response["ETag"]
    cached = client.get(f"/scan/pages/{page.id}/image/", HTTP_IF_NONE_MATCH=etag)
    assert cached.status_code == 304

    # Second render comes from the on-disk cache, not the storage backend.
    monkeypatch.setattr(
        views,
        "_render_scan_thumbnail",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("should be cached")),
    )
    assert client.get(f"/scan/pages/{page.id}/image/").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_storage_outage_during_upload_is_retryable_503(monkeypatch: pytest.MonkeyPatch) -> None:
    from ebook_server.storage import StorageUnavailableError

    client, job = _create_scan_job("capture-outage")

    def _unmounted(*_args: Any, **_kwargs: Any) -> Any:
        raise StorageUnavailableError("storage root is not mounted")

    monkeypatch.setattr(views, "_stage_scan_upload", _unmounted)
    response = _post_page(client, job, _png_for(b"outage"))
    assert response.status_code == 503
    assert response.json() == {"error": "storage unavailable", "retryable": True}
    assert not ScanPage.objects.filter(scan_job=job).exists()
