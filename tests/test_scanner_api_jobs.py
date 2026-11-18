"""Scanner-assigned job IDs (#126): ID format, path converter, ``PUT jobs/{job_id}`` (§6.3)."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.test import Client
from django.urls import reverse

from ebook_server.core.models import ScanJob, get_or_create_profile
from ebook_server.core.scan_job_ids import is_device_job_id, normalize_scan_job_id
from ebook_server.core.scanner_api import scanner_job
from ebook_server.core.scanner_auth import generate_key, hash_key

JOB_ID = "20261117T143201Z"
STARTED = "2026-11-17T14:32:01Z"


@pytest.fixture
def key(db: None, settings: Any) -> str:
    """The scanner API key, configured as its SHA-256 (#125)."""
    value = generate_key()
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = hash_key(value)
    return value


def _put(key: str, job_id: str, body: Any, *, raw: bool = False) -> Any:
    return Client().put(
        f"/api/scanner/v1/jobs/{job_id}",
        data=body if raw else json.dumps(body),
        content_type="application/json",
        headers={"Authorization": f"Bearer {key}"},
    )


def _error_code(response: Any) -> str:
    return str(response.json()["error"]["code"])


# --- ID format --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["20261117T143201Z", "20261117T143201Z-1", "20261117T143201Z-2", "20261117T143201Z-9999"],
)
def test_device_job_id_format_accepts(value: str) -> None:
    assert is_device_job_id(value)
    assert normalize_scan_job_id(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "20261117T143201",
        "20261117T143201Z-0",
        "20261117T143201Z-01",
        "20261117T143201Z-10000",
        "2026-11-17T14:32:01Z",
        "20261117t143201z",
        "20261117T143201Z-a1b2",
        "20261117T143201Z\n",
        "../20261117T143201Z",
    ],
)
def test_device_job_id_format_rejects(value: str) -> None:
    assert not is_device_job_id(value)


def test_normalize_scan_job_id_canonicalises_uuids() -> None:
    value = uuid.uuid4()
    assert normalize_scan_job_id(value) == str(value)
    assert normalize_scan_job_id(value.hex.upper()) == str(value)
    assert normalize_scan_job_id("not-an-id") is None
    assert normalize_scan_job_id(None) is None
    assert normalize_scan_job_id("x" * 100) is None


@pytest.mark.django_db
def test_web_jobs_get_uuid_string_ids() -> None:
    job = ScanJob.objects.create()
    assert isinstance(job.pk, str)
    assert str(uuid.UUID(job.pk)) == job.pk
    assert ScanJob.objects.get(pk=uuid.UUID(job.pk)) == job


# --- existing /scan/jobs/ routes accept both ID forms -----------------------------------


def test_scan_routes_reverse_and_resolve_both_id_forms() -> None:
    web_id = str(uuid.uuid4())
    for job_id in (web_id, JOB_ID, f"{JOB_ID}-3"):
        assert reverse("scan-job-detail", kwargs={"job_id": job_id}) == f"/scan/jobs/{job_id}/"
        assert reverse("scan-job-workspace", kwargs={"job_id": job_id}).startswith(
            f"/scan/jobs/{job_id}/"
        )


@pytest.mark.django_db
def test_scan_session_routes_serve_device_jobs() -> None:
    profile = get_or_create_profile("member")
    client = Client()
    client.force_login(get_user_model().objects.get(pk=profile.user_id))
    device_job = ScanJob.objects.create(id=JOB_ID, scanner_id="libreflip-01")
    web_job = ScanJob.objects.create(operator=profile.user)

    for job in (device_job, web_job):
        detail = client.get(f"/scan/jobs/{job.pk}/")
        assert detail.status_code == 200
        assert detail.json()["id"] == job.pk
        assert client.get(f"/scan/jobs/{job.pk}/todos/").status_code == 200
        assert client.get(f"/scan/jobs/{job.pk}/workspace/").status_code == 200
    listing = client.get(f"/scan/todos/?job_id={JOB_ID}")
    assert listing.status_code == 200
    assert client.get("/scan/todos/?job_id=bogus").status_code == 400
    assert client.get("/scan/jobs/20261117T143201Z-0/").status_code == 404
    assert client.get("/scan/jobs/20261117T999999Z/").status_code == 404


