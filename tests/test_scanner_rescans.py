"""Rescan over the scanner device API (#131, libreflip contract §6.11, D7, §10 item 7).

Done when §10 item 7 passes: flag a page of a ``captured`` job in the portal; it appears
in the scanner's list; the replacement creates version 2, keeps version 1, completes the
todo and appends ``rescan_flag`` then ``rescan_replace`` to ``scan.jsonl``. Repeated on a
``complete`` job, the job returns to ``captured`` with "re-finish needed".
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from django.test import Client
from PIL import Image

from ebook_server.core import scan_events, scanner_rescans
from ebook_server.core.models import (
    Author,
    Book,
    Identifier,
    ScanEvent,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    ScanTodo,
    get_or_create_profile,
)
from tests.test_scanner_api_uploads import (
    CONTENT_TYPE,
    JOB_ID,
    _auth,
    _jpeg,
    _multipart,
    _put_page,
    _sha,
    device,  # noqa: F401  (fixture)
    key,  # noqa: F401  (fixture)
)

OTHER_JOB_ID = "20261117T150000Z"
JOB_EVENT = {
    "type": "job",
    "page_width_mm": 148.0,
    "page_number_crop": {
        "left": {"x": 10, "y": 20, "w": 30, "h": 40},
        "right": {"x": 50, "y": 60, "w": 70, "h": 80},
    },
}


@pytest.fixture(autouse=True)
def _data_dir(settings: Any, tmp_path: Path) -> Path:
    settings.APP_DATA_DIR = tmp_path
    settings.BOOK_SCANNER_PUBLIC_BASE_URL = "https://serif.c-base.lan/"
    return tmp_path


@pytest.fixture
def on_commit(
    django_capture_on_commit_callbacks: Callable[..., Any],
) -> Callable[[], Any]:
    """Run on-commit callbacks (the scan.jsonl render) right away."""
    return lambda: django_capture_on_commit_callbacks(execute=True)


@pytest.fixture
def member() -> Client:
    client = Client()
    client.force_login(get_or_create_profile("rescan-member").user)
    return client


# --- helpers ------------------------------------------------------------------------------


def _post_events(api_key: str, first: int, events: list[dict[str, Any]]) -> Any:
    return Client().post(
        f"/api/scanner/v1/jobs/{JOB_ID}/events",
        data=json.dumps({"first_line_no": first, "lines": [json.dumps(event) for event in events]}),
        content_type="application/json",
        headers=_auth(api_key),
    )


def _scan_book(api_key: str, pages: int = 4) -> None:
    """Pages 0..pages-1, the job event, then capture-complete: the job is ``captured``."""
    images = [_jpeg(f"page-{n}".encode(), size=(320, 200)) for n in range(pages)]
    for number, image in enumerate(images):
        assert _put_page(api_key, number, image).status_code == 201
    assert _post_events(api_key, 1, [JOB_EVENT]).status_code == 200
    response = Client().post(
        f"/api/scanner/v1/jobs/{JOB_ID}/capture-complete",
        data=json.dumps(
            {
                "finished_at": "2026-11-17T15:40:00Z",
                "sans_last_line_no": 1,
                "pages": [
                    {"sequence_number": n, "sha256": _sha(image)} for n, image in enumerate(images)
                ],
                "covers": [],
                "metadata_opf": None,
            }
        ),
        content_type="application/json",
        headers=_auth(api_key),
    )
    assert response.status_code == 200, response.content
    assert response.json()["state"] == "captured"


def _page(sequence: int, job_id: str = JOB_ID) -> ScanPage:
    return ScanPage.objects.get(scan_job_id=job_id, logical_sequence_number=sequence)


def _flag(client: Client, sequence: int, reason: str = "glare") -> Any:
    return client.post(
        f"/scan/pages/{_page(sequence).pk}/rescan/",
        data=json.dumps({"reason": reason}),
        content_type="application/json",
    )


def _replacement_meta(image: bytes, sequence: int, **overrides: Any) -> dict[str, Any]:
    return {
        "sha256": _sha(image),
        "captured_at": "2026-11-18T09:00:00Z",
        "camera_role": "left" if sequence % 2 == 0 else "right",
        "recognized_page_number": str(sequence + 1),
        "page_number_verified": True,
        **overrides,
    }


def _replace(
    api_key: str,
    todo_id: int | str,
    image: bytes,
    sequence: int,
    *,
    job_id: str = JOB_ID,
    **overrides: Any,
) -> Any:
    return Client().post(
        f"/api/scanner/v1/jobs/{job_id}/rescans/{todo_id}/replacement",
        data=_multipart(_replacement_meta(image, sequence, **overrides), image),
        content_type=CONTENT_TYPE,
        headers=_auth(api_key),
    )


def _get(api_key: str, path: str, **headers: str) -> Any:
    return Client().get(f"/api/scanner/v1/{path}", headers={**_auth(api_key), **headers})


def _code(response: Any) -> str:
    return str(response.json()["error"]["code"])


def _serif_events(job_id: str = JOB_ID) -> list[dict[str, Any]]:
    return [
        json.loads(raw)
        for raw in ScanEvent.objects.filter(scan_job_id=job_id, origin=ScanEvent.Origin.SERIF)
        .order_by("id")
        .values_list("raw", flat=True)
    ]


def _scan_jsonl(data_dir: Path, job_id: str = JOB_ID) -> list[dict[str, Any]]:
    path = data_dir / "scan-artifacts" / "scan-jobs" / job_id / "scan.jsonl"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _jpeg_size(content: bytes) -> tuple[int, int]:
    with Image.open(io.BytesIO(content)) as image:
        assert image.format == "JPEG"
        return image.size


def _body(response: Any) -> bytes:
    return b"".join(response.streaming_content)


# --- §10 item 7 ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_rescan_of_a_captured_job_end_to_end(
    key: str,  # noqa: F811
    member: Client,
    on_commit: Callable[[], Any],
    _data_dir: Path,
) -> None:
    _scan_book(key)
    assert _get(key, "rescans").json() == {"items": []}

    # 1. Flag a page of the captured job in the portal.
    with on_commit():
        flagged = _flag(member, 2)
    assert flagged.status_code == 201, flagged.content
    todo_id = flagged.json()["todo"]["id"]
    assert _flag(member, 2).status_code == 200  # an open todo is reused, no second event

    # 2. It appears in the scanner's list.
    listing = _get(key, "rescans").json()
    assert listing == {
        "items": [
            {
                "job_id": JOB_ID,
                "state": "captured",
                "title": None,
                "authors": None,
                "isbn": None,
                "open_rescans": 1,
                "scan_started_at": "2026-11-17T14:32:01Z",
            }
        ]
    }
    detail = _get(key, f"jobs/{JOB_ID}/rescans").json()
    assert detail["page_width_mm"] == 148.0
    assert detail["page_number_crop"] == JOB_EVENT["page_number_crop"]
    assert detail["pages"] == [
        {"sequence_number": n, "recognized_page_number": str(n + 1)} for n in range(4)
    ]
    assert len(detail["todos"]) == 1
    todo = detail["todos"][0]
    assert todo["todo_id"] == todo_id
    assert todo["sequence_number"] == 2
    assert todo["reason"] == "glare"
    assert todo["created_at"].endswith("Z")
    assert todo["thumbnail_url"] == f"/api/scanner/v1/jobs/{JOB_ID}/pages/2/thumbnail"

    thumbnail = _get(key, todo["thumbnail_url"].removeprefix("/api/scanner/v1/"))
    assert thumbnail.status_code == 200
    assert thumbnail["Content-Type"] == "image/jpeg"
    assert max(_jpeg_size(_body(thumbnail))) <= 800
    old_etag = thumbnail["ETag"]

    # 3. The replacement creates version 2 and keeps version 1.
    replacement = _jpeg(b"page-2-rescan", size=(320, 200))
    with on_commit():
        response = _replace(key, todo_id, replacement, 2, recognized_page_number="3")
    assert response.status_code == 201, response.content
    assert response.json() == {
        "todo_id": todo_id,
        "todo_state": "completed",
        "sequence_number": 2,
        "sha256": _sha(replacement),
        "version": 2,
        "previous_version": 1,
        "state": "captured",
    }
    versions = list(_page(2).versions.order_by("version"))
    assert [(v.version, v.is_active) for v in versions] == [(1, False), (2, True)]
    assert versions[1].checksum_sha256 == _sha(replacement)
    assert versions[1].uploader is None
    assert versions[1].page_number_verified is True
    assert versions[1].scanner_metadata["todo_id"] == todo_id
    assert ScanTodo.objects.get(pk=todo_id).state == ScanTodo.State.COMPLETED

    # 4. scan.jsonl gains rescan_flag, then rescan_replace (origin serif).
    serif = _serif_events()
    assert [(e["type"], e["origin"], e["todo_id"], e["sequence_number"]) for e in serif] == [
        ("rescan_flag", "serif", todo_id, 2),
        ("rescan_replace", "serif", todo_id, 2),
    ]
    assert serif[1]["sha256"] == _sha(replacement)
    assert [line["type"] for line in _scan_jsonl(_data_dir)] == [
        "job",
        "rescan_flag",
        "rescan_replace",
    ]

    # Repeating the same upload changes nothing; the list is empty again.
    again = _replace(key, todo_id, replacement, 2)
    assert again.status_code == 200
    assert again.json()["version"] == 2
    assert _page(2).versions.count() == 2
    assert len(_serif_events()) == 2
    assert _get(key, "rescans").json() == {"items": []}
    assert _get(key, f"jobs/{JOB_ID}").json()["open_rescans"] == 0

    # The thumbnail now shows version 2.
    fresh = _get(key, f"jobs/{JOB_ID}/pages/2/thumbnail")
    assert fresh.status_code == 200
    assert fresh["ETag"] != old_etag


@pytest.mark.django_db
def test_rescan_of_a_complete_job_returns_it_to_captured(
    key: str,  # noqa: F811
    member: Client,
    on_commit: Callable[[], Any],
) -> None:
    _scan_book(key)
    book = Book.objects.create(title="Finished Book")
    ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.COMPLETE, book=book)

    with on_commit():
        flagged = _flag(member, 1, reason="blurred")
    assert flagged.status_code == 201
    todo_id = flagged.json()["todo"]["id"]
    items = _get(key, "rescans").json()["items"]
    assert [(item["job_id"], item["state"], item["title"]) for item in items] == [
        (JOB_ID, "complete", "Finished Book")
    ]

    replacement = _jpeg(b"page-1-rescan", size=(320, 200))
    with on_commit():
        response = _replace(key, todo_id, replacement, 1)
    assert response.status_code == 201, response.content
    assert response.json()["state"] == "captured"

    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.state == ScanJob.State.CAPTURED
    assert job.refinish_needed is True
    assert _get(key, f"jobs/{JOB_ID}").json()["note"] == "re-finish needed"
    assert [e["type"] for e in _serif_events()] == ["rescan_flag", "rescan_replace"]
    assert [v.is_active for v in _page(1).versions.order_by("version")] == [False, True]


# --- replacement rules --------------------------------------------------------------------


def _open_todo(sequence: int, job_id: str = JOB_ID, **fields: Any) -> ScanTodo:
    return ScanTodo.objects.create(
        scan_job_id=job_id,
        scan_page=_page(sequence, job_id),
        todo_type=ScanTodo.TodoType.RESCAN,
        reason=fields.pop("reason", "glare"),
        **fields,
    )


@pytest.mark.django_db
def test_cancelled_todo_is_todo_state(key: str) -> None:  # noqa: F811
    _scan_book(key)
    todo = _open_todo(0, state=ScanTodo.State.CANCELLED)
    response = _replace(key, todo.pk, _jpeg(b"late"), 0)
    assert response.status_code == 409
    assert _code(response) == "todo_state"
    assert response.json()["error"]["details"] == {"todo_id": todo.pk, "state": "cancelled"}
    assert _page(0).versions.count() == 1


@pytest.mark.django_db
def test_completed_todo_with_another_image_is_todo_state(key: str) -> None:  # noqa: F811
    _scan_book(key)
    todo = _open_todo(0)
    assert _replace(key, todo.pk, _jpeg(b"first"), 0).status_code == 201
    response = _replace(key, todo.pk, _jpeg(b"second"), 0)
    assert response.status_code == 409
    assert _code(response) == "todo_state"
    assert _page(0).versions.count() == 2


@pytest.mark.django_db
def test_replacement_validation_errors_store_nothing(
    key: str,  # noqa: F811
    _data_dir: Path,
) -> None:
    _scan_book(key)
    todo = _open_todo(3)
    image = _jpeg(b"replacement")

    parity = _replace(key, todo.pk, image, 3, camera_role="left")
    assert (parity.status_code, _code(parity)) == (422, "camera_role_parity")
    mismatch = _replace(key, todo.pk, image, 3, sha256="0" * 64)
    assert (mismatch.status_code, _code(mismatch)) == (422, "checksum_mismatch")
    bad_flag = _replace(key, todo.pk, image, 3, page_number_verified="yes")
    assert (bad_flag.status_code, _code(bad_flag)) == (400, "invalid_request")
    not_jpeg = b"definitely not an image"
    broken = Client().post(
        f"/api/scanner/v1/jobs/{JOB_ID}/rescans/{todo.pk}/replacement",
        data=_multipart(_replacement_meta(not_jpeg, 3), not_jpeg),
        content_type=CONTENT_TYPE,
        headers=_auth(key),
    )
    assert (broken.status_code, _code(broken)) == (422, "invalid_image")

    assert _page(3).versions.count() == 1
    assert ScanTodo.objects.get(pk=todo.pk).state == ScanTodo.State.OPEN
    assert _serif_events() == []
    uploads = _data_dir / "scan-artifacts" / "uploads"
    assert not uploads.exists() or list(uploads.iterdir()) == []
    page_dir = _data_dir / "scan-artifacts" / "scan-jobs" / JOB_ID / "0003-single"
    assert len(list(page_dir.iterdir())) == 1


@pytest.mark.django_db
def test_a_failure_inside_the_transaction_leaves_nothing(
    key: str,  # noqa: F811
    _data_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The five effects are one transaction: an event failure rolls all of them back."""
    _scan_book(key)
    ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.COMPLETE)
    todo = _open_todo(2)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("event store down")

    monkeypatch.setattr(scan_events, "append_serif_event", broken)
    with pytest.raises(RuntimeError):
        _replace(key, todo.pk, _jpeg(b"never"), 2)
    assert [v.is_active for v in _page(2).versions.all()] == [True]
    assert ScanTodo.objects.get(pk=todo.pk).state == ScanTodo.State.OPEN
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.COMPLETE
    page_dir = _data_dir / "scan-artifacts" / "scan-jobs" / JOB_ID / "0002-single"
    assert len(list(page_dir.iterdir())) == 1


