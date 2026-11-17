"""``scan.jsonl`` event lines (#129, contract §4.4/§6.7): API, projection, rendering."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from ebook_server.core import intake, scan_events, scan_pipeline, views
from ebook_server.core.models import (
    Book,
    ProcessingJob,
    ScanEvent,
    ScanJob,
    ScannerDevice,
    ScanPage,
    get_or_create_profile,
)
from ebook_server.core.scanner_devices import create_device
from ebook_server.storage import LocalFilesystemStorage, ReadOnlyFilesystemStorage

JOB_ID = "20261117T143201Z"
STARTED = datetime(2026, 11, 17, 14, 32, 1, tzinfo=UTC)

JOB_LINE = json.dumps(
    {
        "type": "job",
        "job_id": JOB_ID,
        "scan_started_at": "2026-11-17T14:32:01Z",
        "page_width_mm": 148.0,
        "page_number_crop": {
            "left": {"x": 1, "y": 2, "w": 3, "h": 4},
            "right": {"x": 5, "y": 6, "w": 7, "h": 8},
        },
    },
    separators=(",", ":"),
)


def attempt(sequence_number: int, result: str = "success") -> str:
    return (
        f'{{"type":"attempt","sequence_number":{sequence_number},'
        f'"ambient_mbar":1016.1,"pickup_result":"{result}"}}'
    )


def page_line(sequence_number: int, number: str | None, status: str = "ok") -> str:
    return json.dumps(
        {
            "type": "page",
            "sequence_number": sequence_number,
            "file": f"{sequence_number:04d}.jpg",
            "recognized_page_number": number,
            "status": status,
        },
        separators=(",", ":"),
    )


@pytest.fixture
def app_data(tmp_path: Path, settings: Any) -> Path:
    settings.APP_DATA_DIR = tmp_path
    return tmp_path


@pytest.fixture
def device(db: None) -> tuple[ScannerDevice, str]:
    return create_device("libreflip-01")


@pytest.fixture
def job(device: tuple[ScannerDevice, str], app_data: Path) -> ScanJob:
    return ScanJob.objects.create(
        id=JOB_ID,
        state=ScanJob.State.CAPTURING,
        device=device[0],
        scanner_id=device[0].scanner_id,
        scan_started_at=STARTED,
    )


@pytest.fixture
def post(
    device: tuple[ScannerDevice, str],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> Callable[..., Any]:
    """POST an event batch with the device key; on-commit renders run immediately."""

    def _post(
        first_line_no: Any,
        lines: Any,
        *,
        job_id: str = JOB_ID,
        key: str | None = None,
        body: Any = None,
    ) -> Any:
        payload = body if body is not None else {"first_line_no": first_line_no, "lines": lines}
        with django_capture_on_commit_callbacks(execute=True):
            return Client().post(
                f"/api/scanner/v1/jobs/{job_id}/events",
                data=payload if isinstance(payload, str | bytes) else json.dumps(payload),
                content_type="application/json",
                headers={"Authorization": f"Bearer {key or device[1]}"},
            )

    return _post


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def _stored(job_id: str = JOB_ID) -> list[tuple[str, int | None, str]]:
    return list(
        ScanEvent.objects.filter(scan_job_id=job_id)
        .order_by("id")
        .values_list("origin", "line_no", "raw")
    )


def _job_file(app_data: Path, job_id: str = JOB_ID) -> Path:
    return app_data / "scan-artifacts" / "scan-jobs" / job_id / "scan.jsonl"


# --- storing and idempotency --------------------------------------------------------------


def test_batch_is_stored_and_rendered(job: ScanJob, post: Any, app_data: Path) -> None:
    lines = [JOB_LINE, attempt(0), page_line(0, None)]
    response = post(1, lines)
    assert response.status_code == 200
    assert response.json() == {"sans_last_line_no": 3}
    assert [(origin, no) for origin, no, _raw in _stored()] == [
        ("sans", 1),
        ("sans", 2),
        ("sans", 3),
    ]
    assert [raw for _o, _n, raw in _stored()] == lines
    assert ScanEvent.objects.get(line_no=2).type == "attempt"
    assert _job_file(app_data).read_bytes() == "".join(f"{line}\n" for line in lines).encode()
    assert scan_events.sans_high_water_mark(job) == 3
    assert scan_events.sans_high_water_mark(job.pk) == 3


def test_resending_overlapping_batches_is_a_no_op(job: ScanJob, post: Any) -> None:
    lines = [JOB_LINE, attempt(0), attempt(0, "failure"), page_line(0, "1")]
    assert post(1, lines[:3]).json() == {"sans_last_line_no": 3}
    before = _stored()

    # The same batch again, and a sub-range of it, change nothing.
    assert post(1, lines[:3]).json() == {"sans_last_line_no": 3}
    assert post(2, lines[1:2]).json() == {"sans_last_line_no": 3}
    assert _stored() == before

    # An overlapping batch stores only the new tail.
    response = post(2, lines[1:])
    assert response.status_code == 200
    assert response.json() == {"sans_last_line_no": 4}
    assert [raw for _o, _n, raw in _stored()] == lines
    assert post(1, lines).json() == {"sans_last_line_no": 4}
    assert ScanEvent.objects.count() == 4


def test_unicode_lines_are_kept_byte_for_byte(job: ScanJob, post: Any, app_data: Path) -> None:
    line = '{"type":"note","text":"Größe ✓ – 頁","spacing" :  1}'
    assert post(1, [line]).status_code == 200
    assert post(1, [line]).status_code == 200
    assert _job_file(app_data).read_bytes() == (line + "\n").encode("utf-8")


def test_empty_batch_reports_the_high_water_mark(job: ScanJob, post: Any) -> None:
    assert post(1, []).json() == {"sans_last_line_no": 0}
    post(1, [JOB_LINE])
    assert post(2, []).json() == {"sans_last_line_no": 1}


# --- gaps, conflicts, invalid lines -------------------------------------------------------


def test_gap_is_refused_with_the_expected_first_line(job: ScanJob, post: Any) -> None:
    response = post(2, [attempt(0)])
    assert response.status_code == 409
    assert _code(response) == "event_gap"
    assert response.json()["error"]["details"]["expected_first_line_no"] == 1
    post(1, [JOB_LINE, attempt(0)])
    response = post(4, [attempt(1)])
    assert response.status_code == 409
    assert response.json()["error"]["details"]["expected_first_line_no"] == 3
    assert ScanEvent.objects.count() == 2


def test_conflicting_overlap_is_refused_and_stores_nothing(job: ScanJob, post: Any) -> None:
    post(1, [JOB_LINE, attempt(0)])
    # Same content, different bytes (whitespace) is still a conflict: byte-identical only.
    altered = attempt(0).replace(",", ", ", 1)
    response = post(2, [altered, attempt(1)])
    assert response.status_code == 409
    assert _code(response) == "event_conflict"
    assert response.json()["error"]["details"]["line_no"] == 2
    assert ScanEvent.objects.count() == 2


@pytest.mark.parametrize(
    "line",
    [
        "not json",
        "[1, 2]",
        '"just a string"',
        "{}",
        '{"type": 3}',
        '{"type": ""}',
        '{"type": null}',
        '{"type":"page",\n"sequence_number":1}',
        '{"type":"page"}\r',
        '{"type":"blob","data":"' + "x" * (64 * 1024) + '"}',
    ],
    ids=[
        "not-json",
        "array",
        "string",
        "no-type",
        "int-type",
        "empty-type",
        "null-type",
        "newline",
        "carriage-return",
        "too-long",
    ],
)
def test_invalid_event_is_refused(job: ScanJob, post: Any, line: str) -> None:
    response = post(1, [JOB_LINE, line])
    assert response.status_code == 422
    assert _code(response) == "invalid_event"
    assert response.json()["error"]["details"]["line_no"] == 2
    assert ScanEvent.objects.count() == 0


def test_line_of_exactly_64_kib_is_accepted(job: ScanJob, post: Any) -> None:
    prefix = '{"type":"blob","data":"'
    line = prefix + "x" * (64 * 1024 - len(prefix) - 2) + '"}'
    assert len(line.encode()) == 64 * 1024
    assert post(1, [line]).status_code == 200


@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        {"lines": []},
        {"first_line_no": 0, "lines": []},
        {"first_line_no": True, "lines": []},
        {"first_line_no": "1", "lines": []},
        {"first_line_no": 1, "lines": "x"},
        {"first_line_no": 1, "lines": [{"type": "job"}]},
    ],
)
def test_malformed_body_is_a_bad_request(job: ScanJob, post: Any, body: Any) -> None:
    response = post(None, None, body=body)
    assert response.status_code == 400
    assert _code(response) in {"invalid_json", "invalid_request"}


def test_batch_limits(job: ScanJob, post: Any) -> None:
    too_many = [attempt(index) for index in range(1001)]
    response = post(1, too_many)
    assert response.status_code == 413
    assert _code(response) == "payload_too_large"
    assert post(1, too_many[:1000]).json() == {"sans_last_line_no": 1000}

    big = '{"type":"blob","data":"' + "y" * 60_000 + '"}'
    response = post(1001, [big] * 18)  # 18 x ~60 KB > 1 MiB of line bytes
    assert response.status_code == 413
    assert ScanEvent.objects.count() == 1000


# --- job state and ownership ----------------------------------------------------------------


@pytest.mark.parametrize(
    "state",
    [
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
        ScanJob.State.FINALIZING,
        ScanJob.State.COMPLETE,
    ],
)
def test_events_are_accepted_in_every_state_but_cancelled(
    job: ScanJob, post: Any, state: str
) -> None:
    ScanJob.objects.filter(pk=job.pk).update(state=state)
    assert post(1, [JOB_LINE]).status_code == 200


def test_cancelled_job_refuses_events(job: ScanJob, post: Any) -> None:
    ScanJob.objects.filter(pk=job.pk).update(state=ScanJob.State.CANCELLED)
    response = post(1, [JOB_LINE])
    assert response.status_code == 409
    assert _code(response) == "job_state"
    assert ScanEvent.objects.count() == 0


def test_other_devices_and_unknown_jobs_are_not_found(job: ScanJob, post: Any) -> None:
    _other, other_key = create_device("libreflip-02")
    assert post(1, [JOB_LINE], key=other_key).status_code == 404
    assert post(1, [JOB_LINE], job_id="20261117T143202Z").status_code == 404
    web_job = ScanJob.objects.create(id="20261117T143203Z", provisional_title="web")
    assert post(1, [JOB_LINE], job_id=web_job.pk).status_code == 404
    response = post(1, [JOB_LINE], job_id="not-a-job")
    assert response.status_code == 400
    assert _code(response) == "invalid_job_id"
    assert ScanEvent.objects.count() == 0


def test_events_need_a_device_key(job: ScanJob) -> None:
    response = Client().post(
        f"/api/scanner/v1/jobs/{JOB_ID}/events",
        data=json.dumps({"first_line_no": 1, "lines": [JOB_LINE]}),
        content_type="application/json",
    )
    assert response.status_code == 401


def test_only_post_is_allowed(job: ScanJob, device: tuple[ScannerDevice, str]) -> None:
    response = Client().get(
        f"/api/scanner/v1/jobs/{JOB_ID}/events",
        headers={"Authorization": f"Bearer {device[1]}"},
    )
    assert response.status_code == 405


def test_job_status_reports_the_high_water_mark(
    job: ScanJob, post: Any, device: tuple[ScannerDevice, str]
) -> None:
    post(1, [JOB_LINE, attempt(0)])
    scan_events.append_serif_event(job, "rescan_flag", {"sequence_number": 0}, todo_id=1)
    response = Client().put(
        f"/api/scanner/v1/jobs/{JOB_ID}",
        data=json.dumps({"scan_started_at": "2026-11-17T14:32:01Z"}),
        content_type="application/json",
        headers={"Authorization": f"Bearer {device[1]}"},
    )
    assert response.status_code == 200
    assert response.json()["events"] == {"sans_last_line_no": 2}


# --- projection --------------------------------------------------------------------------------


def _page(job: ScanJob, sequence_number: int) -> ScanPage:
    return ScanPage.objects.create(
        scan_job=job, logical_sequence_number=sequence_number, side=ScanPage.Side.SINGLE
    )


def test_job_event_projects_calibration(job: ScanJob, post: Any) -> None:
    post(1, [JOB_LINE])
    job.refresh_from_db()
    assert job.page_width_mm == 148.0
    assert job.page_number_crop == {
        "left": {"x": 1, "y": 2, "w": 3, "h": 4},
        "right": {"x": 5, "y": 6, "w": 7, "h": 8},
    }
    # Malformed fields of a known type are stored but not projected.
    post(2, ['{"type":"job","page_width_mm":"wide","page_number_crop":[1]}'])
    job.refresh_from_db()
    assert job.page_width_mm == 148.0
    assert job.page_number_crop is not None
    assert ScanEvent.objects.count() == 2


def test_page_event_projects_the_latest_values(job: ScanJob, post: Any) -> None:
    page_0, page_1 = _page(job, 0), _page(job, 1)
    post(1, [page_line(0, "1"), page_line(1, None, "blurred"), page_line(0, "2", "ok")])
    page_0.refresh_from_db()
    page_1.refresh_from_db()
    assert (page_0.recognized_page_number, page_0.status) == ("2", "ok")
    assert (page_1.recognized_page_number, page_1.status) == (None, "blurred")


def test_page_event_before_the_page_is_applied_when_the_page_appears(
    job: ScanJob, post: Any
) -> None:
    post(1, [page_line(5, "7"), page_line(5, "8", "manual")])
    page = _page(job, 5)
    assert page.recognized_page_number is None
    assert scan_events.project_page_from_events(page) is True
    fresh = ScanPage.objects.get(pk=page.pk)
    assert (fresh.recognized_page_number, fresh.status) == ("8", "manual")
    assert scan_events.project_page_from_events(_page(job, 6)) is False


def test_late_page_replays_the_latest_value_of_each_field(job: ScanJob, post: Any) -> None:
    """PDXDJN #140 finding 3: a later status-only event keeps the earlier number."""
    post(1, [page_line(5, "7")])
    post(2, ['{"type":"page","sequence_number":5,"status":"manual"}'])
    post(3, ['{"type":"page","sequence_number":6,"recognized_page_number":"9"}'])
    page = _page(job, 5)
    assert scan_events.project_page_from_events(page) is True
    fresh = ScanPage.objects.get(pk=page.pk)
    assert (fresh.recognized_page_number, fresh.status) == ("7", "manual")
    # Same result as the live projection for a page that existed all along.
    live = _page(job, 7)
    post(4, [page_line(7, "10"), '{"type":"page","sequence_number":7,"status":"blurred"}'])
    live.refresh_from_db()
    assert (live.recognized_page_number, live.status) == ("10", "blurred")
    # An explicit null is a value, and the latest one wins.
    post(6, ['{"type":"page","sequence_number":5,"recognized_page_number":null}'])
    ScanPage.objects.filter(pk=page.pk).update(recognized_page_number="stale")
    assert scan_events.project_page_from_events(ScanPage.objects.get(pk=page.pk)) is True
    late = ScanPage.objects.get(pk=page.pk)
    assert (late.recognized_page_number, late.status) == (None, "manual")


