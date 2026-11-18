"""Device page and cover uploads (#128, libreflip contract §4.2, §4.3, §6.5, §6.6).

Done when (issue #128):

* repeating the same upload creates no new version;
* killing the connection mid-upload leaves no active partial version;
* parity and checksum errors are rejected;
* covers appear on the Book after finish.
"""

from __future__ import annotations

import hashlib
import io
import json
import struct
import zlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import PropertyMock

import pytest
from django.http import HttpRequest
from django.test import Client, RequestFactory
from PIL import Image

from ebook_server.core import scan_covers, scan_pipeline, scanner_api, scanner_uploads
from ebook_server.core.models import (
    Book,
    CoverImage,
    ScanCover,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    ScanUploadSource,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.core.scanner_auth import generate_key, hash_key

JOB_ID = "20261117T143201Z"
STARTED = "2026-11-17T14:32:01Z"
BOUNDARY = "sansboundary7MA4YWxkTrZu0gW"


@pytest.fixture(autouse=True)
def _data_dir(settings: Any, tmp_path: Path) -> None:
    settings.APP_DATA_DIR = tmp_path
    settings.BOOK_SCANNER_PUBLIC_BASE_URL = "https://serif.c-base.lan/"


@pytest.fixture
def key(db: None, settings: Any) -> str:
    """The scanner key (#125: one scanner, one key hash in settings), job registered."""
    api_key = generate_key()
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = hash_key(api_key)
    response = Client().put(
        f"/api/scanner/v1/jobs/{JOB_ID}",
        data=json.dumps({"scan_started_at": STARTED}),
        content_type="application/json",
        headers=_auth(api_key),
    )
    assert response.status_code == 201, response.content
    return api_key


def _auth(api_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _jpeg(label: bytes, size: tuple[int, int] = (64, 48)) -> bytes:
    digest = hashlib.sha256(label).digest()
    stream = io.BytesIO()
    Image.new("RGB", size, (digest[0], digest[1], digest[2])).save(stream, format="JPEG")
    return stream.getvalue()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _png_chunk(kind: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + kind
        + data
        + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
    )


def _png_with_undecodable_pixels(idat: bytes) -> bytes:
    """A 64x48 RGB PNG whose headers and chunk CRCs are valid (``verify()`` passes) but
    whose IDAT does not decode to the image's pixels."""
    header = struct.pack(">IIBBBBB", 64, 48, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", header)
        + _png_chunk(b"IDAT", idat)
        + _png_chunk(b"IEND", b"")
    )


def _multipart(
    metadata: dict[str, Any] | bytes | None,
    image: bytes | None,
    *,
    image_filename: str | None = "0000.jpg",
    metadata_filename: str | None = None,
) -> bytes:
    """A multipart body as sans sends it: a ``metadata`` JSON part and an ``image`` part."""
    parts: list[bytes] = []
    if metadata is not None:
        raw = metadata if isinstance(metadata, bytes) else json.dumps(metadata).encode()
        disposition = 'form-data; name="metadata"'
        if metadata_filename:
            disposition += f'; filename="{metadata_filename}"'
        parts.append(
            f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
            "Content-Type: application/json\r\n\r\n".encode()
            + raw
            + b"\r\n"
        )
    if image is not None:
        disposition = 'form-data; name="image"'
        if image_filename:
            disposition += f'; filename="{image_filename}"'
        parts.append(
            f"--{BOUNDARY}\r\nContent-Disposition: {disposition}\r\n"
            "Content-Type: image/jpeg\r\n\r\n".encode()
            + image
            + b"\r\n"
        )
    return b"".join(parts) + f"--{BOUNDARY}--\r\n".encode()


CONTENT_TYPE = f"multipart/form-data; boundary={BOUNDARY}"


def _page_meta(image: bytes, sequence: int, **overrides: Any) -> dict[str, Any]:
    return {
        "sha256": _sha(image),
        "camera_role": "left" if sequence % 2 == 0 else "right",
        "captured_at": "2026-11-17T14:33:12Z",
        "recognized_page_number": str(sequence + 1),
        "status": "ok",
        **overrides,
    }


def _put_page(
    api_key: str,
    sequence: int,
    image: bytes,
    *,
    job_id: str = JOB_ID,
    body: bytes | None = None,
    **overrides: Any,
) -> Any:
    payload = (
        body if body is not None else _multipart(_page_meta(image, sequence, **overrides), image)
    )
    return Client().put(
        f"/api/scanner/v1/jobs/{job_id}/pages/{sequence}",
        data=payload,
        content_type=CONTENT_TYPE,
        headers=_auth(api_key),
    )


def _put_cover(
    api_key: str,
    position: str,
    image: bytes,
    *,
    sha256: str | None = None,
    captured_at: Any = "2026-11-17T14:32:30Z",
) -> Any:
    metadata = {"sha256": sha256 or _sha(image), "captured_at": captured_at}
    return Client().put(
        f"/api/scanner/v1/jobs/{JOB_ID}/covers/{position}",
        data=_multipart(metadata, image, image_filename=f"cover-{position}.jpg"),
        content_type=CONTENT_TYPE,
        headers=_auth(api_key),
    )


def _scan_files(root: Path) -> list[Path]:
    base = root / scanner_uploads.SCANNING_STORAGE_ROOT
    if not base.exists():
        return []
    return sorted(path for path in base.rglob("*") if path.is_file())


def _staged_files(root: Path) -> list[Path]:
    staging = root / scanner_uploads.SCANNING_STORAGE_ROOT / scanner_uploads.STAGING_DIRNAME
    return sorted(staging.glob("*")) if staging.exists() else []


def _error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


# --- pages (§6.5) -------------------------------------------------------------------------


@pytest.mark.django_db
def test_new_page_is_version_one_from_the_scanner(key: str, tmp_path: Path) -> None:
    image = _jpeg(b"page-0")

    response = _put_page(key, 0, image)

    assert response.status_code == 201, response.content
    assert response.json() == {
        "sequence_number": 0,
        "sha256": _sha(image),
        "version": 1,
        "state": "capturing",
    }
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.state == ScanJob.State.CAPTURING
    page = ScanPage.objects.get(scan_job=job)
    assert page.logical_sequence_number == 0
    assert page.side == ScanPage.Side.SINGLE
    assert page.recognized_page_number == "1"
    assert page.status == "ok"
    version = page.versions.get()
    assert version.is_active and version.version == 1
    assert version.uploader is None
    assert version.source == ScanUploadSource.SCANNER
    assert version.checksum_sha256 == _sha(image)
    assert version.mime_type == "image/jpeg"
    assert version.size_bytes == len(image)
    assert version.capture_timestamp is not None
    assert version.scanner_metadata["camera_role"] == "left"
    assert version.storage_location.identifier == "scan"
    stored = tmp_path / "scan-artifacts" / version.storage_identifier
    assert stored.read_bytes() == image
    assert _staged_files(tmp_path) == []
    # GET jobs/{id} reports it (§6.4).
    status = Client().get(f"/api/scanner/v1/jobs/{JOB_ID}", headers=_auth(key)).json()
    assert status["pages"] == [{"sequence_number": 0, "sha256": _sha(image), "version": 1}]


@pytest.mark.django_db
def test_repeating_the_same_upload_creates_no_new_version(key: str, tmp_path: Path) -> None:
    image = _jpeg(b"page-1")
    assert _put_page(key, 1, image).status_code == 201

    repeated = _put_page(key, 1, image)

    assert repeated.status_code == 200, repeated.content
    assert repeated.json()["version"] == 1
    assert ScanPageVersion.objects.filter(scan_page__scan_job_id=JOB_ID).count() == 1
    assert len(_scan_files(tmp_path)) == 1
    assert _staged_files(tmp_path) == []


@pytest.mark.django_db
def test_a_different_image_for_an_existing_page_is_page_conflict(key: str, tmp_path: Path) -> None:
    first = _jpeg(b"first")
    assert _put_page(key, 2, first).status_code == 201

    response = _put_page(key, 2, _jpeg(b"second"))

    assert response.status_code == 409
    assert _error_code(response) == "page_conflict"
    assert response.json()["error"]["details"]["sha256"] == _sha(first)
    version = ScanPageVersion.objects.get(scan_page__scan_job_id=JOB_ID)
    assert version.checksum_sha256 == _sha(first)
    assert len(_scan_files(tmp_path)) == 1


@pytest.mark.django_db
@pytest.mark.parametrize(("sequence", "role"), [(0, "right"), (1, "left"), (12, "right")])
def test_camera_role_must_match_sequence_parity(key: str, sequence: int, role: str) -> None:
    response = _put_page(key, sequence, _jpeg(b"parity"), camera_role=role)

    assert response.status_code == 422
    assert _error_code(response) == "camera_role_parity"
    assert not ScanPageVersion.objects.exists()
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.OPEN


@pytest.mark.django_db
def test_checksum_mismatch_is_rejected_and_nothing_is_kept(key: str, tmp_path: Path) -> None:
    response = _put_page(key, 0, _jpeg(b"real"), sha256=_sha(b"something else"))

    assert response.status_code == 422
    assert _error_code(response) == "checksum_mismatch"
    assert not ScanPage.objects.exists()
    assert _scan_files(tmp_path) == []


@pytest.mark.django_db
def test_undecodable_image_is_invalid_image(key: str, tmp_path: Path) -> None:
    truncated = _jpeg(b"cut", size=(320, 240))[:200]

    response = _put_page(key, 0, truncated)

    assert response.status_code == 422
    assert _error_code(response) == "invalid_image"
    assert not ScanPageVersion.objects.exists()
    assert _scan_files(tmp_path) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "idat",
    [
        pytest.param(b"not a zlib stream" * 4, id="broken-zlib"),
        pytest.param(zlib.compress(b"\x00" * 50), id="too-few-pixels"),
    ],
)
@pytest.mark.parametrize("target", ["page", "cover"])
def test_png_with_valid_checksums_but_undecodable_pixels_is_invalid_image(
    key: str, tmp_path: Path, idat: bytes, target: str
) -> None:
    image = _png_with_undecodable_pixels(idat)
    with Image.open(io.BytesIO(image)) as probe:
        probe.verify()  # the structure and the chunk CRCs are fine

    response = _put_page(key, 0, image) if target == "page" else _put_cover(key, "front", image)

    assert response.status_code == 422, response.content
    assert _error_code(response) == "invalid_image"
    assert not ScanPageVersion.objects.exists()
    assert not ScanCover.objects.exists()
    assert _scan_files(tmp_path) == []
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.OPEN


@pytest.mark.django_db
def test_decompression_bomb_is_invalid_image(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(Image, "MAX_IMAGE_PIXELS", 1000)  # 64x48 = 3072 pixels > limit

    response = _put_page(key, 0, _jpeg(b"bomb"))

    assert response.status_code == 422
    assert _error_code(response) == "invalid_image"
    assert not ScanPageVersion.objects.exists()
    assert _scan_files(tmp_path) == []


@pytest.mark.django_db
@pytest.mark.parametrize(
    "captured_at", ["2026-02-30T14:32:30Z", "2026-13-01T14:32:30Z", "yesterday", 12]
)
def test_malformed_cover_captured_at_is_400(key: str, captured_at: Any) -> None:
    response = _put_cover(key, "front", _jpeg(b"cover"), captured_at=captured_at)

    assert response.status_code == 400, response.content
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["details"]["field"] == "captured_at"
    assert not ScanCover.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"sha256": "ABC"}, "sha256"),
        ({"camera_role": "top"}, "camera_role"),
        ({"captured_at": "yesterday"}, "captured_at"),
        # ISO-shaped but not a calendar date: 400, never 500.
        ({"captured_at": "2026-02-30T14:33:12Z"}, "captured_at"),
        ({"captured_at": "2026-11-17T25:00:00Z"}, "captured_at"),
        ({"recognized_page_number": 23}, "recognized_page_number"),
        ({"status": "not ok!"}, "status"),
    ],
)
def test_malformed_page_metadata_is_400(key: str, overrides: dict[str, Any], field: str) -> None:
    response = _put_page(key, 0, _jpeg(b"meta"), **overrides)

    assert response.status_code == 400
    assert response.json()["error"]["details"]["field"] == field


