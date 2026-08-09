from __future__ import annotations

import pytest
from django.contrib.auth import authenticate
from django.test import Client

from ebook_server.core.auth import LdapBackend, LdapIdentity, StubLdapAdapter
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
