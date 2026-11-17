"""Raw-scan lifecycle (#127, libreflip contract §5, §6.4, §6.9, §6.10, §9, §10 items 1 & 3).

* ``captured`` state, ``GET jobs/{id}``, ``capture-complete`` and ``abort``;
* finish from ``captured`` and the re-finish after a replacement on a complete job;
* the portal's ``/j/<job_id>`` link and "Raw scans" list;
* raw scans are invisible to library browse, search, home, detail, download and covers.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.core.cache import cache
from django.test import Client

from ebook_server.core import (
    scan_identification,
    scan_lifecycle,
    scan_manifest,
    scan_pipeline,
    search,
)
from ebook_server.core.models import (
    Book,
    BookSearchDocument,
    FileVariant,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.core.scanner_auth import generate_key, hash_key
from tests.test_scan_jobs import _png_for, _put_scan_page, _upload_scan_page

JOB_ID = "20261117T143201Z"
STARTED = "2026-11-17T14:32:01Z"
BASE_URL = "https://serif.c-base.lan/"


@pytest.fixture(autouse=True)
def _public_base_url(settings: Any) -> None:
    settings.BOOK_SCANNER_PUBLIC_BASE_URL = BASE_URL


@pytest.fixture(autouse=True)
def _fresh_search_cache() -> Iterator[None]:
    # A rebuild caches "index ready"; later tests must not inherit it.
    cache.clear()
    yield
    cache.clear()


@pytest.fixture
def key(db: None, settings: Any) -> str:
    """The scanner API key, configured as its SHA-256 (#125: one scanner, one key)."""
    value = generate_key()
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = hash_key(value)
    return value


@pytest.fixture
def member() -> Client:
    client = Client()
    client.force_login(get_or_create_profile("raw-scan-member").user)
    return client


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _register(key: str, job_id: str = JOB_ID) -> Any:
    response = Client().put(
        f"/api/scanner/v1/jobs/{job_id}",
        data=json.dumps({"scan_started_at": STARTED}),
        content_type="application/json",
        headers=_auth(key),
    )
    assert response.status_code in (200, 201), response.content
    return response


def _status(key: str, job_id: str = JOB_ID) -> Any:
    return Client().get(f"/api/scanner/v1/jobs/{job_id}", headers=_auth(key))


def _capture_complete(key: str, body: Any, job_id: str = JOB_ID) -> Any:
    return Client().post(
        f"/api/scanner/v1/jobs/{job_id}/capture-complete",
        data=json.dumps(body),
        content_type="application/json",
        headers=_auth(key),
    )


def _abort(key: str, job_id: str = JOB_ID) -> Any:
    return Client().post(f"/api/scanner/v1/jobs/{job_id}/abort", headers=_auth(key))


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _add_page(client: Client, job_id: str, sequence: int, label: bytes) -> str:
    """Store a page through the session API; returns its SHA-256.

    The scanner page upload (§6.5) arrives with S4; the lifecycle only needs pages.
    """
    image = _png_for(label)
    response = _upload_scan_page(
        client, job_id, sequence=sequence, side="single", payload=image, filename="p.png"
    )
    assert response.status_code in (200, 201), response.content
    return _sha(image)


def _manifest(pages: dict[int, str], **extra: Any) -> dict[str, Any]:
    return {
        "finished_at": "2026-11-17T15:40:00Z",
        "sans_last_line_no": 0,
        "pages": [{"sequence_number": n, "sha256": sha} for n, sha in sorted(pages.items())],
        # Covers are checked since S4 (#128); these jobs have none.
        "covers": [],
        "metadata_opf": None,
        **extra,
    }


def _location() -> StorageLocation:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return location


def _variant(book: Book, source_type: str, *, scan_job: ScanJob | None = None) -> FileVariant:
    count = FileVariant.objects.count() + 1
    return FileVariant.objects.create(
        book=book,
        format="pdf" if source_type == FileVariant.SourceType.CLEANED else "epub",
        mime_type="application/octet-stream",
        original_filename=f"file-{count}",
        storage_location=_location(),
        storage_identifier=f"books/raw-scan-test-{count}",
        size_bytes=count,
        sha256=f"{count:064x}",
        source_type=source_type,
        processing_state=FileVariant.ProcessingState.COMPLETE,
        converter_options={"scan_job_id": str(scan_job.pk)} if scan_job else {},
    )


# --- GET jobs/{id} (§6.4) ---------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_get_job_reports_state_inventory_and_portal_url(key: str, member: Client) -> None:
    _register(key)
    sha1 = _add_page(member, JOB_ID, 1, b"one")
    sha2 = _add_page(member, JOB_ID, 2, b"two")

    response = _status(key)

    assert response.status_code == 200
    body = response.json()
    assert body["job_id"] == JOB_ID
    assert body["state"] == "capturing"
    assert body["portal_url"] == f"{BASE_URL}j/{JOB_ID}"
    # No stored events yet (S5): the high-water mark is 0.
    assert body["events"] == {"sans_last_line_no": 0}
    assert body["pages"] == [
        {"sequence_number": 1, "sha256": sha1, "version": 1},
        {"sequence_number": 2, "sha256": sha2, "version": 1},
    ]
    # No covers uploaded (S4 reports an empty list); the OPF is not stored yet (S6).
    assert body["covers"] == []
    assert "metadata_opf" not in body
    assert body["open_rescans"] == 0


@pytest.mark.django_db
def test_get_job_is_404_for_web_jobs_and_unknown_ids(key: str) -> None:
    web_job = ScanJob.objects.create(id="20261117T150000Z")
    assert web_job.source == ScanJob.Source.WEB

    assert _status(key).status_code == 404
    assert _status(key, web_job.pk).status_code == 404
    assert _status(key, "20261117T160000Z").status_code == 404
    bad = _status(key, "not-a-job-id")
    assert bad.status_code == 400
    assert bad.json()["error"]["code"] == "invalid_job_id"


# --- capture-complete (§6.9) --------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_capture_complete_verifies_manifest_and_is_idempotent(key: str, member: Client) -> None:
    _register(key)
    pages = {1: _add_page(member, JOB_ID, 1, b"one"), 2: _add_page(member, JOB_ID, 2, b"two")}

    response = _capture_complete(key, _manifest(pages))

    assert response.status_code == 200, response.content
    assert response.json() == {
        "verified": True,
        "state": "captured",
        "portal_url": f"{BASE_URL}j/{JOB_ID}",
    }
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.state == ScanJob.State.CAPTURED
    assert job.captured_at is not None
    assert job.capture_finished_at is not None
    assert job.capture_finished_at.isoformat().startswith("2026-11-17T15:40:00")

    captured_at = job.captured_at
    repeat = _capture_complete(key, _manifest(pages))
    assert repeat.status_code == 200
    assert repeat.json() == response.json()
    job.refresh_from_db()
    assert job.captured_at == captured_at
    assert _status(key).json()["state"] == "captured"


@pytest.mark.django_db
def test_capture_complete_from_open_job_with_no_pages(key: str) -> None:
    _register(key)

    response = _capture_complete(key, _manifest({}))

    assert response.status_code == 200
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.CAPTURED


@pytest.mark.django_db(transaction=True)
def test_capture_complete_lists_missing_and_mismatched_items(key: str, member: Client) -> None:
    _register(key)
    pages = {1: _add_page(member, JOB_ID, 1, b"one"), 2: _add_page(member, JOB_ID, 2, b"two")}
    manifest = _manifest(
        {1: pages[1], 2: "f" * 64, 3: "e" * 64},
        sans_last_line_no=57,
    )

    response = _capture_complete(key, manifest)

    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "incomplete"
    assert error["details"]["missing"] == ["page:3"]
    assert error["details"]["mismatched"] == ["page:2", "events"]
    assert error["details"]["sans_last_line_no"] == 0
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.CAPTURING


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("extra", "missing"),
    [
        ({"covers": [{"position": "front", "sha256": "a" * 64}]}, ["cover:front"]),
        (
            {
                "covers": [
                    {"position": "front", "sha256": "a" * 64},
                    {"position": "back", "sha256": "b" * 64},
                ]
            },
            ["cover:front", "cover:back"],
        ),
        ({"metadata_opf": {"sha256": "c" * 64}}, ["metadata_opf"]),
    ],
)
def test_capture_complete_fails_closed_for_items_serif_cannot_store_yet(
    key: str, extra: dict[str, Any], missing: list[str]
) -> None:
    """A declared cover that was never uploaded (S4 checks it) or an OPF serif cannot
    store yet (S6) was never stored: capture-complete must not tell the scanner it may
    delete it."""
    _register(key)

    response = _capture_complete(key, _manifest({}, **extra))

    assert response.status_code == 409, response.content
    error = response.json()["error"]
    assert error["code"] == "incomplete"
    assert error["details"]["missing"] == missing
    assert error["details"]["mismatched"] == []
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.state == ScanJob.State.OPEN
    assert job.capture_manifest_sha256 == ""


