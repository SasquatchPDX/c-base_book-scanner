"""Local-password authentication used while LDAP is turned off (ldap_mode=local)."""

from __future__ import annotations

import io
from unittest.mock import patch

import pytest
from django.contrib.auth import authenticate, get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings
from pydantic import SecretStr, ValidationError

from ebook_server.core.models import AppUserProfile, UserRole
from ebook_server.settings import Settings

PASSWORD = "correct horse battery"


def _set_password(username: str, password: str = PASSWORD, *extra: str) -> None:
    with patch("sys.stdin", io.StringIO(password + "\n")):
        call_command(
            "set_local_password", username, "--password-stdin", *extra, stdout=io.StringIO()
        )


def _production(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "environment": "production",
        "secret_key": SecretStr(
            "production-secret-key-with-more-than-fifty-random-looking-characters-123",
        ),
        "allowed_hosts": ["library.example.test"],
        "database_url": "postgresql://ebook:secret@database/library",
        "redis_url": "redis://redis:6379/0",
        "secure_ssl_redirect": True,
        "session_cookie_secure": True,
        "csrf_cookie_secure": True,
        "secure_hsts_seconds": 31536000,
        "metrics_bearer_token": SecretStr("metrics-monitoring-token"),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def test_production_accepts_local_mode_without_any_ldap_settings() -> None:
    assert _production(ldap_mode="local").ldap_mode == "local"


def test_production_still_rejects_stub_mode() -> None:
    with pytest.raises(ValidationError, match="ldap_mode must be 'ldap' or 'local'"):
        _production(ldap_mode="stub")


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_LDAP_MODE="local")
def test_local_mode_authenticates_only_with_the_stored_password() -> None:
    _set_password("Scanner")

    user = authenticate(username="scanner", password=PASSWORD)
    assert user is not None
    assert user.username == "scanner"
    # Login names are case-insensitive, like the LDAP mode.
    assert authenticate(username="SCANNER", password=PASSWORD) == user
    assert authenticate(username="scanner", password="wrong password") is None
    assert authenticate(username="nobody", password=PASSWORD) is None
    assert authenticate(username="scanner", password="") is None


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_LDAP_MODE="local")
def test_local_mode_rejects_accounts_without_a_password_and_disabled_accounts() -> None:
    get_user_model().objects.create(username="nopassword")
    assert authenticate(username="nopassword", password=PASSWORD) is None

    _set_password("member")
    AppUserProfile.objects.filter(ldap_uid="member").update(is_disabled=True)
    assert authenticate(username="member", password=PASSWORD) is None


@pytest.mark.django_db
@override_settings(BOOK_SCANNER_LDAP_MODE="local")
def test_local_mode_login_page_and_scanner_api_session() -> None:
    _set_password("scanner")
    client = Client()
    response = client.post(
        "/accounts/login/", {"username": "scanner", "password": "wrong password"}
    )
    assert response.status_code == 401
    response = client.post("/accounts/login/", {"username": "scanner", "password": PASSWORD})
    assert response.status_code == 302
    assert client.get("/scan/jobs/", HTTP_ACCEPT="application/json").status_code == 200


@pytest.mark.django_db
def test_set_local_password_admin_flag_and_validation() -> None:
    _set_password("boss", PASSWORD, "--admin")
    profile = AppUserProfile.objects.select_related("user").get(ldap_uid="boss")
    assert profile.role == UserRole.ADMIN
    assert profile.user.is_staff and profile.user.is_superuser

    with pytest.raises(CommandError, match="at least 8"):
        _set_password("shorty", "short")

    # Resetting re-enables a disabled account and replaces the password.
    profile.is_disabled = True
    profile.save(update_fields=["is_disabled"])
    _set_password("boss", "a different password")
    profile.refresh_from_db()
    profile.user.refresh_from_db()
    assert not profile.is_disabled
    assert profile.user.check_password("a different password")
