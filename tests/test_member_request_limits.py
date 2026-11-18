from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.http import HttpResponseBase
from django.test import Client, override_settings
from django.utils import timezone

from ebook_server.core import request_limits
from ebook_server.core.models import (
    ImportJob,
    LoginAttemptWindow,
    RequestLimitCount,
    UploadReservation,
    get_or_create_profile,
)


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_LOGIN_ACCOUNT_BURST=2, BOOK_SCANNER_LOGIN_ADDRESS_BURST=5)
def test_login_backoff_precedes_ldap_and_is_shared_between_clients() -> None:
    first, second = Client(), Client()
    now = timezone.now()
    with patch("ebook_server.core.views.authenticate", return_value=None) as ldap:

        def submit(client: Client, name: str, address: str = "192.0.2.10") -> HttpResponseBase:
            return client.post(
                "/accounts/login/", {"username": name, "password": "fixture"}, REMOTE_ADDR=address
            )

        with patch.object(timezone, "now", return_value=now):
            assert submit(first, "Reader").status_code == 401
            assert submit(second, " reader ").status_code == 401
            blocked = submit(second, "READER", "192.0.2.20")
            assert blocked.status_code == 429 and blocked["Retry-After"] == "1"
            assert ldap.call_count == 2
            assert submit(second, "unrelated").status_code == 401
        with patch.object(timezone, "now", return_value=now + timedelta(seconds=1)):
            assert submit(first, "reader").status_code == 401
            assert submit(second, "reader")["Retry-After"] == "2"
        with patch.object(timezone, "now", return_value=now + timedelta(minutes=16)):
            assert submit(first, "reader").status_code == 401
    assert RequestLimitCount.objects.get(pk="login").total == 2
    assert all(len(key) == 64 for key in LoginAttemptWindow.objects.values_list("key", flat=True))


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_LOGIN_ACCOUNT_BURST=1, BOOK_SCANNER_LOGIN_ADDRESS_BURST=1)
def test_success_refunds_both_budgets_and_address_cap_applies_to_other_names() -> None:
    ticket = request_limits.reserve_login("known", "peer")
    request_limits.release_successful_login(ticket)
    request_limits.reserve_login("known", "peer")
    with pytest.raises(request_limits.RequestLimited):
        request_limits.reserve_login("new", "peer")
    request_limits.reserve_login("new", "another peer")


@pytest.mark.django_db
def test_upload_admission_precedes_staging_and_limits_survive_completion(tmp_path: Path) -> None:
    first, second, other = Client(), Client(), Client()
    user = get_or_create_profile("reader").user
    first.force_login(user)
    second.force_login(user)
    other.force_login(get_or_create_profile("another-reader").user)

    def upload(client: Client) -> HttpResponseBase:
        return client.post("/imports/upload/", {"file": SimpleUploadedFile("story.txt", b"book")})

    with (
        override_settings(
            APP_DATA_DIR=tmp_path,
            BOOK_SCANNER_MEMBER_UPLOADS_PENDING=1,
            BOOK_SCANNER_MEMBER_UPLOAD_BYTES_PER_HOUR=7,
        ),
        patch("ebook_server.core.intake_views.enqueue"),
    ):
        assert upload(first).status_code == 201
        staged = list((tmp_path / "uploads").iterdir())
        assert len(staged) == 1
        assert upload(second)["Retry-After"] == "30"
        assert list((tmp_path / "uploads").iterdir()) == staged
        job = ImportJob.objects.get()
        job.state = ImportJob.State.COMPLETE
        job.save(update_fields=["state"])
        assert upload(second)["Retry-After"] == "3600"
        with override_settings(
            BOOK_SCANNER_MEMBER_UPLOAD_BYTES_PER_HOUR=100, BOOK_SCANNER_MEMBER_UPLOADS_PER_HOUR=1
        ):
            assert upload(second).status_code == 429
        assert upload(other).status_code == 201
        assert ImportJob.objects.count() == 2
        assert UploadReservation.objects.count() == 0
        assert RequestLimitCount.objects.get(pk="upload").total == 3


@pytest.mark.django_db
def test_expired_staging_lease_leaves_no_job_or_file(tmp_path: Path) -> None:
    client = Client()
    client.force_login(get_or_create_profile("slow-upload").user)
    with (
        override_settings(APP_DATA_DIR=tmp_path),
        patch.object(request_limits, "UPLOAD_LEASE", timedelta(0)),
    ):
        response = client.post(
            "/imports/upload/", {"file": SimpleUploadedFile("story.txt", b"book")}
        )
    assert response.status_code == 429
    assert not ImportJob.objects.exists()
    assert not UploadReservation.objects.exists()
    assert not list((tmp_path / "uploads").iterdir())
