"""Authentication adapter boundary: LDAP, or local passwords while LDAP is turned off."""

from __future__ import annotations

import ssl
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User
from ldap3 import BASE, NONE, Connection, Server, Tls
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from .models import canonical_ldap_uid, get_or_create_profile


@dataclass(frozen=True)
class LdapIdentity:
    username: str
    email: str | None = None


class LdapAdapter:
    """Adapter interface used by auth backends and tests."""

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        raise NotImplementedError


class LdapAuthenticationError(RuntimeError):
    """Raised when LDAP is unavailable, misconfigured, or rejects a bind."""


class StubLdapAdapter(LdapAdapter):
    """Deterministic local test adapter."""

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        if not username:
            raise ValueError("username required")
        if not password:
            raise ValueError("password required")
        return LdapIdentity(username=username, email=f"{username}@example.invalid")


class LocalPasswordAdapter(LdapAdapter):
    """LDAP turned off: authenticate against passwords stored in the local database.

    Accounts are created with ``manage.py set_local_password``. Unknown users and users
    without a usable password are rejected; there is no self-registration.
    """

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        uid = canonical_ldap_uid(username)
        if not uid or not password:
            raise LdapAuthenticationError("username and password are required")
        user = get_user_model().objects.filter(username__iexact=uid).order_by("pk").first()
        if user is None:
            # Run the hasher anyway so response time does not reveal which accounts exist.
            get_user_model()().set_password(password)
            raise LdapAuthenticationError("invalid credentials")
        if not user.has_usable_password() or not user.check_password(password):
            raise LdapAuthenticationError("invalid credentials")
        return LdapIdentity(username=user.username, email=user.email or None)


class Ldap3Adapter(LdapAdapter):
    """Fail-closed LDAP adapter using ldap3 binds and escaped directory searches."""

    def __init__(self) -> None:
        raw_uri = str(settings.BOOK_SCANNER_LDAP_SERVER_URI).strip()
        parsed = urlparse(raw_uri)
        if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
            raise LdapAuthenticationError("LDAP server URI is not configured")
        use_ssl = parsed.scheme == "ldaps"
        tls: Tls | None = None
        if use_ssl or settings.BOOK_SCANNER_LDAP_START_TLS:
            require_cert = bool(getattr(settings, "BOOK_SCANNER_LDAP_TLS_REQUIRE_CERT", True))
            ca_file = str(getattr(settings, "BOOK_SCANNER_LDAP_CA_FILE", "") or "").strip()
            try:
                tls = Tls(
                    validate=ssl.CERT_REQUIRED if require_cert else ssl.CERT_NONE,
                    ca_certs_file=ca_file or None,
                )
            except LDAPException as exc:
                raise LdapAuthenticationError("LDAP TLS configuration is invalid") from exc
        self.server = Server(
            parsed.hostname,
            port=parsed.port,
            use_ssl=use_ssl,
            tls=tls,
            connect_timeout=float(settings.BOOK_SCANNER_LDAP_CONNECT_TIMEOUT),
            get_info=NONE,
        )

    def _connection(self, bind_dn: str, password: str) -> Connection:
        connection = Connection(
            self.server,
            user=bind_dn or None,
            password=password or None,
            raise_exceptions=True,
        )
        try:
            connection.open()
            if settings.BOOK_SCANNER_LDAP_START_TLS:
                if self.server.ssl:
                    raise LdapAuthenticationError("StartTLS cannot be combined with LDAPS")
                connection.start_tls()
            if not connection.bind():
                raise LdapAuthenticationError("LDAP bind rejected")
        except (LDAPException, OSError) as exc:
            connection.unbind()
            raise LdapAuthenticationError("LDAP bind failed") from exc
        return connection

    def _resolve_user_dn(self, username: str) -> str:
        template = str(settings.BOOK_SCANNER_LDAP_USER_DN_TEMPLATE).strip()
        if template:
            return template.format(username=escape_rdn(username))

        search_base = str(settings.BOOK_SCANNER_LDAP_USER_SEARCH_BASE).strip()
        if not search_base:
            raise LdapAuthenticationError(
                "LDAP user DN template or search base must be configured",
            )
        search_filter = str(settings.BOOK_SCANNER_LDAP_USER_SEARCH_FILTER).format(
            username=escape_filter_chars(username),
        )
        service_connection = self._connection(
            str(settings.BOOK_SCANNER_LDAP_SERVICE_BIND_DN),
            str(settings.BOOK_SCANNER_LDAP_SERVICE_BIND_PASSWORD),
        )
        try:
            if not service_connection.search(
                search_base,
                search_filter,
                attributes=[],
                size_limit=2,
            ):
                raise LdapAuthenticationError("LDAP identity not found")
            entries = list(service_connection.entries)
            if len(entries) != 1:
                raise LdapAuthenticationError("LDAP identity is missing or ambiguous")
            return str(entries[0].entry_dn)
        except LDAPException as exc:
            raise LdapAuthenticationError("LDAP user search failed") from exc
        finally:
            service_connection.unbind()

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        normalized_username = username.strip()
        if not normalized_username or not password:
            raise LdapAuthenticationError("username and password are required")
        user_dn = self._resolve_user_dn(normalized_username)
        connection = self._connection(user_dn, password)
        email: str | None = None
        directory_uid: str | None = None
        try:
            email_attribute = str(settings.BOOK_SCANNER_LDAP_EMAIL_ATTRIBUTE).strip()
            uid_attribute = str(
                getattr(settings, "BOOK_SCANNER_LDAP_USERNAME_ATTRIBUTE", "uid") or ""
            ).strip()
            attributes = [name for name in (email_attribute, uid_attribute) if name]
            if attributes and connection.search(
                user_dn,
                "(objectClass=*)",
                search_scope=BASE,
                attributes=attributes,
            ):
                entry = connection.entries[0]
                attrs = entry.entry_attributes_as_dict
                email = _first_value(attrs.get(email_attribute)) if email_attribute else None
                directory_uid = _first_value(attrs.get(uid_attribute)) if uid_attribute else None
        except (LDAPException, IndexError):
            pass
        finally:
            connection.unbind()
        # Directories match uids case-insensitively; use the directory's own spelling
        # (or a casefolded login name) so "Alice" and "alice" are one account.
        username_out = (directory_uid or "").strip() or canonical_ldap_uid(normalized_username)
        return LdapIdentity(username=username_out, email=email)


