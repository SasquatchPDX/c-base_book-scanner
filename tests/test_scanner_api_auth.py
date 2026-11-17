"""The scanner API key and the ``/api/scanner/v1/`` skeleton (#125, contract §3/§6.1/§6.2).

There is one scanner with one key; the server only knows the key's SHA-256
(``BOOK_SCANNER_SCANNER_API_KEY_SHA256``).
"""

from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import structlog
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import Client
from pydantic import SecretStr, ValidationError

from ebook_server.core.models import AppUserProfile, get_or_create_profile
from ebook_server.core.scanner_auth import generate_key, hash_key, is_scanner_key
from ebook_server.settings import Settings

PING = "/api/scanner/v1/ping"


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _assert_error(response: Any, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status
    assert response["Content-Type"] == "application/json"
    body = response.json()
    assert set(body) == {"error"}
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    return body


@pytest.fixture
def scanner_key(settings: Any) -> str:
    key = generate_key()
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = hash_key(key)
    return key


def _member_client(username: str = "member", *, disabled: bool = False) -> Client:
    profile = get_or_create_profile(username)
    client = Client()
    client.force_login(get_user_model().objects.get(pk=profile.user_id))
    if disabled:
        AppUserProfile.objects.filter(pk=profile.pk).update(is_disabled=True)
    return client


# --- the key ---------------------------------------------------------------------------


def test_key_is_random_urlsafe_and_matches_only_its_configured_sha256(settings: Any) -> None:
    key = generate_key()
    assert len(key) == 43  # 32 bytes, base64url without padding
    assert "=" not in key
    assert generate_key() != key
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = hashlib.sha256(key.encode()).hexdigest()
    assert is_scanner_key(key)
    assert not is_scanner_key(generate_key())


def test_unknown_and_malformed_keys_do_not_match(scanner_key: str) -> None:
    assert not is_scanner_key(None)
    assert not is_scanner_key("")
    assert not is_scanner_key("not-a-key")
    assert not is_scanner_key("x" * 1000)
    assert not is_scanner_key("bad key with spaces")
    assert not is_scanner_key(hash_key(scanner_key))  # the hash itself is not a key


def test_no_key_matches_while_the_api_is_disabled(settings: Any) -> None:
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = ""
    assert not is_scanner_key("")
    assert not is_scanner_key(generate_key())


# --- management command ----------------------------------------------------------------


def test_generate_scanner_key_prints_key_and_settings_line_and_stores_nothing(
    settings: Any, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = ""
    monkeypatch.chdir(tmp_path)
    out = io.StringIO()
    call_command("generate_scanner_key", stdout=out)
    lines = [line for line in out.getvalue().splitlines() if line.strip()]
    key = lines[1]
    assert len(key) == 43
    env_line = lines[-1]
    assert env_line == f"BOOK_SCANNER_SCANNER_API_KEY_SHA256={hash_key(key)}"
    assert out.getvalue().count(key) == 1
    # Nothing written: not the working directory, not the running settings.
    assert list(tmp_path.iterdir()) == []
    assert settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 == ""

    again = io.StringIO()
    call_command("generate_scanner_key", stdout=again)
    assert key not in again.getvalue()


# --- authentication on /api/scanner/v1/ ------------------------------------------------


def test_ping_with_valid_key(scanner_key: str, settings: Any) -> None:
    settings.BOOK_SCANNER_SCANNER_MAX_IMAGE_BYTES = 1234
    settings.BOOK_SCANNER_SCANNER_MAX_OPF_BYTES = 567
    before = datetime.now(UTC).replace(microsecond=0)

    response = Client().get(PING, headers=_bearer(scanner_key))

    assert response.status_code == 200
    assert response["Cache-Control"] == "no-store"
    body = response.json()
    assert set(body) == {"api_version", "server_time", "limits"}
    assert body["api_version"] == 1
    assert body["server_time"].endswith("Z")
    server_time = datetime.fromisoformat(body["server_time"].replace("Z", "+00:00"))
    assert before <= server_time <= datetime.now(UTC) + timedelta(seconds=1)
    assert body["limits"] == {
        "max_image_bytes": 1234,
        "max_opf_bytes": 567,
        "max_event_batch_bytes": 1048576,
        "max_event_batch_lines": 1000,
    }


def test_ping_accepts_trailing_slash_and_case_insensitive_scheme(scanner_key: str) -> None:
    response = Client().get(PING + "/", HTTP_AUTHORIZATION=f"bearer {scanner_key}")
    assert response.status_code == 200


def test_without_key_is_401(scanner_key: str) -> None:
    response = Client().get(PING)
    _assert_error(response, 401, "authentication_required")
    assert response["WWW-Authenticate"].startswith("Bearer")
    _assert_error(
        Client().get(PING, HTTP_AUTHORIZATION="Basic Zm9vOmJhcg=="), 401, "authentication_required"
    )


def test_wrong_key_is_401(scanner_key: str) -> None:
    _assert_error(Client().get(PING, headers=_bearer(scanner_key + "x")), 401, "invalid_api_key")
    _assert_error(Client().get(PING, headers=_bearer(generate_key())), 401, "invalid_api_key")


def test_old_key_is_401_after_the_setting_changes(scanner_key: str, settings: Any) -> None:
    assert Client().get(PING, headers=_bearer(scanner_key)).status_code == 200
    new_key = generate_key()
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = hash_key(new_key)  # new line + restart
    _assert_error(Client().get(PING, headers=_bearer(scanner_key)), 401, "invalid_api_key")
    assert Client().get(PING, headers=_bearer(new_key)).status_code == 200


def test_empty_setting_answers_503_for_every_request(settings: Any) -> None:
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = ""
    key = generate_key()
    _assert_error(Client().get(PING, headers=_bearer(key)), 503, "scanner_api_disabled")
    _assert_error(Client().get(PING), 503, "scanner_api_disabled")
    _assert_error(
        Client().put("/api/scanner/v1/jobs/20261117T143201Z", headers=_bearer(key)),
        503,
        "scanner_api_disabled",
    )
    _assert_error(Client().get("/api/scanner/v1/nope"), 503, "scanner_api_disabled")


@pytest.mark.django_db
def test_logged_in_browser_session_gets_401_on_scanner_api(scanner_key: str) -> None:
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
def test_session_cookie_does_not_rescue_an_invalid_key(scanner_key: str) -> None:
    client = _member_client()
    _assert_error(client.get(PING, headers=_bearer("forged-key")), 401, "invalid_api_key")


@pytest.mark.django_db
def test_valid_key_with_a_disabled_members_session_gets_the_scanner_api_response(
    scanner_key: str,
) -> None:
    """Mixed credentials: member-session state never decides a scanner API response."""
    client = _member_client("banned", disabled=True)

    response = client.get(PING, headers=_bearer(scanner_key))
    assert response.status_code == 200
    assert response.json()["api_version"] == 1

    _assert_error(
        client.get("/api/scanner/v1/nope", headers=_bearer(scanner_key)), 404, "not_found"
    )
    _assert_error(client.get(PING, headers=_bearer("forged-key")), 401, "invalid_api_key")
    _assert_error(client.get(PING), 401, "authentication_required")
    # Outside the namespace the ban still applies to the same session.
    assert client.get("/library/").status_code == 403


@pytest.mark.django_db
def test_disabled_members_session_still_gets_json_503_when_the_api_is_off(
    settings: Any,
) -> None:
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = ""
    client = _member_client("banned", disabled=True)
    _assert_error(client.get(PING), 503, "scanner_api_disabled")


@pytest.mark.django_db
def test_scanner_api_is_csrf_exempt(scanner_key: str) -> None:
    client = Client(enforce_csrf_checks=True)
    # No CSRF token and no cookie: the request reaches the API (404 envelope, not 403).
    _assert_error(
        client.post(
            "/api/scanner/v1/unknown",
            data=b"{}",
            content_type="application/json",
            headers=_bearer(scanner_key),
        ),
        404,
        "not_found",
    )


def test_unknown_route_and_wrong_method_use_the_error_envelope(scanner_key: str) -> None:
    _assert_error(
        Client().get("/api/scanner/v1/nope", headers=_bearer(scanner_key)), 404, "not_found"
    )
    _assert_error(Client().get("/api/scanner/v1/nope"), 401, "authentication_required")
    response = Client().post(PING, headers=_bearer(scanner_key))
    body = _assert_error(response, 405, "method_not_allowed")
    assert body["error"]["details"] == {"allowed": ["GET"]}
    assert response["Allow"] == "GET"


def test_every_request_logs_one_line_without_the_key(scanner_key: str) -> None:
    with structlog.testing.capture_logs() as logs:
        Client().get(PING, headers=_bearer(scanner_key))
        Client().get(PING, headers=_bearer("wrong-key"))
        Client().get("/api/scanner/v1/jobs/20261117T143201Z", headers=_bearer(scanner_key))
    lines = [entry for entry in logs if entry["event"] == "scanner_api_request"]
    assert [(line["route"], line["status"]) for line in lines] == [
        ("scanner-api-ping", 200),
        ("scanner-api-ping", 401),
        ("scanner-api-not-found", 404),
    ]
    assert all("job_id" in line for line in lines)
    rendered = repr(logs)
    assert scanner_key not in rendered
    assert hash_key(scanner_key) not in rendered
    assert "wrong-key" not in rendered


# --- the scanner key never authenticates anywhere else ---------------------------------


@pytest.mark.django_db
@pytest.mark.parametrize("path", ["/library/", "/reviews/", "/admin/", "/scan/jobs/"])
def test_valid_scanner_key_is_refused_outside_the_scanner_api(scanner_key: str, path: str) -> None:
    response = Client().get(path, headers=_bearer(scanner_key))
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "scanner_key_not_allowed"


@pytest.mark.django_db
def test_scanner_key_does_not_upgrade_a_member_session_elsewhere(scanner_key: str) -> None:
    client = _member_client()
    assert client.get("/library/").status_code == 200
    assert client.get("/library/", headers=_bearer(scanner_key)).status_code == 403


@pytest.mark.django_db
def test_other_bearer_tokens_are_unaffected(scanner_key: str, settings: Any) -> None:
    settings.BOOK_SCANNER_METRICS_BEARER_TOKEN = "metrics-token"
    response = Client().get("/metrics/", HTTP_AUTHORIZATION="Bearer metrics-token")
    assert response.status_code == 200


# --- settings --------------------------------------------------------------------------

KEY_HASH = hash_key("example-scanner-key")


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
        "scanner_api_key_sha256": KEY_HASH,
        "public_base_url": "https://serif.c-base.lan/",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_production_accepts_https_public_base_url_with_a_scanner_key() -> None:
    configured = _production()
    assert configured.public_base_url == "https://serif.c-base.lan/"
    assert configured.scanner_api_key_sha256 == KEY_HASH


def test_production_without_scanner_key_needs_no_public_base_url() -> None:
    """Regression (PDXDJN on #135): deployments without the scanner keep starting."""
    configured = _production(scanner_api_key_sha256="", public_base_url="")
    assert configured.scanner_api_key_sha256 == ""
    assert configured.public_base_url == ""


@pytest.mark.parametrize("url", ["", "   ", "http://serif.c-base.lan/", "https://", "serif.lan"])
def test_production_with_scanner_key_rejects_missing_or_insecure_public_base_url(
    url: str,
) -> None:
    with pytest.raises(ValidationError, match="public_base_url"):
        _production(public_base_url=url)


def test_production_rejects_an_http_public_base_url_even_without_a_scanner_key() -> None:
    with pytest.raises(ValidationError, match="public_base_url"):
        _production(scanner_api_key_sha256="", public_base_url="http://serif.c-base.lan/")


def test_scanner_settings_defaults() -> None:
    configured = Settings()
    assert configured.scanner_api_key_sha256 == ""
    assert configured.scanner_max_image_bytes == 52428800
    assert configured.scanner_max_opf_bytes == 1048576


def test_scanner_key_hash_is_normalised_and_validated() -> None:
    assert Settings(scanner_api_key_sha256=f"  {KEY_HASH.upper()} ").scanner_api_key_sha256 == (
        KEY_HASH
    )
    for bad in ("abc", "g" * 64, KEY_HASH + "0", "example-scanner-key"):
        with pytest.raises(ValidationError, match="scanner_api_key_sha256"):
            Settings(scanner_api_key_sha256=bad)


def test_scanner_settings_reject_bad_values() -> None:
    with pytest.raises(ValidationError, match="scanner_max_image_bytes"):
        Settings(scanner_max_image_bytes=0)
    with pytest.raises(ValidationError, match="scanner_max_opf_bytes"):
        Settings(scanner_max_opf_bytes=-1)
    with pytest.raises(ValidationError, match="public_base_url"):
        Settings(public_base_url="ftp://serif.c-base.lan/")
    assert Settings(public_base_url="http://localhost:8000/").public_base_url