@pytest.mark.parametrize(
    "line",
    [
        '{"type":"job","page_width_mm":' + "9" * 400 + "}",
        '{"type":"job","page_width_mm":-' + "9" * 400 + "}",
        '{"type":"job","page_width_mm":1e400}',
        '{"type":"job","page_width_mm":NaN}',
        '{"type":"job","page_number_crop":{"x":NaN}}',
        '{"type":"completeness_check","flags":[Infinity]}',
        '{"type":"page","sequence_number":' + "9" * 400 + ',"status":"ok"}',
        '{"type":"page","sequence_number":2147483648,"status":"ok"}',
    ],
)
def test_out_of_range_numbers_are_malformed_fields_not_errors(
    job: ScanJob, post: Any, line: str
) -> None:
    """PDXDJN #140 finding 4: the event is stored, the field is not projected."""
    post(1, [JOB_LINE])
    response = post(2, [line])
    assert response.status_code == 200, response.content
    assert response.json() == {"sans_last_line_no": 2}
    assert ScanEvent.objects.filter(scan_job=job).count() == 2
    job.refresh_from_db()
    assert job.page_width_mm == 148.0
    assert job.page_number_crop == json.loads(JOB_LINE)["page_number_crop"]
    assert job.completeness_flags is None


def test_deeply_nested_line_is_an_invalid_event(job: ScanJob, post: Any) -> None:
    response = post(1, ['{"type":"x","a":' + "[" * 30000 + "]" * 30000 + "}"])
    assert response.status_code == 422
    assert _code(response) == "invalid_event"
    assert ScanEvent.objects.count() == 0