@pytest.mark.django_db
def test_missing_parts_and_bad_bodies(key: str) -> None:
    image = _jpeg(b"parts")
    no_image = _put_page(key, 0, image, body=_multipart(_page_meta(image, 0), None))
    assert no_image.status_code == 400
    assert no_image.json()["error"]["details"]["field"] == "image"

    no_metadata = _put_page(key, 0, image, body=_multipart(None, image))
    assert no_metadata.status_code == 400
    assert no_metadata.json()["error"]["details"]["field"] == "metadata"

    bad_json = _put_page(key, 0, image, body=_multipart(b"[1, 2", image))
    assert _error_code(bad_json) == "invalid_json"

    not_multipart = Client().put(
        f"/api/scanner/v1/jobs/{JOB_ID}/pages/0",
        data=image,
        content_type="image/jpeg",
        headers=_auth(key),
    )
    assert not_multipart.status_code == 415
    assert not ScanPageVersion.objects.exists()


@pytest.mark.django_db
def test_parts_without_filenames_and_metadata_as_a_file_are_accepted(key: str) -> None:
    image = _jpeg(b"no-filename")
    body = _multipart(_page_meta(image, 0), image, image_filename=None, metadata_filename="m.json")

    response = _put_page(key, 0, image, body=body)

    assert response.status_code == 201, response.content


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("job_id", "sequence", "status", "code"),
    [
        ("not-a-job", "0", 400, "invalid_job_id"),
        (JOB_ID, "-1", 400, "invalid_sequence_number"),
        (JOB_ID, "x", 400, "invalid_sequence_number"),
        (JOB_ID, "1000000", 400, "invalid_sequence_number"),
        ("20261117T999999Z", "0", 404, "not_found"),
    ],
)
def test_bad_ids_are_refused_before_the_body_is_read(
    key: str, job_id: str, sequence: str, status: int, code: str
) -> None:
    response = Client().put(
        f"/api/scanner/v1/jobs/{job_id}/pages/{sequence}",
        data=_multipart({"sha256": "0" * 64}, b"x"),
        content_type=CONTENT_TYPE,
        headers=_auth(key),
    )
    assert response.status_code == status
    assert _error_code(response) == code