# --- PUT jobs/{job_id} ------------------------------------------------------------------


@pytest.mark.django_db
def test_put_job_creates_open_job(key: str, settings: Any) -> None:
    settings.BOOK_SCANNER_PUBLIC_BASE_URL = "https://serif.c-base.lan/"

    response = _put(key, JOB_ID, {"scan_started_at": STARTED, "capture_layout": "single_page"})

    assert response.status_code == 201
    body = response.json()
    assert body == {
        "job_id": JOB_ID,
        "state": "open",
        "portal_url": f"https://serif.c-base.lan/j/{JOB_ID}",
        "scan_started_at": STARTED,
        "events": {"sans_last_line_no": 0},
        "pages": [],
        "covers": [],
        # Registered by the OPF store (#130); null until the scanner sends one.
        "metadata_opf": None,
        "open_rescans": 0,
    }
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.source == ScanJob.Source.SCANNER
    assert job.operator is None
    assert job.state == ScanJob.State.OPEN
    assert job.scan_started_at == datetime(2026, 11, 17, 14, 32, 1, tzinfo=UTC)


@pytest.mark.django_db
def test_put_job_falls_back_to_request_host(key: str, settings: Any) -> None:
    settings.BOOK_SCANNER_PUBLIC_BASE_URL = ""
    response = _put(key, f"{JOB_ID}-2", {"scan_started_at": STARTED})
    assert response.status_code == 201
    assert "capture_layout" not in response.json()
    assert response.json()["portal_url"] == f"http://testserver/j/{JOB_ID}-2"


@pytest.mark.django_db
def test_put_job_repeat_with_same_data_is_200_and_changes_nothing(
    key: str,
) -> None:
    assert _put(key, JOB_ID, {"scan_started_at": STARTED}).status_code == 201
    before = ScanJob.objects.get(pk=JOB_ID)

    # Same instant, different notation.
    again = _put(key, JOB_ID, {"scan_started_at": "2026-11-17T15:32:01+01:00"})

    assert again.status_code == 200
    assert again.json()["job_id"] == JOB_ID
    after = ScanJob.objects.get(pk=JOB_ID)
    assert after.updated_at == before.updated_at
    assert ScanJob.objects.count() == 1


@pytest.mark.django_db
def test_put_job_conflicts_are_409(key: str) -> None:
    assert _put(key, JOB_ID, {"scan_started_at": STARTED}).status_code == 201

    other_time = _put(key, JOB_ID, {"scan_started_at": "2026-11-17T14:32:02Z"})
    assert other_time.status_code == 409
    assert _error_code(other_time) == "job_id_conflict"

    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.scan_started_at == datetime(2026, 11, 17, 14, 32, 1, tzinfo=UTC)
    assert job.source == ScanJob.Source.SCANNER


@pytest.mark.django_db
def test_put_job_accepts_and_ignores_legacy_single_page_layout(key: str) -> None:
    """#195: ``capture_layout: single_page`` is still accepted, so the scanner keeps working."""
    created = _put(key, JOB_ID, {"scan_started_at": STARTED, "capture_layout": "single_page"})
    assert created.status_code == 201
    # The field is not part of the job identity any more: a repeat without it is the same job.
    again = _put(key, JOB_ID, {"scan_started_at": STARTED})
    assert again.status_code == 200
    assert "capture_layout" not in again.json()