def test_completeness_check_projects_flags(job: ScanJob, post: Any) -> None:
    flags = [
        {
            "sequence_number_range": [46, 48],
            "detail": "expected +1, got +3",
            "missing_page_numbers": ["46", "47"],
        }
    ]
    post(1, [json.dumps({"type": "completeness_check", "flags": flags})])
    job.refresh_from_db()
    assert job.completeness_flags == flags
    assert job.completeness_checked_at is not None

    post(2, ['{"type":"completeness_check","flags":[]}'])
    job.refresh_from_db()
    assert job.completeness_flags == []


def test_unknown_types_are_stored_only(job: ScanJob, post: Any) -> None:
    post(1, ['{"type":"motor_telemetry","rpm":12}', attempt(3)])
    job.refresh_from_db()
    assert job.page_width_mm is None
    assert job.completeness_flags is None
    assert [event.type for event in ScanEvent.objects.order_by("id")] == [
        "motor_telemetry",
        "attempt",
    ]


def test_workspace_shows_completeness_flags_and_printed_numbers(job: ScanJob, post: Any) -> None:
    _page(job, 0)
    post(
        1,
        [
            page_line(0, "xiv"),
            json.dumps(
                {
                    "type": "completeness_check",
                    "flags": [{"sequence_number_range": [46, 48], "detail": "expected +1"}],
                }
            ),
        ],
    )
    client = Client()
    client.force_login(get_or_create_profile("member").user)
    response = client.get(f"/scan/jobs/{JOB_ID}/workspace/", HTTP_ACCEPT="text/html")
    assert response.status_code == 200
    html = response.content.decode()
    assert "Scanner completeness check" in html
    assert "expected +1" in html
    assert "printed xiv" in html