@pytest.mark.django_db
def test_a_web_created_job_is_404(key: str) -> None:
    web_job_id = "20261117T150000Z"
    ScanJob.objects.create(id=web_job_id, source=ScanJob.Source.WEB)
    image = _jpeg(b"web")

    page = _put_page(key, 0, image, job_id=web_job_id)
    cover = Client().put(
        f"/api/scanner/v1/jobs/{web_job_id}/covers/front",
        data=_multipart({"sha256": _sha(image)}, image),
        content_type=CONTENT_TYPE,
        headers=_auth(key),
    )

    assert page.status_code == 404
    assert cover.status_code == 404
    assert not ScanPageVersion.objects.exists()
    assert not ScanCover.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize("state", [ScanJob.State.CAPTURED, ScanJob.State.CANCELLED])
def test_pages_outside_open_or_capturing_are_job_state(key: str, state: str) -> None:
    ScanJob.objects.filter(pk=JOB_ID).update(state=state)

    response = _put_page(key, 0, _jpeg(b"late"))

    assert response.status_code == 409
    assert _error_code(response) == "job_state"
    assert response.json()["error"]["details"]["state"] == state
    assert not ScanPageVersion.objects.exists()


@pytest.mark.django_db
def test_job_state_is_rechecked_under_the_lock(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """capture-complete/abort between the pre-check and storing: refused, file removed."""
    original = scanner_uploads.publish_staged

    def publish_then_abort(*args: Any, **kwargs: Any) -> str:
        destination = original(*args, **kwargs)
        ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.CANCELLED)
        return destination

    monkeypatch.setattr(scanner_uploads, "publish_staged", publish_then_abort)

    response = _put_page(key, 0, _jpeg(b"race"))

    assert response.status_code == 409
    assert _error_code(response) == "job_state"
    assert not ScanPageVersion.objects.exists()
    assert _scan_files(tmp_path) == []