@pytest.mark.django_db
def test_capture_complete_fails_closed_for_events_serif_has_not_stored(key: str) -> None:
    _register(key)

    response = _capture_complete(key, _manifest({}, sans_last_line_no=3))

    assert response.status_code == 409
    assert response.json()["error"]["details"]["mismatched"] == ["events"]
    assert response.json()["error"]["details"]["sans_last_line_no"] == 0
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.OPEN


@pytest.mark.django_db(transaction=True)
def test_capture_complete_after_success_revalidates_a_changed_manifest(
    key: str, member: Client
) -> None:
    """Success is bound to the verified manifest: an identical retry is 200, a changed
    manifest (an absent page 9, a cover, a later event line) is checked again."""
    _register(key)
    pages = {1: _add_page(member, JOB_ID, 1, b"one"), 2: _add_page(member, JOB_ID, 2, b"two")}
    assert _capture_complete(key, _manifest(pages)).status_code == 200
    job = ScanJob.objects.get(pk=JOB_ID)
    digest = job.capture_manifest_sha256
    assert len(digest) == 64

    with_page_9 = _capture_complete(key, _manifest({**pages, 9: "9" * 64}))
    assert with_page_9.status_code == 409, with_page_9.content
    assert with_page_9.json()["error"]["code"] == "incomplete"
    assert with_page_9.json()["error"]["details"]["missing"] == ["page:9"]

    changed_page = _capture_complete(key, _manifest({1: pages[1], 2: "f" * 64}))
    assert changed_page.status_code == 409
    assert changed_page.json()["error"]["details"]["mismatched"] == ["page:2"]

    with_cover = _capture_complete(
        key, _manifest(pages, covers=[{"position": "front", "sha256": "a" * 64}])
    )
    assert with_cover.status_code == 409
    assert with_cover.json()["error"]["details"]["missing"] == ["cover:front"]

    later_events = _capture_complete(key, _manifest(pages, sans_last_line_no=4))
    assert later_events.status_code == 409
    assert later_events.json()["error"]["details"]["mismatched"] == ["events"]

    # The job is untouched by the refused attempts, and an identical retry (pages in
    # any order, another finished_at) still succeeds.
    job.refresh_from_db()
    assert job.state == ScanJob.State.CAPTURED
    assert job.capture_manifest_sha256 == digest
    retry = _manifest(pages, finished_at="2026-11-17T15:41:00Z")
    retry["pages"].reverse()
    repeat = _capture_complete(key, retry)
    assert repeat.status_code == 200, repeat.content
    assert repeat.json()["verified"] is True

    # A subset of what serif holds is a different manifest, but it verifies.
    subset = _capture_complete(key, _manifest({1: pages[1]}))
    assert subset.status_code == 200
    job.refresh_from_db()
    assert job.capture_manifest_sha256 == digest