# --- serif events and the rendered file ---------------------------------------------------------


def test_serif_events_are_interleaved_in_arrival_order(
    job: ScanJob,
    post: Any,
    app_data: Path,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    post(1, [JOB_LINE, attempt(0)])
    at = datetime(2026, 11, 18, 9, 0, 0, tzinfo=UTC)
    with django_capture_on_commit_callbacks(execute=True):
        flag = scan_events.append_serif_event(
            job, "rescan_flag", {"sequence_number": 0}, todo_id=41, at=at
        )
    post(3, [page_line(0, "1")])
    with django_capture_on_commit_callbacks(execute=True):
        scan_events.append_serif_event(
            job.pk, "rescan_replace", {"sequence_number": 0}, todo_id=41, at=at
        )
    post(4, [attempt(1)])

    assert json.loads(flag.raw) == {
        "type": "rescan_flag",
        "sequence_number": 0,
        "origin": "serif",
        "at": "2026-11-18T09:00:00Z",
        "todo_id": 41,
    }
    assert flag.line_no is None
    assert [(origin, no) for origin, no, _raw in _stored()] == [
        ("sans", 1),
        ("sans", 2),
        ("serif", None),
        ("sans", 3),
        ("serif", None),
        ("sans", 4),
    ]
    # serif events never move the scanner's numbering.
    assert scan_events.sans_high_water_mark(job) == 4
    expected = "".join(f"{raw}\n" for _o, _n, raw in _stored()).encode()
    assert scan_events.render_scan_jsonl(job) == expected
    assert _job_file(app_data).read_bytes() == expected
    # The scanner's own lines keep their relative order, byte for byte.
    sans_lines = [
        line
        for line in _job_file(app_data).read_text(encoding="utf-8").splitlines()
        if json.loads(line).get("origin") != "serif"
    ]
    assert sans_lines == [JOB_LINE, attempt(0), page_line(0, "1"), attempt(1)]


def test_render_skips_when_nothing_is_new(
    job: ScanJob, post: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    post(1, [JOB_LINE])
    writes: list[str] = []
    original = LocalFilesystemStorage.stage_bytes

    def counting(self: LocalFilesystemStorage, destination: str, data: bytes) -> str:
        writes.append(destination)
        return original(self, destination, data)

    monkeypatch.setattr(LocalFilesystemStorage, "stage_bytes", counting)
    scan_events.write_scan_jsonl(JOB_ID)
    assert writes == []


def test_render_catches_up_with_events_stored_while_writing(
    job: ScanJob, post: Any, app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    post(1, [JOB_LINE])
    original = LocalFilesystemStorage.commit_staged
    injected: list[bool] = []

    def racing(self: LocalFilesystemStorage, staged: str, destination: str) -> str:
        result = original(self, staged, destination)
        if not injected:
            injected.append(True)
            # A batch commits after this render took its snapshot.
            ScanEvent.objects.create(
                scan_job=job, origin="sans", line_no=2, raw=attempt(0), type="attempt"
            )
        return result

    monkeypatch.setattr(LocalFilesystemStorage, "commit_staged", racing)
    _job_file(app_data).unlink()
    scan_events.write_scan_jsonl(JOB_ID)
    assert _job_file(app_data).read_text(encoding="utf-8") == f"{JOB_LINE}\n{attempt(0)}\n"


def test_render_rewrites_a_file_a_stale_render_replaced(
    job: ScanJob, post: Any, app_data: Path
) -> None:
    post(1, [JOB_LINE, attempt(0)])
    # A slower, older render renamed its snapshot over the newer file.
    _job_file(app_data).write_text(f"{JOB_LINE}\n", encoding="utf-8")
    post(3, [attempt(1)])
    assert _job_file(app_data).read_text(encoding="utf-8") == (
        f"{JOB_LINE}\n{attempt(0)}\n{attempt(1)}\n"
    )


def test_an_older_render_never_replaces_a_newer_snapshot(
    job: ScanJob, post: Any, app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PDXDJN #140 finding 2: the older worker is fenced out instead of renaming its
    stale snapshot over the newer file (and then possibly dying before repairing it)."""
    post(1, [JOB_LINE])
    _job_file(app_data).unlink()  # force the next render to write
    original = LocalFilesystemStorage.stage_bytes
    interleaved: list[bool] = []

    def older_worker_stalls(self: LocalFilesystemStorage, destination: str, data: bytes) -> str:
        staged = original(self, destination, data)
        if not interleaved:
            interleaved.append(True)
            # While the older worker sits between staging and renaming, a new line is
            # stored and a newer worker renders and publishes it completely.
            scan_events.append_sans_lines(JOB_ID, 2, [attempt(0)])
            scan_events.write_scan_jsonl(JOB_ID)
        return staged

    monkeypatch.setattr(LocalFilesystemStorage, "stage_bytes", older_worker_stalls)
    commits: list[str] = []
    original_commit = LocalFilesystemStorage.commit_staged

    def counting(self: LocalFilesystemStorage, staged: str, destination: str) -> str:
        commits.append(destination)
        return original_commit(self, staged, destination)

    monkeypatch.setattr(LocalFilesystemStorage, "commit_staged", counting)
    scan_events.write_scan_jsonl(JOB_ID)  # the older worker
    expected = f"{JOB_LINE}\n{attempt(0)}\n"
    assert _job_file(app_data).read_text(encoding="utf-8") == expected
    assert len(commits) == 1  # only the newer worker renamed its snapshot into place
    job.refresh_from_db()
    assert job.scan_jsonl_job_rendered_through == ScanEvent.objects.latest("id").pk
    assert not list(_job_file(app_data).parent.glob("*.partial"))


def test_a_failed_write_is_not_recorded_and_a_retry_repairs_it(
    job: ScanJob, post: Any, app_data: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    post(1, [JOB_LINE])

    def broken(self: LocalFilesystemStorage, staged: str, destination: str) -> str:
        raise OSError("share went away")

    with monkeypatch.context() as patched:
        patched.setattr(LocalFilesystemStorage, "commit_staged", broken)
        with pytest.raises(OSError, match="share went away"):
            scan_events.append_sans_lines(JOB_ID, 2, [attempt(0)])
            scan_events.write_scan_jsonl(JOB_ID)
    job.refresh_from_db()
    first_id = ScanEvent.objects.order_by("id").first()
    assert first_id is not None
    assert job.scan_jsonl_job_rendered_through == first_id.pk  # line 2 never recorded
    assert _job_file(app_data).read_text(encoding="utf-8") == f"{JOB_LINE}\n"
    assert not list(_job_file(app_data).parent.glob("*.partial"))
    scan_events.write_scan_jsonl(JOB_ID)  # the retry
    assert _job_file(app_data).read_text(encoding="utf-8") == f"{JOB_LINE}\n{attempt(0)}\n"


def test_a_retry_verifies_the_file_instead_of_trusting_the_mark(
    job: ScanJob, post: Any, app_data: Path
) -> None:
    """The mark says everything is rendered, but the file lost a line: rewrite it."""
    post(1, [JOB_LINE, attempt(0)])
    job.refresh_from_db()
    assert job.scan_jsonl_job_rendered_through == ScanEvent.objects.latest("id").pk
    _job_file(app_data).write_text(f"{JOB_LINE}\n", encoding="utf-8")
    scan_events.write_scan_jsonl(JOB_ID)
    assert _job_file(app_data).read_text(encoding="utf-8") == f"{JOB_LINE}\n{attempt(0)}\n"
    # Same size, different bytes: still repaired (hash, not just size).
    _job_file(app_data).write_bytes(b"x" * len(scan_events.render_scan_jsonl(job)))
    scan_events.write_scan_jsonl(JOB_ID)
    assert _job_file(app_data).read_bytes() == scan_events.render_scan_jsonl(job)


def test_a_foreign_file_in_the_job_folder_is_not_overwritten(
    job: ScanJob, post: Any, app_data: Path
) -> None:
    foreign = _job_file(app_data)
    foreign.parent.mkdir(parents=True)
    foreign.write_text("not serif's\n")
    post(1, [JOB_LINE])
    post(2, [attempt(0)])
    assert foreign.read_text() == "not serif's\n"
    job.refresh_from_db()
    assert job.scan_jsonl_job_identifier == f"scan-jobs/{JOB_ID}/scan-{JOB_ID}.jsonl"
    assert (foreign.parent / f"scan-{JOB_ID}.jsonl").read_text(encoding="utf-8") == (
        f"{JOB_LINE}\n{attempt(0)}\n"
    )


def test_no_file_without_events(job: ScanJob, app_data: Path) -> None:
    scan_events.write_scan_jsonl(JOB_ID)
    assert not _job_file(app_data).exists()
    scan_events.write_scan_jsonl("20261117T000000Z")  # unknown job: nothing to do


def test_job_namespace_matches_page_uploads() -> None:
    assert scan_events.JOB_NAMESPACE == views.SCANNING_NAMESPACE


# --- book folder after finish -----------------------------------------------------------


def _finish(job: ScanJob, book: Book, capture: Callable[..., Any]) -> None:
    ScanJob.objects.filter(pk=job.pk).update(book=book, state=ScanJob.State.FINALIZING)
    processing = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.SCAN_FINALIZATION,
        state=ProcessingJob.JobState.QUEUED,
        details={
            "scan_job_id": str(job.pk),
            "create_cleaned_variant": False,
            "prior_scan_state": "capturing",
        },
    )
    with capture(execute=True):
        scan_pipeline.process_scan_finalization_job(str(processing.job_id))


@pytest.fixture
def library_root(app_data: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = app_data / "library-artifacts"
    monkeypatch.setattr(intake, "_default_storage", lambda: LocalFilesystemStorage(str(root)))
    yield root


def test_finish_writes_scan_jsonl_into_the_book_folder(
    job: ScanJob,
    post: Any,
    library_root: Path,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    post(1, [JOB_LINE, attempt(0)])
    book = Book.objects.create(title="Scanned Book")
    _finish(job, book, django_capture_on_commit_callbacks)
    job.refresh_from_db()
    book.refresh_from_db()
    assert job.state == ScanJob.State.COMPLETE
    assert book.archive_folder
    assert job.scan_jsonl_book_identifier == f"{book.archive_folder}/scan.jsonl"
    book_file = library_root / job.scan_jsonl_book_identifier
    assert book_file.read_text(encoding="utf-8") == f"{JOB_LINE}\n{attempt(0)}\n"

    # Later events (e.g. a rescan on a library book) update the book's copy too.
    post(3, [attempt(1)])
    assert book_file.read_text(encoding="utf-8").endswith(f"{attempt(1)}\n")
    assert not list(book_file.parent.glob("*.partial"))


def test_second_scan_of_a_book_gets_its_own_file(
    job: ScanJob,
    post: Any,
    library_root: Path,
    device: tuple[ScannerDevice, str],
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    book = Book.objects.create(title="Twice Scanned")
    post(1, [JOB_LINE])
    _finish(job, book, django_capture_on_commit_callbacks)

    second = ScanJob.objects.create(
        id="20261118T090000Z",
        state=ScanJob.State.CAPTURING,
        device=device[0],
        scan_started_at=STARTED,
    )
    post(1, [attempt(9)], job_id=second.pk)
    _finish(second, book, django_capture_on_commit_callbacks)
    second.refresh_from_db()
    book.refresh_from_db()
    assert second.scan_jsonl_book_identifier == (
        f"{book.archive_folder}/scan-20261118T090000Z.jsonl"
    )
    folder = library_root / book.archive_folder
    assert (folder / "scan.jsonl").read_text(encoding="utf-8") == f"{JOB_LINE}\n"
    assert (folder / "scan-20261118T090000Z.jsonl").read_text(encoding="utf-8") == (
        f"{attempt(9)}\n"
    )


def test_a_foreign_scan_jsonl_in_the_book_folder_is_not_overwritten(
    job: ScanJob,
    post: Any,
    library_root: Path,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    book = Book.objects.create(title="Uploaded Log")
    folder = "Unknown author/Uploaded Log"
    Book.objects.filter(pk=book.pk).update(archive_folder=folder)
    (library_root / folder).mkdir(parents=True)
    (library_root / folder / "scan.jsonl").write_text("uploaded elsewhere\n")
    post(1, [JOB_LINE])
    _finish(job, book, django_capture_on_commit_callbacks)
    assert (library_root / folder / "scan.jsonl").read_text() == "uploaded elsewhere\n"
    assert (library_root / folder / f"scan-{JOB_ID}.jsonl").read_text(encoding="utf-8") == (
        f"{JOB_LINE}\n"
    )


def test_both_names_taken_in_the_book_folder_are_preserved(
    job: ScanJob,
    post: Any,
    library_root: Path,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    """PDXDJN #140 finding 1: with scan.jsonl and scan-<job>.jsonl both present (e.g.
    logs uploaded through #113 or another tool), a third, free name is claimed."""
    book = Book.objects.create(title="Crowded Folder")
    folder = "Unknown author/Crowded Folder"
    Book.objects.filter(pk=book.pk).update(archive_folder=folder)
    (library_root / folder).mkdir(parents=True)
    (library_root / folder / "scan.jsonl").write_text("first foreign\n")
    (library_root / folder / f"scan-{JOB_ID}.jsonl").write_text("second foreign\n")
    post(1, [JOB_LINE])
    _finish(job, book, django_capture_on_commit_callbacks)
    post(2, [attempt(0)])
    assert (library_root / folder / "scan.jsonl").read_text() == "first foreign\n"
    assert (library_root / folder / f"scan-{JOB_ID}.jsonl").read_text() == "second foreign\n"
    job.refresh_from_db()
    claimed = job.scan_jsonl_book_identifier
    assert claimed.startswith(f"{folder}/scan-{JOB_ID}-") and claimed.endswith(".jsonl")
    assert (library_root / claimed).read_text(encoding="utf-8") == f"{JOB_LINE}\n{attempt(0)}\n"


def test_a_name_taken_between_check_and_create_is_not_replaced(
    job: ScanJob,
    post: Any,
    library_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> None:
    """Another writer creates scan.jsonl after serif staged its bytes: the exclusive
    create refuses that name instead of replacing the newcomer."""
    book = Book.objects.create(title="Racing Upload")
    folder = "Unknown author/Racing Upload"
    Book.objects.filter(pk=book.pk).update(archive_folder=folder)
    post(1, [JOB_LINE])
    original = LocalFilesystemStorage.commit_staged_exclusive

    def racing(self: LocalFilesystemStorage, staged: str, destination: str) -> str:
        if destination == f"{folder}/scan.jsonl":
            (library_root / destination).write_text("arrived first\n")
        return original(self, staged, destination)

    monkeypatch.setattr(LocalFilesystemStorage, "commit_staged_exclusive", racing)
    _finish(job, book, django_capture_on_commit_callbacks)
    assert (library_root / folder / "scan.jsonl").read_text() == "arrived first\n"
    job.refresh_from_db()
    assert job.scan_jsonl_book_identifier == f"{folder}/scan-{JOB_ID}.jsonl"


def test_unfinished_job_writes_nothing_into_the_book_folder(
    job: ScanJob, post: Any, library_root: Path
) -> None:
    book = Book.objects.create(title="Not Yet")
    ScanJob.objects.filter(pk=job.pk).update(book=book)
    post(1, [JOB_LINE])
    assert not library_root.exists() or not list(library_root.rglob("scan*.jsonl"))


# --- storage -----------------------------------------------------------------------------------


def test_replace_bytes_is_atomic_and_replaces(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    assert storage.replace_bytes("a/b/scan.jsonl", b"one\n") == "a/b/scan.jsonl"
    storage.replace_bytes("a/b/scan.jsonl", b"one\ntwo\n")
    assert (tmp_path / "a" / "b" / "scan.jsonl").read_bytes() == b"one\ntwo\n"
    assert sorted(path.name for path in (tmp_path / "a" / "b").iterdir()) == ["scan.jsonl"]
    with pytest.raises(ValueError):
        storage.replace_bytes("../escape.jsonl", b"x")


def test_replace_bytes_refused_on_read_only_storage(tmp_path: Path) -> None:
    from ebook_server.storage import ReadOnlyStorageError

    with pytest.raises(ReadOnlyStorageError):
        ReadOnlyFilesystemStorage(str(tmp_path)).replace_bytes("scan.jsonl", b"x")


def test_commit_staged_exclusive_never_replaces(tmp_path: Path) -> None:
    storage = LocalFilesystemStorage(str(tmp_path))
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "scan.jsonl").write_bytes(b"theirs\n")
    staged = storage.stage_bytes("a/scan.jsonl", b"ours\n")
    assert staged.startswith("a/.") and staged.endswith(".partial")
    with pytest.raises(FileExistsError):
        storage.commit_staged_exclusive(staged, "a/scan.jsonl")
    assert (tmp_path / "a" / "scan.jsonl").read_bytes() == b"theirs\n"
    assert storage.commit_staged_exclusive(staged, "a/scan-2.jsonl") == "a/scan-2.jsonl"
    assert (tmp_path / "a" / "scan-2.jsonl").read_bytes() == b"ours\n"
    assert sorted(path.name for path in (tmp_path / "a").iterdir()) == [
        "scan-2.jsonl",
        "scan.jsonl",
    ]
    storage.discard_staged(staged)  # already gone: fine


def test_staging_refused_on_read_only_storage(tmp_path: Path) -> None:
    from ebook_server.storage import ReadOnlyStorageError

    storage = ReadOnlyFilesystemStorage(str(tmp_path))
    with pytest.raises(ReadOnlyStorageError):
        storage.stage_bytes("scan.jsonl", b"x")
    with pytest.raises(ReadOnlyStorageError):
        storage.commit_staged_exclusive(".x.partial", "scan.jsonl")
