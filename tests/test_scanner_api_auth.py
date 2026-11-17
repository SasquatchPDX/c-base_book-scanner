"""Scanner device identity and the ``/api/scanner/v1/`` skeleton (#125, contract §3/§6.1/§6.2)."""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.management import CommandError, call_command
from django.test import Client
from pydantic import SecretStr, ValidationError

from ebook_server.core.models import ScannerDevice, get_or_create_profile
from ebook_server.core.scanner_devices import (
    authenticate_key,
    create_device,
    hash_key,
    revoke_device,
    rotate_key,
)
from ebook_server.settings import Settings

PING = "/api/scanner/v1/ping"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    return body


@pytest.fixture
def device_key(db: None) -> tuple[ScannerDevice, str]:
    return create_device("libreflip-01", name="libreflip at c-base")


# --- keys and the device record -------------------------------------------------------


@pytest.mark.django_db
def test_key_is_random_urlsafe_and_only_its_sha256_is_stored(
    device_key: tuple[ScannerDevice, str],
) -> None:
    device, key = device_key
    assert len(key) == 43  # 32 bytes, base64url without padding
    assert "=" not in key
    device.refresh_from_db()
    assert device.key_sha256 == hashlib.sha256(key.encode()).hexdigest()
    assert key not in device.key_sha256
    assert authenticate_key(key) == device


@pytest.mark.django_db
def test_two_devices_get_different_keys() -> None:
    _, first = create_device("scanner-a")
    _, second = create_device("scanner-b")
    assert first != second


@pytest.mark.django_db
def test_unknown_and_malformed_keys_do_not_authenticate(
    device_key: tuple[ScannerDevice, str],
) -> None:
    assert authenticate_key("") is None
    assert authenticate_key("not-a-key") is None
    assert authenticate_key("x" * 1000) is None
    assert authenticate_key("bad key with spaces") is None


# --- management command ---------------------------------------------------------------


@pytest.mark.django_db
def test_management_command_create_prints_key_once_and_list_never_shows_it() -> None:
    out = io.StringIO()
    call_command("scanner_device", "create", "libreflip-01", "--name", "Lab scanner", stdout=out)
    lines = out.getvalue().strip().splitlines()
    key = lines[-1]
    device = ScannerDevice.objects.get(scanner_id="libreflip-01")
    assert device.name == "Lab scanner"
    assert device.key_sha256 == hash_key(key)
    assert out.getvalue().count(key) == 1

    listing = io.StringIO()
    call_command("scanner_device", "list", stdout=listing)
    assert "libreflip-01" in listing.getvalue()
    assert "active" in listing.getvalue()
    assert key not in listing.getvalue()
    assert device.key_sha256 not in listing.getvalue()


@pytest.mark.django_db
def test_management_command_rotate_and_revoke() -> None:
    _, old_key = create_device("libreflip-01")
    out = io.StringIO()
    call_command("scanner_device", "rotate", "libreflip-01", stdout=out)
    new_key = out.getvalue().strip().splitlines()[-1]
    assert new_key != old_key
    assert authenticate_key(old_key) is None
    assert authenticate_key(new_key) is not None
    assert ScannerDevice.objects.get().rotated_at is not None

    call_command("scanner_device", "revoke", "libreflip-01", stdout=io.StringIO())
    assert authenticate_key(new_key) is None
    listing = io.StringIO()
    call_command("scanner_device", "list", stdout=listing)
    assert "revoked" in listing.getvalue()


@pytest.mark.django_db
def test_management_command_errors() -> None:
    create_device("libreflip-01")
    with pytest.raises(CommandError, match="already exists"):
        call_command("scanner_device", "create", "libreflip-01", stdout=io.StringIO())
    with pytest.raises(CommandError, match="unknown"):
        call_command("scanner_device", "rotate", "nope", stdout=io.StringIO())
    with pytest.raises(CommandError, match="unknown"):
        call_command("scanner_device", "revoke", "nope", stdout=io.StringIO())
    with pytest.raises(CommandError):
        call_command("scanner_device", "create", "not a slug!", stdout=io.StringIO())
    assert ScannerDevice.objects.count() == 1


@pytest.mark.django_db
def test_rotate_reactivates_a_revoked_device() -> None:
    create_device("libreflip-01")
    revoke_device("libreflip-01")
    _, key = rotate_key("libreflip-01")
    assert authenticate_key(key) is not None


# --- authentication on /api/scanner/v1/ ------------------------------------------------