@pytest.mark.django_db(transaction=True)
def test_identical_retry_after_a_replacement_still_verifies(
    key: str, member: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a rescan replaced page 1 on a complete job, the scanner's lost-response
    retry of the manifest serif verified is still 200; any other manifest is checked
    against what serif holds now."""
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    book = Book.objects.create(title="Retry After Rescan")
    key, pages = _captured_job_with_book(key, member, book)
    assert (
        member.post(f"/scan/jobs/{JOB_ID}/finish/", data={"allow_raw_pages": "true"}).status_code
        == 202
    )
    page = ScanPage.objects.get(scan_job_id=JOB_ID)
    assert member.post(f"/scan/pages/{page.id}/rescan/", data={"reason": "x"}).status_code == 201
    assert _put_scan_page(member, page.id, _png_for(b"sharper")).status_code == 201

    assert _capture_complete(key, _manifest(pages)).status_code == 200
    stale = _capture_complete(key, _manifest(pages, finished_at=None, sans_last_line_no=0))
    assert stale.status_code == 200  # finished_at is not part of the manifest identity
    other = _capture_complete(key, _manifest({**pages, 2: "2" * 64}))
    assert other.status_code == 409


@pytest.mark.django_db
def test_capture_complete_on_cancelled_job_is_job_state(key: str) -> None:
    _register(key)
    assert _abort(key).status_code == 200

    response = _capture_complete(key, _manifest({}))

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_state"
    assert response.json()["error"]["details"] == {"state": "cancelled"}


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"pages": [{"sequence_number": 0, "sha256": "ABC"}]}, "pages.sha256"),
        ({"pages": [{"sequence_number": -1, "sha256": "a" * 64}]}, "pages"),
        (
            {
                "pages": [
                    {"sequence_number": 0, "sha256": "a" * 64},
                    {"sequence_number": 0, "sha256": "a" * 64},
                ]
            },
            "pages",
        ),
        ({"pages": {}}, "pages"),
        ({"sans_last_line_no": "12"}, "sans_last_line_no"),
        ({"finished_at": "yesterday"}, "finished_at"),
        # ISO-shaped but not a calendar date: 400, never 500.
        ({"finished_at": "2026-02-30T15:40:00Z"}, "finished_at"),
    ],
)
def test_capture_complete_rejects_malformed_manifests(
    key: str, body: dict[str, Any], field: str
) -> None:
    _register(key)

    response = _capture_complete(key, body)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_request"
    assert response.json()["error"]["details"]["field"] == field
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.OPEN


@pytest.mark.django_db
def test_capture_complete_and_abort_are_404_for_web_jobs_and_unknown_ids(key: str) -> None:
    """Only ``source=scanner`` jobs are reachable; a web job with a scanner-shaped ID
    looks exactly like an unknown one."""
    ScanJob.objects.create(id=JOB_ID)

    assert _capture_complete(key, _manifest({})).status_code == 404
    assert _abort(key).status_code == 404
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.state == ScanJob.State.OPEN
    assert job.source == ScanJob.Source.WEB

    unknown = "20261117T160000Z"
    assert _capture_complete(key, _manifest({}), unknown).status_code == 404
    assert _abort(key, unknown).status_code == 404


class _FakeCoverSource:
    """Stands in for the S4 cover store: what a registered source looks like."""

    name = "covers"

    def __init__(self, held: dict[str, str]) -> None:
        self.held = held

    def received(self, job: ScanJob) -> dict[str, scan_manifest.ReceivedItem]:
        return {
            f"cover:{pos}": scan_manifest.ReceivedItem(sha, 1) for pos, sha in self.held.items()
        }

    def status(self, job: ScanJob) -> list[dict[str, Any]]:
        return [{"position": pos, "sha256": sha, "version": 1} for pos, sha in self.held.items()]

    def expected(self, value: Any) -> dict[str, str]:
        return {f"cover:{item['position']}": item["sha256"] for item in value or []}


@pytest.fixture
def fake_extensions() -> Iterator[_FakeCoverSource]:
    real = {source.name: source for source in scan_manifest.manifest_sources()}.get("covers")
    source = _FakeCoverSource({"front": "a" * 64})
    scan_manifest.register_manifest_source(source)
    real_high_water_mark = scan_manifest._event_high_water_mark  # S5's, from apps.ready
    scan_manifest.register_event_high_water_mark(lambda job: 812)
    yield source
    scan_manifest.unregister_manifest_source("covers")
    if real is not None:  # the S4 cover store (#128)
        scan_manifest.register_manifest_source(real)
    scan_manifest.register_event_high_water_mark(real_high_water_mark)


@pytest.mark.django_db
def test_registered_sources_extend_status_and_manifest_check(
    key: str, fake_extensions: _FakeCoverSource
) -> None:
    _register(key)

    status = _status(key).json()
    assert status["covers"] == [{"position": "front", "sha256": "a" * 64, "version": 1}]
    assert status["events"] == {"sans_last_line_no": 812}

    covers = [
        {"position": "front", "sha256": "a" * 64},
        {"position": "back", "sha256": "b" * 64},
    ]
    incomplete = _capture_complete(key, _manifest({}, covers=covers, sans_last_line_no=812))
    assert incomplete.status_code == 409
    assert incomplete.json()["error"]["details"]["missing"] == ["cover:back"]
    assert incomplete.json()["error"]["details"]["mismatched"] == []

    fake_extensions.held["back"] = "b" * 64
    verified = _capture_complete(key, _manifest({}, covers=covers, sans_last_line_no=812))
    assert verified.status_code == 200


# --- §10 item 3: interruption ------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_interrupted_upload_leaves_no_duplicates_and_capture_complete_verifies(
    key: str, member: Client
) -> None:
    """A lost response makes the scanner resend a page: still one version, and the
    manifest verifies. (The scanner-side half of §10.3 lives in sans.)"""
    _register(key)
    sha = _add_page(member, JOB_ID, 1, b"page-one")
    # The response to the first upload was "lost"; GET jobs/{id} after the restart...
    assert _status(key).json()["pages"] == [{"sequence_number": 1, "sha256": sha, "version": 1}]
    # ...or a blind resend of the same bytes: idempotent.
    assert _add_page(member, JOB_ID, 1, b"page-one") == sha
    assert ScanPageVersion.objects.filter(scan_page__scan_job_id=JOB_ID).count() == 1

    response = _capture_complete(key, _manifest({1: sha}))

    assert response.status_code == 200
    assert response.json()["verified"] is True


# --- abort (§6.10) -----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_abort_cancels_idempotently_and_keeps_data(key: str, member: Client) -> None:
    _register(key)
    _add_page(member, JOB_ID, 1, b"one")

    first = _abort(key)
    assert first.status_code == 200
    assert first.json()["state"] == "cancelled"
    second = _abort(key)
    assert second.status_code == 200
    assert second.json()["state"] == "cancelled"
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.state == ScanJob.State.CANCELLED
    assert job.finished_at is not None
    assert ScanPage.objects.filter(scan_job=job).count() == 1
    assert ScanPageVersion.objects.filter(scan_page__scan_job=job).count() == 1


@pytest.mark.django_db
def test_abort_is_refused_after_capture_complete(key: str) -> None:
    _register(key)
    assert _capture_complete(key, _manifest({})).status_code == 200

    response = _abort(key)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "job_state"
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.CAPTURED


# --- portal: /j/<job_id> and "Raw scans" -------------------------------------------------


@pytest.mark.django_db
def test_portal_link_redirects_through_login_to_workspace(
    key: str,
) -> None:
    _register(key)
    assert _capture_complete(key, _manifest({})).status_code == 200
    ScanJob.objects.filter(pk=JOB_ID).update(provisional_title="Raw Scan Title")

    anonymous = Client().get(f"/j/{JOB_ID}")
    assert anonymous.status_code == 302
    assert anonymous["Location"].startswith("/accounts/login/")
    assert f"next=/j/{JOB_ID}" in anonymous["Location"]

    client = Client()
    client.force_login(get_or_create_profile("qr-reader").user)
    response = client.get(f"/j/{JOB_ID}")
    assert response.status_code == 302
    assert response["Location"] == f"/scan/jobs/{JOB_ID}/workspace/?view=html"
    assert client.get(f"/j/{JOB_ID}/").status_code == 302

    page = client.get(response["Location"])
    assert page.status_code == 200
    html = page.content.decode()
    assert "Raw scan" in html
    assert "Raw Scan Title" in html

    assert client.get("/j/20261117T160000Z").status_code == 404


@pytest.mark.django_db
def test_raw_scans_list_shows_captured_jobs(key: str) -> None:
    _register(key)
    _register(key, "20261117T150000Z")  # still capturing: not a raw scan yet
    assert _capture_complete(key, _manifest({})).status_code == 200
    ScanJob.objects.filter(pk=JOB_ID).update(provisional_title="Captured Book")
    client = Client()
    client.force_login(get_or_create_profile("raw-list").user)

    payload = client.get("/scan/raw/").json()
    assert payload["count"] == 1
    item = payload["items"][0]
    assert item["job_id"] == JOB_ID
    assert item["title"] == "Captured Book"
    assert item["page_count"] == 0
    assert item["open_rescans"] == 0
    assert item["refinish_needed"] is False

    html = client.get("/scan/raw/?view=html").content.decode()
    assert "Captured Book" in html
    assert "20261117T150000Z" not in html

    def annotate(summary: scan_lifecycle.RawScanSummary) -> None:
        summary.title = "From the OPF"
        summary.completeness["all_pages"] = True

    scan_lifecycle.register_raw_scan_annotator(annotate)
    try:
        item = client.get("/scan/raw/").json()["items"][0]
    finally:
        scan_lifecycle.unregister_raw_scan_annotator(annotate)
    assert item["title"] == "From the OPF"
    assert item["completeness"] == {"all_pages": True}


# --- raw scans are invisible to the library -----------------------------------------------


def _library_titles(client: Client, **params: str) -> list[str]:
    response = client.get("/library/books/", params)
    assert response.status_code == 200
    return [item["title"] for item in response.json()["items"]]


@pytest.mark.django_db
def test_captured_scan_book_is_invisible_until_finished(
    member: Client, django_capture_on_commit_callbacks: Any
) -> None:
    visible = Book.objects.create(title="Library Book")
    raw = Book.objects.create(title="Raw Scan Book")
    _variant(visible, FileVariant.SourceType.UPLOAD)
    with django_capture_on_commit_callbacks(execute=True):
        job = ScanJob.objects.create(
            id=JOB_ID, state=ScanJob.State.CAPTURED, book=raw, provisional_title="Raw"
        )
        cleaned = _variant(raw, FileVariant.SourceType.CLEANED, scan_job=job)

    assert _library_titles(member) == ["Library Book"]
    assert _library_titles(member, q="Raw Scan") == []
    assert _library_titles(member, title="Raw") == []
    assert _library_titles(member, format="pdf") == []
    home = member.get("/library/?view=html")
    assert "Raw Scan Book" not in home.content.decode()
    assert home.context["book_count"] == 1
    assert member.get(f"/library/books/{raw.id}/").status_code == 404
    assert member.get(f"/library/books/{raw.id}/download/").status_code == 404
    assert member.get(f"/library/books/{raw.id}/covers/front/").status_code == 404
    # Never indexed: the search row is removed and a rebuild does not add it back.
    assert not BookSearchDocument.objects.filter(book=raw).exists()
    search.rebuild_search_index()
    assert not BookSearchDocument.objects.filter(book=raw).exists()
    assert search.verify_search_index().ok

    for state in (
        ScanJob.State.OPEN,
        ScanJob.State.CAPTURING,
        ScanJob.State.PAUSED,
        ScanJob.State.FINALIZING,
        ScanJob.State.CANCELLED,
    ):
        ScanJob.objects.filter(pk=job.pk).update(state=state)
        assert "Raw Scan Book" not in _library_titles(member), state

    with django_capture_on_commit_callbacks(execute=True):
        job.state = ScanJob.State.COMPLETE
        job.save()

    assert sorted(_library_titles(member)) == ["Library Book", "Raw Scan Book"]
    assert BookSearchDocument.objects.filter(book=raw).exists()
    detail = member.get(f"/library/books/{raw.id}/").json()
    assert [variant["id"] for variant in detail["variants"]] == [cleaned.id]


@pytest.mark.django_db
def test_raw_scan_never_hides_a_book_already_in_the_library(member: Client) -> None:
    book = Book.objects.create(title="Existing Library Book")
    upload = _variant(book, FileVariant.SourceType.UPLOAD)
    job = ScanJob.objects.create(id=JOB_ID, state=ScanJob.State.CAPTURED, book=book)
    pending = _variant(book, FileVariant.SourceType.CLEANED, scan_job=job)

    assert _library_titles(member) == ["Existing Library Book"]
    detail = member.get(f"/library/books/{book.id}/").json()
    # The book stays, but the variant built from the unfinished scan is not served.
    assert [variant["id"] for variant in detail["variants"]] == [upload.id]
    assert detail["formats_available"] == ["epub"]
    listed = member.get("/library/books/").json()["items"][0]
    assert listed["formats_available"] == ["epub"]
    download = member.get(f"/library/books/{book.id}/download/", {"variant_id": pending.id})
    assert download.status_code == 400
    assert member.get(f"/library/books/{book.id}/download/", {"format": "pdf"}).status_code == 404
    assert _library_titles(member, format="pdf") == []


@pytest.mark.django_db
def test_book_with_a_completed_scan_stays_visible_during_a_second_raw_scan(
    member: Client,
) -> None:
    book = Book.objects.create(title="Scanned Twice")
    first = ScanJob.objects.create(state=ScanJob.State.COMPLETE, book=book)
    _variant(book, FileVariant.SourceType.CLEANED, scan_job=first)
    ScanJob.objects.create(id=JOB_ID, state=ScanJob.State.CAPTURED, book=book)

    assert _library_titles(member) == ["Scanned Twice"]


# --- finish from captured, re-finish after a replacement (§5) ----------------------------


def _captured_job_with_book(key: str, member: Client, book: Book) -> tuple[str, dict[int, str]]:
    _register(key)
    pages = {1: _add_page(member, JOB_ID, 1, b"one")}
    assert _capture_complete(key, _manifest(pages)).status_code == 200
    ScanJob.objects.filter(pk=JOB_ID).update(book=book)
    return key, pages


@pytest.mark.django_db(transaction=True)
def test_finish_from_captured_requires_processed_output_unless_allowed(
    key: str, member: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    book = Book.objects.create(title="Finished From Captured")
    _captured_job_with_book(key, member, book)

    refused = member.post(f"/scan/jobs/{JOB_ID}/finish/")
    assert refused.status_code == 400
    assert "processed output" in refused.content.decode()
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.CAPTURED
    assert _library_titles(member) == []

    finished = member.post(f"/scan/jobs/{JOB_ID}/finish/", data={"allow_raw_pages": "true"})
    assert finished.status_code == 202, finished.content
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.COMPLETE
    assert _library_titles(member) == ["Finished From Captured"]


@pytest.mark.django_db(transaction=True)
def test_finish_uses_a_registered_book_linker_before_identification(
    key: str, member: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    _register(key)
    _add_page(member, JOB_ID, 1, b"one")
    book = Book.objects.create(title="Linked From OPF")

    def linker(job: ScanJob) -> Book | None:
        job.book = book
        job.save(update_fields=["book", "updated_at"])
        return book

    scan_lifecycle.register_book_linker(linker)
    try:
        response = member.post(f"/scan/jobs/{JOB_ID}/finish/", data={"allow_raw_pages": "true"})
    finally:
        scan_lifecycle.unregister_book_linker(linker)

    assert response.status_code == 202, response.content
    assert "requires_bibliographic_review" not in response.json()
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.book_id == book.id
    assert job.state == ScanJob.State.COMPLETE


@pytest.mark.django_db(transaction=True)
def test_finish_without_a_linked_book_keeps_scan_identification(
    key: str, member: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    _register(key)
    assert _capture_complete(key, _manifest({})).status_code == 200
    calls: list[str] = []

    def no_book(job: ScanJob) -> Book | None:
        calls.append("linker")
        return None

    def identify(*, scan_job: ScanJob, requested_by_user_id: int | None) -> Any:
        calls.append("identification")
        raise scan_identification.ScanIdentificationError("identification queued (stub)")

    monkeypatch.setattr(scan_identification, "request_scan_identification", identify)
    scan_lifecycle.register_book_linker(no_book)
    try:
        response = member.post(f"/scan/jobs/{JOB_ID}/finish/")
    finally:
        scan_lifecycle.unregister_book_linker(no_book)

    assert calls == ["linker", "identification"]
    assert "identification queued (stub)" in response.content.decode()
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.CAPTURED


@pytest.mark.django_db(transaction=True)
def test_replacement_on_complete_job_returns_to_captured_until_refinished(
    key: str, member: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    book = Book.objects.create(title="Rescanned After Finish")
    key, _ = _captured_job_with_book(key, member, book)
    assert (
        member.post(f"/scan/jobs/{JOB_ID}/finish/", data={"allow_raw_pages": "true"}).status_code
        == 202
    )
    old = FileVariant.objects.get(book=book, source_type=FileVariant.SourceType.CLEANED)
    page = ScanPage.objects.get(scan_job_id=JOB_ID)

    # D7: a rescan may be requested and delivered after the job is complete.
    rescan = member.post(f"/scan/pages/{page.id}/rescan/", data={"reason": "blurry"})
    assert rescan.status_code == 201, rescan.content
    replacement = _put_scan_page(member, page.id, _png_for(b"sharper"))
    assert replacement.status_code == 201, replacement.content

    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.state == ScanJob.State.CAPTURED
    assert job.refinish_needed is True
    assert job.notes["lifecycle"] == "re-finish needed"
    assert _status(key).json()["note"] == "re-finish needed"
    assert ScanPageVersion.objects.filter(scan_page=page).count() == 2
    # The library keeps the existing variant until the next finish.
    old.refresh_from_db()
    assert old.processing_state == FileVariant.ProcessingState.COMPLETE
    assert _library_titles(member) == ["Rescanned After Finish"]
    detail = member.get(f"/library/books/{book.id}/").json()
    assert [variant["id"] for variant in detail["variants"]] == [old.id]
    raw_list = member.get("/scan/raw/").json()["items"]
    assert raw_list[0]["refinish_needed"] is True

    refinished = member.post(f"/scan/jobs/{JOB_ID}/finish/", data={"allow_raw_pages": "true"})
    assert refinished.status_code == 202, refinished.content
    job.refresh_from_db()
    assert job.state == ScanJob.State.COMPLETE
    assert job.refinish_needed is False
    assert "lifecycle" not in job.notes
    old.refresh_from_db()
    assert old.processing_state == FileVariant.ProcessingState.FAILED
    current = FileVariant.objects.get(
        book=book,
        source_type=FileVariant.SourceType.CLEANED,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )
    assert current.id != old.id


@pytest.mark.django_db(transaction=True)
def test_captured_job_accepts_processing_but_not_new_pages(key: str, member: Client) -> None:
    _register(key)
    _add_page(member, JOB_ID, 1, b"one")
    assert _capture_complete(key, _manifest({})).status_code == 200
    page = ScanPage.objects.get(scan_job_id=JOB_ID)

    new_page = _upload_scan_page(
        member, JOB_ID, sequence=2, side="single", payload=_png_for(b"two"), filename="p.png"
    )
    assert new_page.status_code == 400
    rescan = member.post(f"/scan/pages/{page.id}/rescan/", data={"reason": "skewed"})
    assert rescan.status_code == 201
    assert _status(key).json()["open_rescans"] == 1
    cancel = member.post(f"/scan/jobs/{JOB_ID}/cancel/")
    assert cancel.status_code == 200
    assert ScanJob.objects.get(pk=JOB_ID).state == ScanJob.State.CANCELLED
