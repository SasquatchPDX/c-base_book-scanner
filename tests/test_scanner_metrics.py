"""Scanner device API metrics (#133, contract §9)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest import mock

import pytest
from django.db import DatabaseError
from django.http import JsonResponse
from django.test import Client, RequestFactory, override_settings
from prometheus_client.parser import text_string_to_metric_families

from ebook_server.core import scanner_metrics
from ebook_server.core.models import ScanJob, ScannerApiRequestCount, ScannerDevice
from ebook_server.core.scanner_api import scanner_api_view
from ebook_server.core.scanner_devices import create_device, revoke_device
from ebook_server.core.scanner_metrics import record_request

PING = "/api/scanner/v1/ping"
METRICS_SETTINGS = override_settings(
    BOOK_SCANNER_METRICS_ENABLED=True,
    BOOK_SCANNER_METRICS_BEARER_TOKEN="metrics-secret",
    BOOK_SCANNER_QUEUE_URL="inline://",
)


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _scrape() -> tuple[str, dict[str, list[tuple[dict[str, str], float]]]]:
    with METRICS_SETTINGS:
        response = Client().get("/metrics/", HTTP_AUTHORIZATION="Bearer metrics-secret")
    assert response.status_code == 200
    text = response.content.decode()
    samples: dict[str, list[tuple[dict[str, str], float]]] = {}
    for family in text_string_to_metric_families(text):
        for sample in family.samples:
            samples.setdefault(sample.name, []).append((dict(sample.labels), sample.value))
    return text, samples


def _value(
    samples: dict[str, list[tuple[dict[str, str], float]]], name: str, **labels: str
) -> float:
    matches = [value for sample_labels, value in samples.get(name, []) if sample_labels == labels]
    assert len(matches) == 1, (name, labels, samples.get(name))
    return matches[0]


@pytest.fixture
def device_key(db: None) -> tuple[ScannerDevice, str]:
    return create_device("libreflip-01")


# --- scanner_api_requests_total -------------------------------------------------------


@pytest.mark.django_db
def test_requests_are_counted_by_route_name_and_status(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    client = Client()
    assert client.get(PING, headers=_bearer(key)).status_code == 200
    assert client.get(PING, headers=_bearer(key)).status_code == 200
    assert client.get(PING).status_code == 401
    assert client.get(PING, headers=_bearer("wrong-key")).status_code == 401
    assert client.post(PING, headers=_bearer(key)).status_code == 405

    _, samples = _scrape()

    assert _value(samples, "scanner_api_requests_total", endpoint="ping", status="200") == 2
    assert _value(samples, "scanner_api_requests_total", endpoint="ping", status="401") == 2
    assert _value(samples, "scanner_api_requests_total", endpoint="ping", status="405") == 1


@pytest.mark.django_db
def test_endpoint_label_is_the_route_name_never_the_path(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    client = Client()
    for job_id in ("20261117-aaaa", "20261117-bbbb"):
        response = client.get(f"/api/scanner/v1/jobs/{job_id}/pages/7", headers=_bearer(key))
        assert response.status_code == 404

    text, samples = _scrape()

    assert _value(samples, "scanner_api_requests_total", endpoint="not-found", status="404") == 2
    assert "20261117" not in text
    assert "jobs/" not in text
    assert key not in text


@pytest.mark.django_db
def test_disabled_api_answers_are_counted(device_key: tuple[ScannerDevice, str]) -> None:
    _, key = device_key
    with override_settings(BOOK_SCANNER_SCANNER_API_ENABLED=False):
        assert Client().get(PING, headers=_bearer(key)).status_code == 503

    _, samples = _scrape()

    assert _value(samples, "scanner_api_requests_total", endpoint="ping", status="503") == 1


@pytest.mark.django_db
def test_unhandled_view_error_is_counted_as_500_and_reraised(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key

    @scanner_api_view(["PUT"])
    def broken(request: Any, device: ScannerDevice) -> JsonResponse:
        raise RuntimeError("boom")

    request = RequestFactory().put(
        "/api/scanner/v1/jobs/x/pages/1",
        data=b"\x00" * 10,
        content_type="image/jpeg",
        headers=_bearer(key),
    )
    with pytest.raises(RuntimeError):
        broken(request)

    row = ScannerApiRequestCount.objects.get(endpoint="unknown", status=500)
    assert row.requests == 1
    assert row.bytes_received == 10


# --- scanner_bytes_received_total ------------------------------------------------------


@pytest.mark.django_db
def test_bytes_received_counts_bodies_of_authenticated_requests_only(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    client = Client()
    body = b"\xff\xd8" + b"x" * 1000
    # An upload-shaped request on a route that does not exist yet (later sprints add
    # it); the counting happens in the shared dispatch, so it is covered already.
    client.put(
        "/api/scanner/v1/jobs/20261117-aaaa/pages/1",
        data=body,
        content_type="image/jpeg",
        headers=_bearer(key),
    )
    client.post(PING, data=b"y" * 50, content_type="application/octet-stream", headers=_bearer(key))
    # Unauthenticated bodies are not scanner data and are not counted.
    client.put(
        "/api/scanner/v1/jobs/20261117-aaaa/pages/2",
        data=b"z" * 5000,
        content_type="image/jpeg",
    )

    _, samples = _scrape()

    assert _value(samples, "scanner_bytes_received_total") == len(body) + 50


@pytest.mark.django_db
def test_bytes_counter_is_zero_before_any_traffic() -> None:
    _, samples = _scrape()

    assert _value(samples, "scanner_bytes_received_total") == 0
    assert "scanner_api_requests_total" not in samples


def test_declared_body_bytes_ignores_missing_or_invalid_content_length() -> None:
    factory = RequestFactory()
    request = factory.get("/api/scanner/v1/ping")
    request.META.pop("CONTENT_LENGTH", None)
    assert scanner_metrics.declared_body_bytes(request) == 0
    request.META["CONTENT_LENGTH"] = "abc"
    assert scanner_metrics.declared_body_bytes(request) == 0
    request.META["CONTENT_LENGTH"] = "-5"
    assert scanner_metrics.declared_body_bytes(request) == 0
    request.META["CONTENT_LENGTH"] = "123"
    assert scanner_metrics.declared_body_bytes(request) == 123


@pytest.mark.django_db
def test_record_request_accumulates_in_one_row() -> None:
    record_request("ping", 200, 0)
    record_request("ping", 200, 7)
    record_request("ping", 401, 0)

    row = ScannerApiRequestCount.objects.get(endpoint="ping", status=200)
    assert (row.requests, row.bytes_received) == (2, 7)
    assert ScannerApiRequestCount.objects.count() == 2


@pytest.mark.django_db
def test_record_request_recovers_when_another_process_created_the_row() -> None:
    ScannerApiRequestCount.objects.create(endpoint="ping", status=200, requests=4)
    real_increment = scanner_metrics._increment
    calls: list[int] = []

    def first_update_misses(endpoint: str, status: int, body_bytes: int) -> bool:
        calls.append(1)
        if len(calls) == 1:
            return False  # the row did not exist yet when we looked
        return real_increment(endpoint, status, body_bytes)

    with mock.patch.object(scanner_metrics, "_increment", side_effect=first_update_misses):
        record_request("ping", 200, 3)

    row = ScannerApiRequestCount.objects.get(endpoint="ping", status=200)
    assert (row.requests, row.bytes_received) == (5, 3)


@pytest.mark.django_db
def test_metrics_failure_never_breaks_the_api_response(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    with mock.patch.object(
        scanner_metrics, "_increment", side_effect=DatabaseError("db unavailable")
    ):
        response = Client().get(PING, headers=_bearer(key))

    assert response.status_code == 200


# --- scanner_jobs{state} --------------------------------------------------------------


@pytest.mark.django_db
def test_scanner_jobs_counts_jobs_of_registered_devices_by_state(
    device_key: tuple[ScannerDevice, str],
) -> None:
    create_device("libreflip-02")
    revoke_device("libreflip-02")
    ScanJob.objects.create(scanner_id="libreflip-01", state=ScanJob.State.CAPTURING)
    ScanJob.objects.create(scanner_id="libreflip-01", state=ScanJob.State.CAPTURING)
    ScanJob.objects.create(scanner_id="libreflip-02", state=ScanJob.State.COMPLETE)
    # Web-created jobs and jobs naming an unregistered scanner are not device jobs.
    ScanJob.objects.create(state=ScanJob.State.CAPTURING)
    ScanJob.objects.create(scanner_id="phone-camera", state=ScanJob.State.OPEN)

    _, samples = _scrape()

    assert _value(samples, "scanner_jobs", state="capturing") == 2
    assert _value(samples, "scanner_jobs", state="complete") == 1
    assert _value(samples, "scanner_jobs", state="open") == 0
    assert {labels["state"] for labels, _ in samples["scanner_jobs"]} == set(ScanJob.State.values)


# --- scanner_last_seen_timestamp{scanner_id} ------------------------------------------


@pytest.mark.django_db
def test_last_seen_timestamp_lists_active_seen_devices_only(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    create_device("never-seen")
    revoked, _ = create_device("revoked-one")
    ScannerDevice.objects.filter(pk=revoked.pk).update(
        last_seen_at=datetime(2026, 11, 1, tzinfo=UTC)
    )
    revoke_device("revoked-one")
    assert Client().get(PING, headers=_bearer(key)).status_code == 200
    seen = ScannerDevice.objects.get(scanner_id="libreflip-01").last_seen_at
    assert seen is not None

    text, samples = _scrape()

    assert [labels for labels, _ in samples["scanner_last_seen_timestamp"]] == [
        {"scanner_id": "libreflip-01"}
    ]
    assert _value(
        samples, "scanner_last_seen_timestamp", scanner_id="libreflip-01"
    ) == pytest.approx(seen.timestamp())
    assert "never-seen" not in text
    assert "revoked-one" not in text