@pytest.mark.django_db
def test_size_limit_is_413(key: str, settings: Any, tmp_path: Path) -> None:
    image = _jpeg(b"big", size=(400, 300))
    settings.BOOK_SCANNER_SCANNER_MAX_IMAGE_BYTES = len(image) - 1

    # Within Content-Length allowance (limit + multipart overhead): caught while streaming.
    streamed = _put_page(key, 0, image)
    assert streamed.status_code == 413
    assert _error_code(streamed) == "payload_too_large"

    # A Content-Length above limit + overhead is refused before reading the body.
    settings.BOOK_SCANNER_SCANNER_MAX_IMAGE_BYTES = 1
    padded = _multipart(
        _page_meta(image, 0), image + b"\0" * scanner_uploads.MULTIPART_OVERHEAD_BYTES
    )
    early = _put_page(key, 0, image, body=padded)
    assert early.status_code == 413

    assert not ScanPageVersion.objects.exists()
    assert _scan_files(tmp_path) == []


@pytest.mark.django_db
def test_the_view_never_reads_the_whole_body(key: str, mocker: Any, tmp_path: Path) -> None:
    """The body is streamed in chunks: ``request.body`` (the whole body) is never read."""
    mocker.patch.object(
        HttpRequest,
        "body",
        new_callable=PropertyMock,
        side_effect=AssertionError("request.body must not be read"),
    )
    image = _jpeg(b"stream", size=(800, 600))

    response = _put_page(key, 0, image)

    assert response.status_code == 201, response.content


