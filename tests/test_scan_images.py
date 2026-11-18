"""Working images made once per page version, and stages that never decode the scan (#195)."""

from __future__ import annotations

import io
import json
from collections.abc import Iterable
from pathlib import Path
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.http import StreamingHttpResponse
from PIL import Image, ImageDraw

from ebook_server.core import scan_images, scantailor
from ebook_server.core.models import ScanPage, ScanPageImage, ScanTailorStageState
from ebook_server.core.storage_registry import filesystem_backend_for_location
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page


@pytest.mark.parametrize(
    ("size", "dpi", "expected"),
    [
        ((2480, 3508), (300, 300), 0.5),  # A4 at 300 dpi -> 150 dpi
        ((4960, 7016), (600, 600), 0.25),  # A4 at 600 dpi -> 150 dpi
        ((6000, 9000), (100, 100), 2400 / 9000),  # capped by the maximum edge
        ((1200, 1600), (300, 300), 1.0),  # never below the minimum edge, never upscaled
        ((2000, 2800), (300, 300), 1600 / 2800),
    ],
)
def test_work_scale(size: tuple[int, int], dpi: tuple[int, int], expected: float) -> None:
    assert scan_images.work_scale(size, dpi) == pytest.approx(expected)


def _scan_bytes(width: int, height: int) -> bytes:
    image = Image.new("L", (width, height), 236)
    draw = ImageDraw.Draw(image)
    for y in range(300, height - 300, 70):
        draw.rectangle((250, y, width - 250, y + 22), fill=25)
    image = image.rotate(2.0, expand=False, fillcolor=236)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", dpi=(300, 300))
    return buffer.getvalue()


