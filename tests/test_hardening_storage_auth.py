from __future__ import annotations

import ssl
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from ebook_server.core import auth
from ebook_server.core.auth import Ldap3Adapter, LdapAuthenticationError, LdapBackend, LdapIdentity
from ebook_server.core.models import AppUserProfile, get_or_create_profile
from ebook_server.storage import (
    LocalFilesystemStorage,
    StorageUnavailableError,
    check_storage_root,
    probe_storage_writable,
)

# --- storage root guard -------------------------------------------------------------


def test_store_refuses_to_create_missing_storage_root(tmp_path: Path) -> None:
    mount = tmp_path / "not-mounted"
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4")
    storage = LocalFilesystemStorage(str(mount / "library-artifacts"))

    with override_settings(APP_DATA_DIR=mount, STORAGE_REQUIRE_EXISTING_ROOT=True):
        with pytest.raises(StorageUnavailableError, match="is the network share mounted"):
            storage.store(str(source), "books/1/book.pdf")

    assert not mount.exists()


def test_store_works_when_root_exists_and_sentinel_present(tmp_path: Path) -> None:
    mount = tmp_path / "mounted"
    mount.mkdir()
    source = tmp_path / "book.pdf"
    source.write_bytes(b"%PDF-1.4")
    storage = LocalFilesystemStorage(str(mount / "library-artifacts"))

    with override_settings(
        APP_DATA_DIR=mount,
        STORAGE_REQUIRE_EXISTING_ROOT=True,
        STORAGE_SENTINEL_FILE=".ebook-server-root",
    ):
        with pytest.raises(StorageUnavailableError, match="sentinel"):
            storage.store(str(source), "books/1/book.pdf")
        (mount / ".ebook-server-root").write_text("")
        storage.store(str(source), "books/1/book.pdf")

    assert (mount / "library-artifacts" / "books/1/book.pdf").read_bytes() == b"%PDF-1.4"


def test_check_storage_root_and_write_probe(tmp_path: Path) -> None:
    with pytest.raises(StorageUnavailableError):
        check_storage_root(root=tmp_path / "missing", require_existing=True, sentinel="")
    root = check_storage_root(root=tmp_path, require_existing=True, sentinel="")
    probe_storage_writable(root)
    assert list(tmp_path.iterdir()) == []  # probe file removed


def test_readiness_check_reports_down_for_missing_root(tmp_path: Path) -> None:
    from ebook_server.core.views import _check_storage

    with override_settings(APP_DATA_DIR=tmp_path / "gone", STORAGE_REQUIRE_EXISTING_ROOT=True):
        result = _check_storage()
    assert result.status == "down"
    assert not (tmp_path / "gone").exists()

    with override_settings(APP_DATA_DIR=tmp_path, STORAGE_REQUIRE_EXISTING_ROOT=True):
        assert _check_storage().status == "ok"


# --- LDAP TLS -----------------------------------------------------------------------


@override_settings(
    BOOK_SCANNER_LDAP_SERVER_URI="ldaps://ldap.example.test",
    BOOK_SCANNER_LDAP_TLS_REQUIRE_CERT=True,
    BOOK_SCANNER_LDAP_CA_FILE="",
)
def test_ldaps_validates_certificates_by_default() -> None:
    adapter = Ldap3Adapter()
    assert adapter.server.tls is not None
    assert adapter.server.tls.validate == ssl.CERT_REQUIRED


@override_settings(
    BOOK_SCANNER_LDAP_SERVER_URI="ldap://ldap.example.test",
    BOOK_SCANNER_LDAP_START_TLS=True,
    BOOK_SCANNER_LDAP_TLS_REQUIRE_CERT=True,
)
def test_start_tls_validates_certificates_by_default() -> None:
    assert Ldap3Adapter().server.tls.validate == ssl.CERT_REQUIRED


def test_ldap_ca_file_and_explicit_opt_out(tmp_path: Path) -> None:
    ca = tmp_path / "ca.pem"
    ca.write_text("not a real cert")
    with override_settings(
        BOOK_SCANNER_LDAP_SERVER_URI="ldaps://ldap.example.test",
        BOOK_SCANNER_LDAP_CA_FILE=str(ca),
    ):
        assert Ldap3Adapter().server.tls.ca_certs_file == str(ca)
    with override_settings(
        BOOK_SCANNER_LDAP_SERVER_URI="ldaps://ldap.example.test",
        BOOK_SCANNER_LDAP_CA_FILE="",
        BOOK_SCANNER_LDAP_TLS_REQUIRE_CERT=False,
    ):
        assert Ldap3Adapter().server.tls.validate == ssl.CERT_NONE
    with override_settings(
        BOOK_SCANNER_LDAP_SERVER_URI="ldaps://ldap.example.test",
        BOOK_SCANNER_LDAP_CA_FILE=str(tmp_path / "missing.pem"),
    ):
        with pytest.raises(LdapAuthenticationError):
            Ldap3Adapter()