def _first_value(value: object) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if value is None:
        return None
    text = str(value).strip()
    return text or None


_ADAPTER_SETTING_NAMES = (
    "BOOK_SCANNER_LDAP_SERVER_URI",
    "BOOK_SCANNER_LDAP_CONNECT_TIMEOUT",
    "BOOK_SCANNER_LDAP_START_TLS",
    "BOOK_SCANNER_LDAP_CA_FILE",
    "BOOK_SCANNER_LDAP_TLS_REQUIRE_CERT",
)


@lru_cache(maxsize=4)
def _cached_ldap3_adapter(config_key: tuple[object, ...]) -> Ldap3Adapter:
    del config_key  # only used to key the cache on the effective connection settings
    return Ldap3Adapter()


def get_ldap3_adapter() -> Ldap3Adapter:
    """Return a shared adapter so each login does not rebuild the Server/TLS objects."""
    key = tuple(getattr(settings, name, None) for name in _ADAPTER_SETTING_NAMES)
    return _cached_ldap3_adapter(key)


class LdapBackend(BaseBackend):
    """Django auth backend behind Sprint002 that wraps an LDAP adapter."""

    adapter: LdapAdapter = StubLdapAdapter()

    def __init__(self) -> None:
        mode = str(settings.BOOK_SCANNER_LDAP_MODE).strip().lower()
        if mode == "stub":
            self.adapter = StubLdapAdapter()
        elif mode == "ldap":
            self.adapter = get_ldap3_adapter()
        elif mode == "local":
            self.adapter = LocalPasswordAdapter()
        else:
            raise LdapAuthenticationError(f"unsupported LDAP mode: {mode}")

    def authenticate(
        self,
        request: object | None,
        username: str | None = None,
        password: str | None = None,
        **kwargs: object,
    ) -> User | None:
        if username is None or password is None:
            return None
        try:
            identity = self.adapter.authenticate(username=username, password=password)
        except (ValueError, LdapAuthenticationError):
            return None
        profile = get_or_create_profile(identity.username)
        if profile.is_disabled or not profile.user.is_active:
            return None
        if identity.email and profile.user.email != identity.email:
            profile.user.email = identity.email
            profile.user.save(update_fields=["email"])
        return profile.user

    def get_user(self, user_id: int) -> User | None:
        try:
            return get_user_model().objects.get(pk=user_id)
        except get_user_model().DoesNotExist:
            return None