@pytest.mark.django_db
def test_put_job_conflicts_with_web_job_of_same_id(key: str) -> None:
    ScanJob.objects.create(id=JOB_ID)
    response = _put(key, JOB_ID, {"scan_started_at": STARTED})
    assert response.status_code == 409
    assert _error_code(response) == "job_id_conflict"
    assert ScanJob.objects.get(pk=JOB_ID).source == ScanJob.Source.WEB
    assert ScanJob.objects.get(pk=JOB_ID).scan_started_at is None


@pytest.mark.django_db
@pytest.mark.parametrize(
    "job_id",
    [
        str(uuid.uuid4()),
        "20261117T143201Z-0",
        "2026-11-17T14:32:01Z",
        "nope",
        "20261117T143201Z-a1b2",
    ],
)
def test_put_job_rejects_bad_job_id_format(key: str, job_id: str) -> None:
    response = _put(key, job_id, {"scan_started_at": STARTED})
    assert response.status_code == 400
    assert _error_code(response) == "invalid_job_id"
    assert not ScanJob.objects.exists()


@pytest.mark.django_db
@pytest.mark.parametrize(
    "body",
    [
        "not json",
        "[]",
        "{}",
        json.dumps({"scan_started_at": "yesterday"}),
        json.dumps({"scan_started_at": "2026-11-17T14:32:01"}),
        json.dumps({"scan_started_at": 1763389921}),
        json.dumps({"scan_started_at": STARTED, "capture_layout": "triptych"}),
        # Page Split was removed (#195): two-page spreads are refused.
        json.dumps({"scan_started_at": STARTED, "capture_layout": "spread"}),
        json.dumps({"scan_started_at": STARTED, "capture_layout": 1}),
        # ISO-shaped but not a calendar date/time (PDXDJN on #137): 400, never 500.
        json.dumps({"scan_started_at": "2026-02-30T14:32:01Z"}),
        json.dumps({"scan_started_at": "2026-13-01T14:32:01Z"}),
        json.dumps({"scan_started_at": "2026-11-17T25:32:01Z"}),
        json.dumps({"scan_started_at": "2026-11-17T14:32:01+25:00"}),
        json.dumps({"scan_started_at": "0001-01-01T00:00:00+01:00"}),
    ],
)
def test_put_job_rejects_bad_bodies(key: str, body: str) -> None:
    response = _put(key, JOB_ID, body, raw=True)
    assert response.status_code == 400
    assert _error_code(response) in {"invalid_json", "invalid_request"}
    assert not ScanJob.objects.exists()


@pytest.mark.django_db
def test_put_job_invalid_calendar_date_is_400_invalid_request(key: str) -> None:
    response = _put(key, JOB_ID, {"scan_started_at": "2026-02-30T14:32:01Z"})
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["code"] == "invalid_request"
    assert body["error"]["details"] == {"field": "scan_started_at"}
    assert not ScanJob.objects.exists()


@pytest.mark.django_db
def test_put_job_requires_the_scanner_key(key: str) -> None:
    response = Client().put(
        f"/api/scanner/v1/jobs/{JOB_ID}",
        data=json.dumps({"scan_started_at": STARTED}),
        content_type="application/json",
    )
    assert response.status_code == 401
    assert not ScanJob.objects.exists()


@pytest.mark.django_db
def test_scanner_job_lookup_serves_only_scanner_jobs(key: str) -> None:
    """Scanner job routes resolve only ``source=scanner`` jobs; others are a 404."""
    assert _put(key, JOB_ID, {"scan_started_at": STARTED}).status_code == 201
    web_with_scanner_shaped_id = ScanJob.objects.create(id=f"{JOB_ID}-5")
    web_job = ScanJob.objects.create()

    found = scanner_job(JOB_ID)
    assert found is not None and found.pk == JOB_ID
    assert scanner_job(web_with_scanner_shaped_id.pk) is None
    assert scanner_job(web_job.pk) is None
    assert scanner_job(f"{JOB_ID}-9") is None


@pytest.mark.django_db
def test_job_route_only_accepts_put(key: str) -> None:
    response = Client().post(
        f"/api/scanner/v1/jobs/{JOB_ID}", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 405