@pytest.mark.django_db
def test_replacement_routes_answer_404_and_400(key: str) -> None:  # noqa: F811
    _scan_book(key)
    todo = _open_todo(0)
    image = _jpeg(b"x")
    assert _code(_replace(key, 999_999, image, 0)) == "not_found"
    assert _code(_replace(key, "abc", image, 0)) == "not_found"
    assert _replace(key, todo.pk, image, 0, job_id="not-a-job").status_code == 400
    assert _replace(key, todo.pk, image, 0, job_id=OTHER_JOB_ID).status_code == 404
    # A todo of a web-created job is not reachable through the scanner API.
    web_job = ScanJob.objects.create(id=str(uuid4()), state=ScanJob.State.CAPTURED)
    web_page = ScanPage.objects.create(scan_job=web_job, logical_sequence_number=0)
    web_todo = ScanTodo.objects.create(scan_job=web_job, scan_page=web_page)
    assert _replace(key, web_todo.pk, image, 0).status_code == 404
    assert _get(key, f"jobs/{web_job.pk}/rescans").status_code == 400
    assert ScanTodo.objects.get(pk=todo.pk).state == ScanTodo.State.OPEN


@pytest.mark.django_db
def test_replacement_on_a_cancelled_job_is_job_state(key: str) -> None:  # noqa: F811
    _scan_book(key)
    todo = _open_todo(0)
    ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.CANCELLED)
    response = _replace(key, todo.pk, _jpeg(b"y"), 0)
    assert (response.status_code, _code(response)) == (409, "job_state")
    assert _get(key, "rescans").json() == {"items": []}