@pytest.mark.django_db
def test_ping_with_valid_key(device_key: tuple[ScannerDevice, str], settings: Any) -> None:
    settings.BOOK_SCANNER_SCANNER_MAX_IMAGE_BYTES = 1234
    settings.BOOK_SCANNER_SCANNER_MAX_OPF_BYTES = 567
    device, key = device_key
    assert device.last_seen_at is None
    before = datetime.now(UTC).replace(microsecond=0)

    response = Client().get(PING, headers=_bearer(key))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert body["api_version"] == 1
    assert body["scanner_id"] == "libreflip-01"
    assert body["server_time"].endswith("Z")
    server_time = datetime.fromisoformat(body["server_time"].replace("Z", "+00:00"))
    assert before <= server_time <= datetime.now(UTC) + timedelta(seconds=1)
    assert body["limits"] == {
        "max_image_bytes": 1234,
        "max_opf_bytes": 567,
        "max_event_batch_bytes": 1048576,
        "max_event_batch_lines": 1000,
    }
    device.refresh_from_db()
    assert device.last_seen_at is not None


@pytest.mark.django_db
def test_ping_accepts_trailing_slash_and_case_insensitive_scheme(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    response = Client().get(PING + "/", HTTP_AUTHORIZATION=f"bearer {key}")
    assert response.status_code == 200


@pytest.mark.django_db
def test_without_key_is_401(device_key: tuple[ScannerDevice, str]) -> None:
    response = Client().get(PING)
    _assert_error(response, 401, "authentication_required")
    assert response["WWW-Authenticate"].startswith("Bearer")
    _assert_error(
        Client().get(PING, HTTP_AUTHORIZATION="Basic Zm9vOmJhcg=="), 401, "authentication_required"
    )


@pytest.mark.django_db
def test_wrong_revoked_and_rotated_keys_are_401(device_key: tuple[ScannerDevice, str]) -> None:
    device, key = device_key
    _assert_error(Client().get(PING, headers=_bearer(key + "x")), 401, "invalid_api_key")

    _, new_key = rotate_key(device.scanner_id)
    _assert_error(Client().get(PING, headers=_bearer(key)), 401, "invalid_api_key")
    assert Client().get(PING, headers=_bearer(new_key)).status_code == 200

    revoke_device(device.scanner_id)
    _assert_error(Client().get(PING, headers=_bearer(new_key)), 401, "invalid_api_key")


@pytest.mark.django_db
def test_failed_authentication_does_not_touch_last_seen(
    device_key: tuple[ScannerDevice, str],
) -> None:
    device, key = device_key
    revoke_device(device.scanner_id)
    Client().get(PING, headers=_bearer(key))
    device.refresh_from_db()
    assert device.last_seen_at is None


@pytest.mark.django_db
def test_logged_in_browser_session_gets_401_on_scanner_api() -> None:
    profile = get_or_create_profile("member")
    user = get_user_model().objects.get(pk=profile.user_id)
    user.is_staff = True
    user.is_superuser = True
    user.save()
    client = Client(enforce_csrf_checks=True)
    client.force_login(user)
    assert client.get("/library/").status_code == 200

    _assert_error(client.get(PING), 401, "authentication_required")
    _assert_error(client.get(PING, HTTP_ACCEPT="text/html"), 401, "authentication_required")
    _assert_error(client.put("/api/scanner/v1/jobs/x"), 401, "authentication_required")


@pytest.mark.django_db
def test_session_cookie_does_not_rescue_an_invalid_key() -> None:
    profile = get_or_create_profile("member")
    client = Client()
    client.force_login(get_user_model().objects.get(pk=profile.user_id))
    _assert_error(client.get(PING, headers=_bearer("forged-key")), 401, "invalid_api_key")


@pytest.mark.django_db
def test_scanner_api_is_csrf_exempt(device_key: tuple[ScannerDevice, str]) -> None:
    _, key = device_key
    client = Client(enforce_csrf_checks=True)
    # No CSRF token and no cookie: the request reaches the API (404 envelope, not 403).
    _assert_error(
        client.post(
            "/api/scanner/v1/unknown",
            data=b"{}",
            content_type="application/json",
            headers=_bearer(key),
        ),
        404,
        "not_found",
    )


@pytest.mark.django_db
def test_unknown_route_and_wrong_method_use_the_error_envelope(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    _assert_error(Client().get("/api/scanner/v1/nope", headers=_bearer(key)), 404, "not_found")
    _assert_error(Client().get("/api/scanner/v1/nope"), 401, "authentication_required")
    response = Client().post(PING, headers=_bearer(key))
    body = _assert_error(response, 405, "method_not_allowed")
    assert body["error"]["details"] == {"allowed": ["GET"]}
    assert response["Allow"] == "GET"


@pytest.mark.django_db
def test_disabled_api_answers_503(device_key: tuple[ScannerDevice, str], settings: Any) -> None:
    settings.BOOK_SCANNER_SCANNER_API_ENABLED = False
    _, key = device_key
    _assert_error(Client().get(PING, headers=_bearer(key)), 503, "scanner_api_disabled")
    _assert_error(Client().get(PING), 503, "scanner_api_disabled")


# --- device keys never authenticate anywhere else --------------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/library/", "/reviews/", "/admin/", "/scan/jobs/"])
def test_valid_device_key_is_refused_outside_the_scanner_api(
    device_key: tuple[ScannerDevice, str], path: str
) -> None:
    _, key = device_key
    response = Client().get(path, headers=_bearer(key))
    assert response.status_code in {401, 403}
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "device_key_not_allowed"


@pytest.mark.django_db
def test_revoked_device_key_is_also_refused_outside_the_scanner_api(
    device_key: tuple[ScannerDevice, str],
) -> None:
    device, key = device_key
    revoke_device(device.scanner_id)
    assert Client().get("/library/", headers=_bearer(key)).status_code == 403


@pytest.mark.django_db
def test_device_key_does_not_upgrade_a_member_session_elsewhere(
    device_key: tuple[ScannerDevice, str],
) -> None:
    _, key = device_key
    profile = get_or_create_profile("member")
    client = Client()
    client.force_login(get_user_model().objects.get(pk=profile.user_id))
    assert client.get("/library/").status_code == 200
    assert client.get("/library/", headers=_bearer(key)).status_code == 403


@pytest.mark.django_db
def test_other_bearer_tokens_are_unaffected(settings: Any) -> None:
    settings.BOOK_SCANNER_METRICS_BEARER_TOKEN = "metrics-token"
    response = Client().get("/metrics/", HTTP_AUTHORIZATION="Bearer metrics-token")
    assert response.status_code == 200


# --- admin -----------------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_lists_devices_read_only_and_can_revoke(
    device_key: tuple[ScannerDevice, str],
) -> None:
    device, key = device_key
    user = get_user_model().objects.create_superuser("root", "root@example.test", "pw")
    client = Client()
    client.force_login(user)

    listing = client.get("/admin/core/scannerdevice/")
    assert listing.status_code == 200
    assert b"libreflip-01" in listing.content
    assert device.key_sha256.encode() not in listing.content
    assert client.get("/admin/core/scannerdevice/add/").status_code == 403
    detail = client.get(f"/admin/core/scannerdevice/{device.pk}/change/")
    assert detail.status_code == 200
    assert device.key_sha256.encode() not in detail.content
    client.post(
        f"/admin/core/scannerdevice/{device.pk}/change/", {"name": "renamed", "is_active": ""}
    )
    device.refresh_from_db()
    assert device.name == "libreflip at c-base"

    response = client.post(
        "/admin/core/scannerdevice/",
        {"action": "revoke_devices", "_selected_action": [str(device.pk)]},
    )
    assert response.status_code == 302
    device.refresh_from_db()
    assert not device.is_active
    assert authenticate_key(key) is None


# --- settings --------------------------------------------------------------------------


def _production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": SecretStr(
            "production-secret-key-with-more-than-fifty-random-looking-characters-123",
        ),
        "allowed_hosts": ["library.example.test"],
        "database_url": "postgresql://ebook:secret@database/library",
        "redis_url": "redis://redis:6379/0",
        "ldap_mode": "local",
        "secure_ssl_redirect": True,
        "session_cookie_secure": True,
        "csrf_cookie_secure": True,
        "secure_hsts_seconds": 31536000,
        "metrics_bearer_token": SecretStr("metrics-monitoring-token"),
        "public_base_url": "https://serif.c-base.lan/",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_production_accepts_https_public_base_url() -> None:
    assert _production().public_base_url == "https://serif.c-base.lan/"


@pytest.mark.parametrize("url", ["", "   ", "http://serif.c-base.lan/", "https://", "serif.lan"])
def test_production_rejects_missing_or_insecure_public_base_url(url: str) -> None:
    with pytest.raises(ValidationError, match="public_base_url"):
        _production(public_base_url=url)


def test_scanner_settings_defaults() -> None:
    configured = Settings()
    assert configured.scanner_api_enabled is True
    assert configured.scanner_max_image_bytes == 52428800
    assert configured.scanner_max_opf_bytes == 1048576


def test_scanner_settings_reject_bad_values() -> None:
    with pytest.raises(ValidationError, match="scanner_max_image_bytes"):
        Settings(scanner_max_image_bytes=0)
    with pytest.raises(ValidationError, match="scanner_max_opf_bytes"):
        Settings(scanner_max_opf_bytes=-1)
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(public_base_url="ftp://serif.c-base.lan/")
    assert Settings(public_base_url="http://localhost:8000/").public_base_url
