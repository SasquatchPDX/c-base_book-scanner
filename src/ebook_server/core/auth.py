"""LDAP authentication adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User
from ldap3 import BASE, NONE, Connection, Server
from ldap3.core.exceptions import LDAPException
from ldap3.utils.conv import escape_filter_chars
from ldap3.utils.dn import escape_rdn

from .models import get_or_create_profile


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


class Ldap3Adapter(LdapAdapter):
    """Fail-closed LDAP adapter using ldap3 binds and escaped directory searches."""

    def __init__(self) -> None:
        raw_uri = str(settings.BOOK_SCANNER_LDAP_SERVER_URI).strip()
        parsed = urlparse(raw_uri)
        if parsed.scheme not in {"ldap", "ldaps"} or not parsed.hostname:
            raise LdapAuthenticationError("LDAP server URI is not configured")
        self.server = Server(
            parsed.hostname,
            port=parsed.port,
            use_ssl=parsed.scheme == "ldaps",
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
        try:
            email_attribute = str(settings.BOOK_SCANNER_LDAP_EMAIL_ATTRIBUTE).strip()
            if email_attribute and connection.search(
                user_dn,
                "(objectClass=*)",
                search_scope=BASE,
                attributes=[email_attribute],
            ):
                entry = connection.entries[0]
                value = entry.entry_attributes_as_dict.get(email_attribute)
                if isinstance(value, list) and value:
                    email = str(value[0])
                elif isinstance(value, str):
                    email = value
        except (LDAPException, IndexError):
            email = None
        finally:
            connection.unbind()
        return LdapIdentity(username=normalized_username, email=email)


class LdapBackend(BaseBackend):
    """Django auth backend behind Sprint002 that wraps an LDAP adapter."""

    adapter: LdapAdapter = StubLdapAdapter()

    def __init__(self) -> None:
        mode = str(settings.BOOK_SCANNER_LDAP_MODE).strip().lower()
        if mode == "stub":
            self.adapter = StubLdapAdapter()
        elif mode == "ldap":
            self.adapter = Ldap3Adapter()
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
        if profile.is_disabled:
            return None
        if identity.email and profile.user.email != identity.email:
            profile.user.email = identity.email
            profile.user.save(update_fields=["email"])
        return User.objects.get(username=identity.username)

    def get_user(self, user_id: int) -> User | None:
        try:
            return get_user_model().objects.get(pk=user_id)
        except get_user_model().DoesNotExist:
            return None