# --- interrupted uploads ------------------------------------------------------------------


class _DroppingStream:
    """A request stream that dies after ``limit`` bytes, as on a killed connection."""

    def __init__(self, data: bytes, limit: int, *, error: bool = True) -> None:
        self._data = io.BytesIO(data[:limit])
        self._error = error
        self.read_sizes: list[int] = []

    def read(self, size: int = -1) -> bytes:
        self.read_sizes.append(size)
        chunk = self._data.read(size)
        if not chunk and self._error:
            raise OSError("connection reset by peer")
        return chunk

    def readline(self, size: int = -1) -> bytes:  # pragma: no cover - not used
        return self._data.readline(size)


def _factory_put(api_key: str, sequence: int, body: bytes, stream: Any) -> Any:
    request = RequestFactory().put(
        f"/api/scanner/v1/jobs/{JOB_ID}/pages/{sequence}",
        data=body,
        content_type=CONTENT_TYPE,
        headers=_auth(api_key),
    )
    request._stream = stream
    return scanner_api.page(request, job_id=JOB_ID, sequence_number=str(sequence))


@pytest.mark.django_db
@pytest.mark.parametrize("error", [True, False], ids=["connection-reset", "body-ends-early"])
def test_killed_connection_leaves_no_partial_version(key: str, tmp_path: Path, error: bool) -> None:
    image = _jpeg(b"killed", size=(1600, 1200))
    body = _multipart(_page_meta(image, 0), image)
    stream = _DroppingStream(body, len(body) // 2, error=error)

    response = _factory_put(key, 0, body, stream)

    assert response.status_code == 400
    assert json.loads(response.content)["error"]["code"] == "upload_interrupted"
    assert not ScanPage.objects.exists()
    assert not ScanPageVersion.objects.exists()
    assert _scan_files(tmp_path) == []  # the staged partial file is gone
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.OPEN
    # Read in bounded chunks, never "everything at once".
    assert stream.read_sizes and all(0 < size <= 64 * 1024 for size in stream.read_sizes)

    # The scanner retries with the full body: version 1, as if nothing had happened.
    retried = _put_page(key, 0, image)
    assert retried.status_code == 201
    assert ScanPageVersion.objects.get().version == 1


@pytest.mark.django_db
def test_a_crash_while_recording_the_version_leaves_no_files(
    key: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(**kwargs: Any) -> Any:
        raise RuntimeError("database went away")

    monkeypatch.setattr(ScanPageVersion.objects, "create", boom)
    client = Client(raise_request_exception=True)
    image = _jpeg(b"crash")
    with pytest.raises(RuntimeError, match="database went away"):
        client.put(
            f"/api/scanner/v1/jobs/{JOB_ID}/pages/0",
            data=_multipart(_page_meta(image, 0), image),
            content_type=CONTENT_TYPE,
            headers=_auth(key),
        )

    assert not ScanPageVersion.objects.exists()
    assert _scan_files(tmp_path) == []
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.OPEN


# --- covers (§6.6) ------------------------------------------------------------------------


@pytest.mark.django_db
def test_covers_are_versioned_and_idempotent(key: str, tmp_path: Path) -> None:
    raw = _jpeg(b"front-raw")
    created = _put_cover(key, "front", raw)
    assert created.status_code == 201, created.content
    assert created.json() == {
        "position": "front",
        "sha256": _sha(raw),
        "version": 1,
        "state": "open",
    }

    repeated = _put_cover(key, "front", raw)
    assert repeated.status_code == 200
    assert ScanCover.objects.count() == 1

    rectified = _jpeg(b"front-rectified")
    second = _put_cover(key, "front", rectified)
    assert second.status_code == 201
    assert second.json()["version"] == 2

    versions = list(ScanCover.objects.filter(scan_job_id=JOB_ID).order_by("version"))
    assert [(v.version, v.is_active) for v in versions] == [(1, False), (2, True)]
    assert all(v.uploader is None and v.source == ScanUploadSource.SCANNER for v in versions)
    for version in versions:  # earlier versions are kept on storage
        assert (tmp_path / "scan-artifacts" / version.storage_identifier).is_file()
    # A cover does not start capturing: only the first page does.
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.OPEN

    status = Client().get(f"/api/scanner/v1/jobs/{JOB_ID}", headers=_auth(key)).json()
    assert status["covers"] == [{"position": "front", "sha256": _sha(rectified), "version": 2}]


@pytest.mark.django_db
def test_cover_rules(key: str) -> None:
    image = _jpeg(b"cover")
    assert _put_cover(key, "spine", image).status_code == 400
    mismatch = _put_cover(key, "back", image, sha256=_sha(b"other"))
    assert mismatch.status_code == 422
    assert _error_code(mismatch) == "checksum_mismatch"

    ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.CAPTURED)
    assert _put_cover(key, "back", image).status_code == 201  # allowed in captured

    for state in (ScanJob.State.CANCELLED, ScanJob.State.COMPLETE):
        ScanJob.objects.filter(pk=JOB_ID).update(state=state)
        refused = _put_cover(key, "back", _jpeg(state.encode()))
        assert refused.status_code == 409
        assert _error_code(refused) == "job_state"
    assert ScanCover.objects.count() == 1


@pytest.mark.django_db
def test_capture_complete_checks_covers(key: str) -> None:
    page = _jpeg(b"p0")
    front, back = _jpeg(b"front"), _jpeg(b"back")
    assert _put_page(key, 0, page).status_code == 201
    assert _put_cover(key, "front", front).status_code == 201
    manifest = {
        "finished_at": "2026-11-17T15:40:00Z",
        "sans_last_line_no": 0,
        "pages": [{"sequence_number": 0, "sha256": _sha(page)}],
        "covers": [
            {"position": "front", "sha256": _sha(front)},
            {"position": "back", "sha256": _sha(back)},
        ],
        "metadata_opf": None,
    }

    def capture_complete() -> Any:
        return Client().post(
            f"/api/scanner/v1/jobs/{JOB_ID}/capture-complete",
            data=json.dumps(manifest),
            content_type="application/json",
            headers=_auth(key),
        )

    incomplete = capture_complete()
    assert incomplete.status_code == 409
    assert incomplete.json()["error"]["details"]["missing"] == ["cover:back"]

    assert _put_cover(key, "back", back).status_code == 201
    verified = capture_complete()
    assert verified.status_code == 200, verified.content
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.CAPTURED

    manifest["covers"] = [{"position": "side", "sha256": _sha(back)}]
    ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.CAPTURING)
    assert capture_complete().status_code == 400


