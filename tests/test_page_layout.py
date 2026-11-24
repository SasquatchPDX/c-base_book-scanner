"""Margins: one common page size for all pages (#208, upstream ``page_layout``)."""

from __future__ import annotations

import io
import json
from typing import Any

import pytest
from django.test import Client
from PIL import Image, ImageDraw

from ebook_server.core import page_layout, scantailor
from ebook_server.core.models import ProcessingJob, ScanJob, ScanPage, ScanTailorStageState
from tests.stage_helpers import run_stage
from tests.test_scan_jobs import _create_scan_job, _upload_scan_page

MARGIN_MM = 5.0
# round(5 mm * 300 dpi / 25.4) = 59 px per side at the default 300 dpi.
MARGIN_PX = 59
SUCCESS = ScanTailorStageState.StageStatus.SUCCESS
FAILED = ScanTailorStageState.StageStatus.FAILED


# --- pure port ------------------------------------------------------------------------


def test_defaults_are_upstreams() -> None:
    settings = page_layout.settings_from_parameters({}, (300, 300))
    assert settings.hard_margins_mm == page_layout.Margins(10.0, 5.0, 10.0, 5.0)
    assert (settings.align_x, settings.align_y) == ("center", "top")
    assert settings.align_with_others is True


def test_legacy_pixel_margins_are_converted_with_the_page_dpi() -> None:
    settings = page_layout.settings_from_parameters({"margin": 60, "left": 120}, (600, 300))
    assert settings.hard_margins_mm.left == pytest.approx(120 * 25.4 / 600)
    assert settings.hard_margins_mm.right == pytest.approx(60 * 25.4 / 600)
    assert settings.hard_margins_mm.top == pytest.approx(60 * 25.4 / 300)


@pytest.mark.parametrize(
    ("align_x", "align_y", "expected"),
    [
        ("left", "top", (0.0, 0.0, 20.0, 10.0)),
        ("center", "center", (10.0, 5.0, 10.0, 5.0)),
        ("right", "bottom", (20.0, 10.0, 0.0, 0.0)),
    ],
)
def test_soft_margins_split_by_alignment(
    align_x: str, align_y: str, expected: tuple[float, float, float, float]
) -> None:
    settings = page_layout.LayoutSettings(page_layout.Margins(0, 0, 0, 0), align_x, align_y)
    soft = page_layout.soft_margins((80.0, 90.0), (100.0, 100.0), settings)
    assert (soft.left, soft.top, soft.right, soft.bottom) == expected


def test_null_alignment_has_no_soft_margins_and_no_influence() -> None:
    settings = page_layout.LayoutSettings(page_layout.Margins(0, 0, 0, 0), align_with_others=False)
    soft = page_layout.soft_margins((10.0, 10.0), (100.0, 100.0), settings)
    assert soft == page_layout.Margins(0, 0, 0, 0)
    aggregate = page_layout.aggregate_hard_size(
        [
            page_layout.PageEntry("a", (50.0, 60.0), True),
            page_layout.PageEntry("b", (500.0, 600.0), False),
        ]
    )
    assert aggregate.size_mm == (50.0, 60.0)
    assert aggregate.fingerprint == page_layout.fingerprint(50.0, 60.0)
    assert aggregate.fingerprint != page_layout.fingerprint(50.0, 60.5)


def test_run_without_a_job_keeps_content_plus_margins() -> None:
    source = Image.new("L", (100, 150), 0)
    result = run_stage(
        scantailor._run_margins,
        ScanTailorStageState(parameters={f"{side}_mm": MARGIN_MM for side in _SIDES}),
        source,
    )
    assert result.full_size == (100 + 2 * MARGIN_PX, 150 + 2 * MARGIN_PX)
    assert result.parameters["transform"]["offset"] == [MARGIN_PX, MARGIN_PX]
    assert result.parameters["hard_margins_mm"]["left"] == MARGIN_MM


# --- the job-wide aggregate -----------------------------------------------------------

_SIDES = ("left", "top", "right", "bottom")


