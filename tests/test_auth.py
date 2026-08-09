from __future__ import annotations

import pytest
from django.contrib.auth import authenticate
from django.test import Client, override_settings

from ebook_server.core.auth import (
    Ldap3Adapter,
    LdapAuthenticationError,
    LdapBackend,
    LdapIdentity,
    StubLdapAdapter,
)
from ebook_server.core.models import AppUserProfile, get_or_create_profile


@pytest.mark.django_db(transaction=True)
def test_profile_created_for_authenticated_ldap_identity() -> None:
    profile = get_or_create_profile("alice")
    assert profile.ldap_uid == "alice"
    assert profile.role == "normal"
    assert profile.user.username == "alice"


@pytest.mark.django_db(transaction=True)
def test_disabled_profile_blocks_backend_auth() -> None:
    profile = get_or_create_profile("banned-user", is_disabled=True)
    backend = LdapBackend()
    backend.adapter = StubLdapAdapter()
    assert backend.authenticate(None, username="banned-user", password="x") is None
    assert profile.is_disabled


def test_stub_adapter_returns_identity() -> None:
    identity = StubLdapAdapter().authenticate("librarian", "secret")
    assert isinstance(identity, LdapIdentity)
    assert identity.username == "librarian"


@pytest.mark.django_db(transaction=True)
def test_django_auth_backends_smoke() -> None:
    user = authenticate(request=None, username="admin-user", password="pw")
    assert user is not None
    assert user.username == "admin-user"


def test_stub_adapter_rejects_empty_credentials() -> None:
    with pytest.raises(ValueError):
        StubLdapAdapter().authenticate("user", "")


@pytest.mark.django_db(transaction=True)
def test_login_backed_profile_has_no_registration_path() -> None:
    assert not AppUserProfile.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_banned_user_profile_denies_library_access() -> None:
    profile = get_or_create_profile("blocked-member", is_disabled=True)
    client = Client()
    client.force_login(profile.user)
    response = client.get("/library/")
    assert response.status_code == 403


@pytest.mark.django_db(transaction=True)
def test_role_enforcement_for_admin_endpoint() -> None:
    normal_user_profile = get_or_create_profile("normal-member")
    normal_client = Client()
    normal_client.force_login(normal_user_profile.user)

    admin_response = normal_client.get("/admin-only/")
    assert admin_response.status_code == 403

    admin_profile = get_or_create_profile("admin-member", role="admin")
    admin_client = Client()
    admin_client.force_login(admin_profile.user)

    admin_response_ok = admin_client.get("/admin-only/")
    assert admin_response_ok.status_code == 200


@override_settings(BOOK_SCANNER_LDAP_MODE="unsupported")
def test_unknown_ldap_mode_fails_closed() -> None:
    with pytest.raises(LdapAuthenticationError, match="unsupported LDAP mode"):
        LdapBackend()


@override_settings(
    BOOK_SCANNER_LDAP_SERVER_URI="ldaps://ldap.example.test:636",
    BOOK_SCANNER_LDAP_USER_DN_TEMPLATE="uid={username},ou=people,dc=example,dc=test",
    BOOK_SCANNER_LDAP_CONNECT_TIMEOUT=3,
    BOOK_SCANNER_LDAP_START_TLS=False,
    BOOK_SCANNER_LDAP_EMAIL_ATTRIBUTE="mail",
)
def test_real_ldap_adapter_binds_with_escaped_user_dn(monkeypatch: pytest.MonkeyPatch) -> None:
    events: dict[str, object] = {}

    class FakeEntry:
        entry_attributes_as_dict = {"mail": ["member@example.test"]}

    class FakeConnection:
        entries = [FakeEntry()]

        def __init__(self, server: object, *, user: str, password: str, **kwargs: object) -> None:
            events["user"] = user
            events["password"] = password

        def open(self) -> None:
            events["opened"] = True

        def bind(self) -> bool:
            return True

        def search(self, *args: object, **kwargs: object) -> bool:
            return True

        def unbind(self) -> None:
            events["unbound"] = True

    monkeypatch.setattr("ebook_server.core.auth.Connection", FakeConnection)
    identity = Ldap3Adapter().authenticate("member,admin", "correct horse")

    assert events["user"] == "uid=member\\,admin,ou=people,dc=example,dc=test"
    assert identity.email == "member@example.test"
    assert events["unbound"] is True