# --- finish: covers onto the Book ---------------------------------------------------------


def _existing_front_cover(book: Book) -> CoverImage:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    cover = CoverImage.objects.create(
        book=book,
        position=CoverImage.CoverPosition.FRONT,
        storage_location=location,
        storage_identifier="books/old-front.jpg",
        mime_type="image/jpeg",
        size_bytes=10,
        sha256="e" * 64,
        extractor_tool="zipfile+pillow",
    )
    book.front_cover = cover
    book.save(update_fields=["front_cover"])
    return cover


@pytest.mark.django_db(transaction=True)
def test_covers_appear_on_the_book_after_finish(
    key: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    page, front, back = _jpeg(b"p0"), _jpeg(b"front", (600, 800)), _jpeg(b"back", (600, 800))
    assert _put_page(key, 0, page).status_code == 201
    assert _put_cover(key, "front", front).status_code == 201
    assert _put_cover(key, "back", back).status_code == 201
    book = Book.objects.create(title="Scanned With Covers")
    old_front = _existing_front_cover(book)
    ScanJob.objects.filter(pk=JOB_ID).update(book=book)
    member = Client()
    member.force_login(get_or_create_profile("cover-finisher").user)

    finished = member.post(f"/scan/jobs/{JOB_ID}/finish/", data={"allow_raw_pages": "true"})

    assert finished.status_code == 202, finished.content
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.COMPLETE
    book.refresh_from_db()
    assert book.front_cover is not None and book.back_cover is not None
    for cover, position in ((book.front_cover, "front"), (book.back_cover, "back")):
        assert cover.source == CoverImage.Source.SCANNER
        assert cover.position == position
        assert cover.scan_cover == ScanCover.objects.get(scan_job_id=JOB_ID, position=position)
        assert cover.mime_type == "image/jpeg"
        stored = tmp_path / "library-artifacts" / cover.storage_identifier
        assert hashlib.sha256(stored.read_bytes()).hexdigest() == cover.sha256
        with Image.open(stored) as rendered:
            assert rendered.format == "JPEG"
    # The front cover replaced an extracted one and records what it replaced.
    assert book.front_cover.replaced["cover_id"] == old_front.pk
    assert book.front_cover.replaced["source"] == CoverImage.Source.EXTRACTED
    assert book.back_cover.replaced == {}
    # The raw photos stay in scan storage.
    for scan_cover in ScanCover.objects.all():
        assert (tmp_path / "scan-artifacts" / scan_cover.storage_identifier).is_file()

    # Applying again (a resumed finish) changes nothing.
    job = ScanJob.objects.get(pk=JOB_ID)
    assert scan_covers.apply_scan_covers_to_book(job) == []
    assert CoverImage.objects.filter(book=book).count() == 2


@pytest.mark.django_db
def test_jobs_without_covers_or_book_apply_nothing(key: str) -> None:
    job = ScanJob.objects.get(pk=JOB_ID)
    assert scan_covers.apply_scan_covers_to_book(job) == []
    job.book = Book.objects.create(title="No Covers")
    job.save(update_fields=["book"])
    assert scan_covers.apply_scan_covers_to_book(job) == []
    assert not CoverImage.objects.exists()


@pytest.fixture
def _no_s5(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[ScanPage]]:
    calls: list[ScanPage] = []
    monkeypatch.setattr(scanner_uploads, "project_page_from_events", calls.append)
    yield calls


@pytest.mark.django_db
def test_page_events_are_projected_after_the_page_is_created(
    key: str, _no_s5: list[ScanPage]
) -> None:
    """S5 (#129) hook: a ``page`` event that arrived first is applied to the new page."""
    assert _put_page(key, 0, _jpeg(b"hook")).status_code == 201
    assert [page.logical_sequence_number for page in _no_s5] == [0]