# --- listing ------------------------------------------------------------------------------


@pytest.mark.django_db
def test_rescan_list_title_comes_from_the_opf_then_the_book(
    key: str,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scan_book(key)
    _open_todo(0)
    _open_todo(1, state=ScanTodo.State.CLAIMED)
    _open_todo(2, state=ScanTodo.State.COMPLETED)
    book = Book.objects.create(title="The Book Title")
    book.authors.add(Author.objects.create(full_name="Ada Author"))
    Identifier.objects.create(book=book, kind="isbn13", value="9780306406157")
    ScanJob.objects.filter(pk=JOB_ID).update(book=book)

    item = _get(key, "rescans").json()["items"][0]
    assert (item["title"], item["authors"], item["isbn"], item["open_rescans"]) == (
        "The Book Title",
        ["Ada Author"],
        "9780306406157",
        2,
    )
    # The current OPF (S6, #130) wins over the Book.
    monkeypatch.setattr(
        scanner_rescans,
        "_opf_fields",
        lambda job: {"title": "OPF Title", "authors": ["Opf Author"], "isbn": "0306406152"},
    )
    item = _get(key, "rescans").json()["items"][0]
    assert (item["title"], item["authors"], item["isbn"]) == (
        "OPF Title",
        ["Opf Author"],
        "0306406152",
    )


# --- thumbnails ---------------------------------------------------------------------------


@pytest.mark.django_db
def test_thumbnail_size_limits_and_etag(key: str) -> None:  # noqa: F811
    big = _jpeg(b"big", size=(2400, 1200))
    assert _put_page(key, 0, big).status_code == 201

    default = _get(key, f"jobs/{JOB_ID}/pages/0/thumbnail")
    assert _jpeg_size(_body(default)) == (800, 400)
    assert default["Cache-Control"] == "private, max-age=0, must-revalidate"
    etag = default["ETag"]
    assert _get(key, f"jobs/{JOB_ID}/pages/0/thumbnail", **{"If-None-Match": etag}).status_code == (
        304
    )
    capped = _get(key, f"jobs/{JOB_ID}/pages/0/thumbnail?max=5000")
    assert _jpeg_size(_body(capped)) == (1600, 800)
    rounded = _get(key, f"jobs/{JOB_ID}/pages/0/thumbnail?max=450")
    assert max(_jpeg_size(_body(rounded))) == 400
    assert rounded["ETag"] != etag
    for bad in ("abc", "-1", "50"):
        response = _get(key, f"jobs/{JOB_ID}/pages/0/thumbnail?max={bad}")
        assert (response.status_code, _code(response)) == (400, "invalid_request")
    assert _get(key, f"jobs/{JOB_ID}/pages/7/thumbnail").status_code == 404
    assert _get(key, f"jobs/{JOB_ID}/pages/x/thumbnail").status_code == 400
    assert _get(key, f"jobs/{OTHER_JOB_ID}/pages/0/thumbnail").status_code == 404


@pytest.mark.django_db
def test_thumbnails_are_cached(key: str, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: F811
    assert _put_page(key, 0, _jpeg(b"cached", size=(400, 300))).status_code == 201
    calls: list[int] = []
    real = scanner_rescans.render_jpeg_thumbnail

    def counting(*args: Any) -> None:
        calls.append(args[-1])
        real(*args)

    monkeypatch.setattr(scanner_rescans, "render_jpeg_thumbnail", counting)
    for _ in range(3):
        assert _get(key, f"jobs/{JOB_ID}/pages/0/thumbnail").status_code == 200
    assert calls == [800]


# --- portal ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_workspace_shows_page_number_verification(
    key: str,  # noqa: F811
    member: Client,
) -> None:
    _scan_book(key)
    todo = _open_todo(0)
    response = _replace(key, todo.pk, _jpeg(b"unverified"), 0, page_number_verified=False)
    assert response.status_code == 201
    html = member.get(f"/scan/jobs/{JOB_ID}/workspace/?view=html").content.decode()
    assert html.count("data-page-number-unverified") == 1
    assert "Page number not verified" in html
    page = member.get(f"/scan/pages/{_page(0).pk}/").json()
    assert page["page_number_verified"] is False
    assert page["active_version"]["page_number_verified"] is False


@pytest.mark.django_db
def test_portal_flagging_states_and_web_jobs(
    key: str,  # noqa: F811
    member: Client,
) -> None:
    _scan_book(key)
    ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.CANCELLED)
    assert _flag(member, 0).status_code == 400
    assert not ScanTodo.objects.exists()

    # A web-created job gets a todo but no scan.jsonl line: it has no scanner log.
    web_job = ScanJob.objects.create(id=str(uuid4()), state=ScanJob.State.CAPTURED)
    web_page = ScanPage.objects.create(scan_job=web_job, logical_sequence_number=1)
    response = member.post(
        f"/scan/pages/{web_page.pk}/rescan/",
        data=json.dumps({"reason": "torn"}),
        content_type="application/json",
    )
    assert response.status_code == 201
    assert not ScanEvent.objects.filter(scan_job=web_job).exists()


@pytest.mark.django_db
def test_flag_and_event_commit_together(
    key: str,  # noqa: F811
    member: Client,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _scan_book(key)

    def broken(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("event store down")

    monkeypatch.setattr(scan_events, "append_serif_event", broken)
    with pytest.raises(RuntimeError):
        _flag(member, 0)
    assert not ScanTodo.objects.exists()


@pytest.mark.django_db
def test_versions_keep_their_files(key: str, _data_dir: Path) -> None:  # noqa: F811
    _scan_book(key)
    todo = _open_todo(0)
    assert _replace(key, todo.pk, _jpeg(b"keep"), 0).status_code == 201
    for version in ScanPageVersion.objects.filter(scan_page=_page(0)):
        assert (_data_dir / "scan-artifacts" / version.storage_identifier).is_file()