@override_settings(BOOK_SCANNER_LDAP_MODE="ldap", BOOK_SCANNER_LDAP_SERVER_URI="ldaps://a.test")
def test_backend_reuses_adapter_across_requests() -> None:
    assert LdapBackend().adapter is LdapBackend().adapter


# --- LDAP username canonicalisation ---------------------------------------------------


class _FixedAdapter:
    def __init__(self, username: str) -> None:
        self.username = username

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        del username, password
        return LdapIdentity(username=self.username)


@pytest.mark.django_db(transaction=True)
def test_case_variants_resolve_to_one_account_and_respect_disabled_flag() -> None:
    profile = get_or_create_profile("alice")
    assert get_or_create_profile("Alice").pk == profile.pk
    assert get_user_model().objects.count() == 1

    backend = LdapBackend()
    backend.adapter = _FixedAdapter("Alice")  # type: ignore[assignment]
    assert backend.authenticate(None, username="Alice", password="x") == profile.user

    profile.is_disabled = True
    profile.save(update_fields=["is_disabled"])
    assert backend.authenticate(None, username="Alice", password="x") is None
    backend.adapter = _FixedAdapter("ALICE")  # type: ignore[assignment]
    assert backend.authenticate(None, username="ALICE", password="x") is None
    assert AppUserProfile.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_existing_mixed_case_account_keeps_working() -> None:
    existing = get_or_create_profile("MixedCase")
    backend = LdapBackend()
    backend.adapter = _FixedAdapter("mixedcase")  # type: ignore[assignment]
    user = backend.authenticate(None, username="MIXEDCASE", password="x")
    assert user is not None and user.pk == existing.user_id


def test_ldap3_adapter_prefers_directory_uid_and_falls_back_to_casefold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Entry:
        def __init__(self, attrs: dict[str, Any]) -> None:
            self.entry_attributes_as_dict = attrs

    class FakeConnection:
        def __init__(self, attrs: dict[str, Any]) -> None:
            self.entries = [Entry(attrs)]
            self._attrs = attrs

        def search(self, *args: object, **kwargs: object) -> bool:
            return True

        def unbind(self) -> None:
            return None

    with override_settings(BOOK_SCANNER_LDAP_SERVER_URI="ldaps://ldap.example.test"):
        adapter = Ldap3Adapter()
    monkeypatch.setattr(adapter, "_resolve_user_dn", lambda username: "uid=x,dc=test")

    monkeypatch.setattr(
        adapter,
        "_connection",
        lambda dn, pw: FakeConnection({"mail": ["a@example.test"], "uid": ["alice"]}),
    )
    identity = adapter.authenticate("ALICE", "pw")
    assert (identity.username, identity.email) == ("alice", "a@example.test")

    monkeypatch.setattr(adapter, "_connection", lambda dn, pw: FakeConnection({}))
    assert adapter.authenticate("Bob", "pw").username == "bob"


def test_auth_module_exposes_cached_factory() -> None:
    assert callable(auth.get_ldap3_adapter)


# --- promote_admin ---------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_promote_admin_creates_admin_profile_that_survives_ldap_login() -> None:
    out = StringIO()
    call_command("promote_admin", "Alice", "--superuser", stdout=out)

    profile = AppUserProfile.objects.get(ldap_uid="alice")
    assert profile.role == "admin"
    assert profile.user.is_staff and profile.user.is_superuser

    backend = LdapBackend()
    backend.adapter = _FixedAdapter("alice")  # type: ignore[assignment]
    user = backend.authenticate(None, username="Alice", password="pw")
    assert user is not None and user.pk == profile.user_id

    client = Client()
    client.force_login(user)
    assert client.get("/admin-only/").status_code == 200
    assert client.get("/admin/").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_promote_admin_updates_existing_account_and_validates_input() -> None:
    existing = get_or_create_profile("bob", is_disabled=True)
    call_command("promote_admin", "BOB", "--staff", stdout=StringIO())
    existing.refresh_from_db()
    existing.user.refresh_from_db()
    assert existing.role == "admin"
    assert not existing.is_disabled
    assert existing.user.is_staff and not existing.user.is_superuser
    assert AppUserProfile.objects.count() == 1
    with pytest.raises(CommandError):
        call_command("promote_admin", "  ", stdout=StringIO())