def _run(client: Any, job_id: str, page_id: str, stage: str, parameters: dict[str, Any]) -> None:
    response = client.post(
        f"/scan/jobs/{job_id}/pages/{page_id}/stages/{stage}/",
        data=json.dumps({"parameters": parameters}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content
    state = ScanTailorStageState.objects.get(scan_page_id=page_id, stage=stage)
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS, state.last_error


@pytest.mark.django_db(transaction=True)
def test_stages_work_on_the_working_image_and_output_renders_the_original_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job = _create_scan_job("scan-images-pipeline")
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_scan_bytes(2000, 2800),
        filename="page.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1)
    version = page.active_version()
    assert version is not None

    # Generated once, at upload, by the post-commit job (inline without a queue).
    page_image = ScanPageImage.objects.get(version=version)
    assert (page_image.full_width, page_image.full_height) == (2000, 2800)
    assert (page_image.work_width, page_image.work_height) == (1143, 1600)
    storage = scantailor._scan_storage()
    with Image.open(storage.resolve_location(page_image.thumbnail_storage_identifier)) as thumb:
        assert max(thumb.size) <= scan_images.THUMBNAIL_EDGE

    original = Path(
        filesystem_backend_for_location(version.storage_location).resolve_location(
            version.storage_identifier
        )
    )
    loaded: list[Path] = []
    real_load = scantailor._load_image

    def recording_load(path: Path) -> Image.Image:
        loaded.append(Path(path))
        return real_load(path)

    monkeypatch.setattr(scantailor, "_load_image", recording_load)

    for stage, parameters in (
        ("deskew", {}),
        ("select_content", {}),
        ("dewarping", {"dewarp_mode": "bypass"}),
        ("margins", {"margin": 40}),
    ):
        _run(client, str(job.id), str(page.id), stage, parameters)
    assert original not in loaded, "stages 1-4 must not decode the full-resolution scan"

    states = {state.stage: state for state in ScanTailorStageState.objects.filter(scan_page=page)}
    deskew = states["deskew"]
    # Parameters are in full-resolution coordinates; artifacts are working-resolution.
    assert (deskew.parameters["input_width"], deskew.parameters["input_height"]) == (2000, 2800)
    assert deskew.parameters["transform"]["op"] == "rotate"
    assert abs(deskew.parameters["angle"]) > 0.5
    with Image.open(storage.resolve_location(deskew.output_storage_identifier)) as work:
        assert max(work.size) < 1800
    assert deskew.preview_storage_identifier.endswith(".jpg")
    margins = states["margins"]
    expected_full = (margins.parameters["output_width"], margins.parameters["output_height"])
    assert expected_full[0] == states["select_content"].parameters["output_width"] + 80

    _run(client, str(job.id), str(page.id), "output", {"output_mode": "otsu", "zone_mode": "off"})
    assert loaded.count(original) == 1, "Output renders from the original exactly once"
    output = ScanTailorStageState.objects.get(scan_page=page, stage="output")
    with Image.open(storage.resolve_location(output.output_storage_identifier)) as rendered:
        assert rendered.size == expected_full
        assert rendered.info.get("dpi") is not None
        assert round(rendered.info["dpi"][0]) == 300
    with Image.open(storage.resolve_location(output.preview_storage_identifier)) as preview:
        assert max(preview.size) < max(expected_full)


@pytest.mark.django_db(transaction=True)
def test_missing_working_image_is_rebuilt_and_decode_errors_fail_the_stage() -> None:
    client, job = _create_scan_job("scan-images-rebuild")
    payload = _scan_bytes(900, 1200)
    _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=payload, filename="p.png"
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1)
    version = page.active_version()
    assert version is not None
    ScanPageImage.objects.filter(version=version).delete()
    _run(client, str(job.id), str(page.id), "deskew", {})
    assert ScanPageImage.objects.filter(version=version).exists()

    # The stored scan is damaged later and the working image is gone: a clear error.
    ScanPageImage.objects.filter(version=version).delete()
    original = filesystem_backend_for_location(version.storage_location).resolve_location(
        version.storage_identifier
    )
    Path(original).write_bytes(payload[: len(payload) // 3])
    response = client.post(
        f"/scan/jobs/{job.id}/pages/{page.id}/stages/deskew/",
        data=json.dumps({"parameters": {"manual_angle": 1}}),
        content_type="application/json",
    )
    assert response.status_code == 202
    state = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    assert state.status == ScanTailorStageState.StageStatus.FAILED
    assert "cannot read image" in state.last_error


def _input_preview_url(job_id: str, page_id: str) -> str:
    return f"/scan/jobs/{job_id}/pages/{page_id}/stages/deskew/preview/?input=true"


def _migrated_page(name: str) -> tuple[Any, Any, ScanPage, Any]:
    """A page whose version has no ScanPageImage, as after migration 0050."""
    client, job = _create_scan_job(name)
    _upload_scan_page(
        client,
        str(job.id),
        sequence=1,
        side="single",
        payload=_scan_bytes(900, 1200),
        filename="p.png",
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1)
    version = page.active_version()
    assert version is not None
    ScanPageImage.objects.filter(version=version).delete()
    # Opening the workspace creates the page's stage rows, as in the browser.
    assert client.get(f"/scan/jobs/{job.id}/workspace/").status_code == 200
    assert not ScanPageImage.objects.filter(version=version).exists()
    return client, job, page, version


@pytest.mark.django_db(transaction=True)
def test_first_input_preview_of_a_migrated_page_is_served_once_generated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job, page, version = _migrated_page("scan-images-first-open")
    generated: list[int] = []
    real_job = scan_images.generate_page_image_job

    def counting_job(version_id: int) -> None:
        generated.append(version_id)
        real_job(version_id)

    monkeypatch.setattr(scan_images, "generate_page_image_job", counting_job)

    # The queue finished the job by the time the request looked again: no 404 first.
    response = client.get(_input_preview_url(str(job.id), str(page.id)))
    assert response.status_code == 200
    body = b"".join(cast(Iterable[bytes], cast(StreamingHttpResponse, response).streaming_content))
    with Image.open(io.BytesIO(body)) as image:
        assert image.format == "PNG"
        assert image.size == (900, 1200)
    assert generated == [version.pk]

    client.get(_input_preview_url(str(job.id), str(page.id)))
    assert generated == [version.pk], "an existing working image is not generated again"


class _FakeRedis:
    def __init__(self) -> None:
        self.keys: dict[str, Any] = {}

    def set(self, key: str, value: Any, *, nx: bool = False, ex: int | None = None) -> bool:
        assert ex is not None and ex > 0
        if nx and key in self.keys:
            return False
        self.keys[key] = value
        return True


class _FakeQueue:
    """Stands in for jobs.ConfiguredQueue: records jobs instead of running them."""

    def __init__(self) -> None:
        self.queue = type("RQQueue", (), {"connection": _FakeRedis()})()
        self.jobs: list[tuple[Any, tuple[Any, ...]]] = []

    def enqueue(self, func: Any, *args: Any, **kwargs: Any) -> None:
        self.jobs.append((func, args))


@pytest.mark.django_db(transaction=True)
def test_input_preview_is_retryable_while_generation_is_queued_and_queued_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job, page, version = _migrated_page("scan-images-queued")
    queue = _FakeQueue()
    monkeypatch.setattr(scan_images, "get_queue", lambda: queue)
    monkeypatch.setattr(
        scan_images, "enqueue", lambda func, *args, **kwargs: queue.enqueue(func, *args)
    )
    url = _input_preview_url(str(job.id), str(page.id))

    for _ in range(3):  # the editor keeps asking while the worker is busy
        response = client.get(url)
        assert response.status_code == 503
        assert response.content == b"input preview not ready"
        assert response["Retry-After"] == "2"
        assert "no-store" in response["Cache-Control"]
    assert queue.jobs == [(scan_images.generate_page_image_job, (version.pk,))]

    scan_images.generate_page_image_job(version.pk)  # the worker gets to it
    response = client.get(url)
    assert response.status_code == 200
    assert len(queue.jobs) == 1


@pytest.mark.django_db(transaction=True)
def test_backfill_command_generates_missing_working_images() -> None:
    _client, _job, _page, version = _migrated_page("scan-images-backfill")
    out = io.StringIO()
    call_command("backfill_scan_page_images", "--dry-run", stdout=out)
    assert "1 without a working image (dry run" in out.getvalue()
    assert not ScanPageImage.objects.filter(version=version).exists()

    out = io.StringIO()
    call_command("backfill_scan_page_images", stdout=out)  # no queue: runs inline
    assert "1 without a working image, 1 queued" in out.getvalue()
    assert ScanPageImage.objects.filter(version=version).exists()

    out = io.StringIO()
    call_command("backfill_scan_page_images", stdout=out)
    assert "0 without a working image" in out.getvalue()


@pytest.mark.django_db(transaction=True)
def test_browser_fixture_migrated_page_opens_with_its_input_preview() -> None:
    from django.test import Client

    from ebook_server.core.models import get_or_create_profile
    from tests.browser_fixtures import MIGRATED_SCAN_JOB_ID, seed_migrated_scan_page

    seed_migrated_scan_page()
    page = ScanPage.objects.get(scan_job_id=MIGRATED_SCAN_JOB_ID)
    assert not ScanPageImage.objects.filter(version__scan_page=page).exists()
    client = Client()
    client.force_login(get_or_create_profile("fry").user)
    assert client.get(f"/scan/jobs/{MIGRATED_SCAN_JOB_ID}/workspace/").status_code == 200
    response = client.get(_input_preview_url(MIGRATED_SCAN_JOB_ID, str(page.id)))
    assert response.status_code == 200
    assert ScanPageImage.objects.filter(version__scan_page=page).count() == 1