def _page_png(
    size: tuple[int, int] = (300, 400), dpi: int | None = None, blank: bool = False
) -> bytes:
    image = Image.new("L", size, 255)
    if not blank:
        draw = ImageDraw.Draw(image)
        for y in range(40, size[1] - 40, 16):
            draw.line((30, y, size[0] - 30, y), fill=0, width=3)
    stream = io.BytesIO()
    if dpi is None:
        image.save(stream, format="PNG")
    else:
        image.save(stream, format="PNG", dpi=(dpi, dpi))
    return stream.getvalue()


def _url(job: ScanJob, page: ScanPage, stage: str) -> str:
    return f"/scan/jobs/{job.id}/pages/{page.id}/stages/{stage}/"


def _post(
    client: Client, job: ScanJob, page: ScanPage, stage: str, parameters: dict[str, Any]
) -> None:
    response = client.post(
        _url(job, page, stage),
        data=json.dumps({"parameters": parameters}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content


def _state(page: ScanPage, stage: str) -> ScanTailorStageState:
    return ScanTailorStageState.objects.get(scan_page=page, stage=stage)


def _margins(**overrides: Any) -> dict[str, Any]:
    parameters: dict[str, Any] = {f"{side}_mm": MARGIN_MM for side in _SIDES}
    parameters.update({"align_x": "center", "align_y": "center", "align_with_others": True})
    parameters.update(overrides)
    return parameters


def _job_with_pages(
    user: str, count: int, *, dpis: list[int | None] | None = None, blank: set[int] | None = None
) -> tuple[Client, ScanJob, list[ScanPage]]:
    client, job = _create_scan_job(user)
    for index in range(count):
        dpi = dpis[index] if dpis else None
        size = (600, 800) if dpi == 600 else (300, 400)
        response = _upload_scan_page(
            client,
            str(job.id),
            sequence=index + 1,
            side="single",
            payload=_page_png(size, dpi, blank=index in (blank or set())),
            filename=f"page{index + 1}.png",
        )
        assert response.status_code in {200, 201}, response.content
    pages = list(ScanPage.objects.filter(scan_job=job).order_by("logical_sequence_number"))
    return client, job, pages


def _prepare(client: Client, job: ScanJob, page: ScanPage, box: list[int] | None) -> None:
    """Deskew 0°, Select Content (manual ``box`` or auto), Dewarping off."""
    _post(client, job, page, "deskew", {"manual_angle": 0})
    content: dict[str, Any] = (
        {"content_mode": "auto"} if box is None else {"content_mode": "manual", "content_box": box}
    )
    _post(client, job, page, "select_content", content)
    _post(client, job, page, "dewarping", {"dewarp_mode": "off"})


def _size(page: ScanPage) -> tuple[int, int]:
    parameters = _state(page, "margins").parameters
    return int(parameters["output_width"]), int(parameters["output_height"])


def _requeued() -> int:
    return ProcessingJob.objects.filter(details__trigger=scantailor.AGGREGATE_TRIGGER).count()


def _assert_nothing_failed(job: ScanJob) -> None:
    failed = ScanTailorStageState.objects.filter(scan_job=job, status=FAILED)
    assert not failed.exists(), [(s.stage, s.last_error) for s in failed]


BOXES = ([20, 20, 120, 170], [30, 40, 110, 160], [100, 200, 140, 210])  # 100x150, 80x120, 40x10


@pytest.mark.django_db(transaction=True)
def test_all_pages_get_the_largest_content_plus_margins() -> None:
    client, job, pages = _job_with_pages("layout-common-size", 3)
    for page, box in zip(pages, BOXES, strict=True):
        _prepare(client, job, page, box)
    for page in pages:
        _post(client, job, page, "margins", _margins())

    expected = (100 + 2 * MARGIN_PX, 150 + 2 * MARGIN_PX)
    assert [_size(page) for page in pages] == [expected] * 3
    largest = _state(pages[0], "margins").parameters
    # The largest page's margin is exactly the set margin.
    assert largest["transform"]["offset"] == [MARGIN_PX, MARGIN_PX]
    assert largest["soft_margins_px"] == {"left": 0, "top": 0, "right": 0, "bottom": 0}
    # The title block is centred on the common page.
    title = _state(pages[2], "margins").parameters
    soft = title["soft_margins_px"]
    assert abs(soft["left"] - soft["right"]) <= 1 and abs(soft["top"] - soft["bottom"]) <= 1
    assert title["transform"]["offset"] == [MARGIN_PX + soft["left"], MARGIN_PX + soft["top"]]
    assert soft["left"] + soft["right"] == 60 and soft["top"] + soft["bottom"] == 140
    # Every run records the aggregate it used.
    fingerprints = {_state(page, "margins").parameters["aggregate_fingerprint"] for page in pages}
    assert fingerprints == {scantailor.margins_aggregate(job).fingerprint}
    assert scantailor.margins_aggregate(job).width_mm == pytest.approx(expected[0] * 25.4 / 300)
    _assert_nothing_failed(job)


@pytest.mark.django_db(transaction=True)
def test_page_not_aligned_with_others_keeps_its_own_size() -> None:
    client, job, pages = _job_with_pages("layout-not-aligned", 3)
    for page, box in zip(pages, BOXES, strict=True):
        _prepare(client, job, page, box)
    _post(client, job, pages[0], "margins", _margins(align_with_others=False))
    for page in pages[1:]:
        _post(client, job, page, "margins", _margins())

    # The 100x150 page keeps its own size; the others follow the 80x120 page.
    assert _size(pages[0]) == (100 + 2 * MARGIN_PX, 150 + 2 * MARGIN_PX)
    assert _size(pages[1]) == _size(pages[2]) == (80 + 2 * MARGIN_PX, 120 + 2 * MARGIN_PX)
    assert _state(pages[0], "margins").parameters["aggregate_fingerprint"] == ""
    _assert_nothing_failed(job)


@pytest.mark.django_db(transaction=True)
def test_content_change_on_one_page_requeues_the_others() -> None:
    client, job, pages = _job_with_pages("layout-requeue", 3)
    for page, box in zip(pages, BOXES, strict=True):
        _prepare(client, job, page, box)
    for page in pages:
        _post(client, job, page, "margins", _margins())
        _post(client, job, page, "output", {"output_mode": "otsu"})
    before = {page.id: _state(page, "margins") for page in pages}
    requeued_before = _requeued()

    # Page 3 becomes the largest page: 160x220.
    _post(
        client,
        job,
        pages[2],
        "select_content",
        {"content_mode": "manual", "content_box": [40, 40, 200, 260]},
    )

    bigger = (160 + 2 * MARGIN_PX, 220 + 2 * MARGIN_PX)
    for page in pages[:2]:
        state = _state(page, "margins")
        assert state.status == SUCCESS
        assert state.revision == before[page.id].revision  # a re-run, not an edit
        assert state.output_sha256 != before[page.id].output_sha256
        assert _size(page) == bigger
        assert (
            state.parameters["aggregate_fingerprint"]
            == scantailor.margins_aggregate(job).fingerprint
        )
        # The Output built on the old page size is stale.
        assert _state(page, "output").status == ScanTailorStageState.StageStatus.INVALIDATED
    assert _requeued() - requeued_before == 2
    assert _state(pages[2], "margins").status == ScanTailorStageState.StageStatus.INVALIDATED
    _assert_nothing_failed(job)

    _post(client, job, pages[2], "dewarping", {"dewarp_mode": "off"})
    _post(client, job, pages[2], "margins", _margins())
    assert [_size(page) for page in pages] == [bigger] * 3
    # Nothing else changed, so nothing more was re-queued.
    assert _requeued() - requeued_before == 2
    _assert_nothing_failed(job)


@pytest.mark.django_db(transaction=True)
def test_requeued_page_edited_meanwhile_is_left_to_the_edit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job, pages = _job_with_pages("layout-requeue-race", 2)
    for page, box in zip(pages, BOXES[:2], strict=True):
        _prepare(client, job, page, box)
    for page in pages:
        _post(client, job, page, "margins", _margins())

    held: list[str] = []

    def hold(job_ids: list[str]) -> int:
        held.extend(job_ids)
        return 1

    monkeypatch.setattr(scantailor, "dispatch_stage_batch", hold)
    margins = _state(pages[1], "margins")
    margins.parameters = {**margins.parameters, "aggregate_fingerprint": "stale"}
    margins.save(update_fields=["parameters"])
    assert scantailor.requeue_stale_margins(job) == 1
    # The member edits that page's Dewarping before the re-run starts.
    ScanTailorStageState.objects.filter(scan_page=pages[1], stage="dewarping").update(
        status=ScanTailorStageState.StageStatus.QUEUED
    )
    for job_id in held:
        scantailor.process_stage_job(job_id)

    assert _state(pages[1], "margins").status == ScanTailorStageState.StageStatus.INVALIDATED
    processing = ProcessingJob.objects.get(job_id=held[0])
    assert processing.state == ProcessingJob.JobState.SUCCESS
    assert processing.details["superseded"] is True
    _assert_nothing_failed(job)


@pytest.mark.django_db(transaction=True)
def test_pages_with_different_dpi_get_the_same_physical_size() -> None:
    client, job, pages = _job_with_pages("layout-dpi", 2, dpis=[300, 600])
    # 100x150 px at 300 dpi and a physically smaller 100x100 px at 600 dpi.
    _prepare(client, job, pages[0], [20, 20, 120, 170])
    _prepare(client, job, pages[1], [100, 100, 200, 200])
    for page in pages:
        _post(client, job, page, "margins", _margins())

    (w300, h300), (w600, h600) = _size(pages[0]), _size(pages[1])
    assert (w300, h300) == (100 + 2 * MARGIN_PX, 150 + 2 * MARGIN_PX)
    assert abs(w300 / 300 - w600 / 600) <= 1 / 600
    assert abs(h300 / 300 - h600 / 600) <= 1 / 600
    small = _state(pages[1], "margins").parameters
    assert small["hard_margins_px"]["left"] == round(MARGIN_MM * 600 / 25.4)
    assert small["page_size_mm"] == _state(pages[0], "margins").parameters["page_size_mm"]
    _assert_nothing_failed(job)


@pytest.mark.django_db(transaction=True)
def test_empty_content_box_uses_a_point_in_the_middle_of_the_page() -> None:
    client, job, pages = _job_with_pages("layout-empty", 2, blank={1})
    _prepare(client, job, pages[0], [20, 20, 120, 170])
    _prepare(client, job, pages[1], None)
    assert _state(pages[1], "select_content").parameters["content_found"] is False
    for page in pages:
        _post(client, job, page, "margins", _margins())

    expected = (100 + 2 * MARGIN_PX, 150 + 2 * MARGIN_PX)
    assert _size(pages[0]) == _size(pages[1]) == expected
    blank = _state(pages[1], "margins").parameters
    assert blank["content_found"] is False
    assert blank["content_size_mm"] == {"width": 0.0, "height": 0.0}
    # adaptContentRect: the common page is cut from around the centre of the 300x400 page.
    assert blank["transform"]["offset"] == [
        round(expected[0] / 2 - 300 / 2),
        round(expected[1] / 2 - 400 / 2),
    ]
    # The blank page alone does not inflate the aggregate beyond its margins.
    entries, _ = scantailor._margins_page_entries(job.pk)
    blank_entry = next(entry for entry in entries if entry.page_id == str(pages[1].id))
    assert blank_entry.hard_size_mm == pytest.approx((2 * MARGIN_PX * 25.4 / 300,) * 2)
    _assert_nothing_failed(job)


@pytest.mark.django_db(transaction=True)
def test_margins_batch_requeues_the_other_pages_once() -> None:
    client, job, pages = _job_with_pages("layout-batch", 3)
    for page, box in zip(pages, BOXES, strict=True):
        _prepare(client, job, page, box)
    for page in pages:
        _post(client, job, page, "margins", _margins())
    requeued_before = _requeued()

    response = client.post(
        f"/scan/jobs/{job.id}/stages/margins/batch/",
        data=json.dumps(
            {
                "parameters": _margins(left_mm=10, right_mm=10),
                "page_ids": [str(pages[0].id)],
                "preserve_exceptions": False,
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 202
    wider = 100 + 2 * round(10 * 300 / 25.4)
    assert [_size(page)[0] for page in pages] == [wider] * 3
    assert _requeued() - requeued_before == 2
    _assert_nothing_failed(job)
