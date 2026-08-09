"""LDAP authentication adapter boundary."""

from __future__ import annotations

from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import BaseBackend
from django.contrib.auth.models import User

from .models import get_or_create_profile


@dataclass(frozen=True)
class LdapIdentity:
    username: str
    email: str | None = None


class LdapAdapter:
    """Adapter interface used by auth backends and tests."""

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        raise NotImplementedError


class StubLdapAdapter(LdapAdapter):
    """Deterministic local test adapter."""

    def authenticate(self, username: str, password: str) -> LdapIdentity:
        if not username:
            raise ValueError("username required")
        if not password:
            raise ValueError("password required")
        return LdapIdentity(username=username, email=f"{username}@example.invalid")


class LdapBackend(BaseBackend):
    """Django auth backend behind Sprint002 that wraps an LDAP adapter."""

    adapter: LdapAdapter = StubLdapAdapter()

    def __init__(self) -> None:
        if settings.BOOK_SCANNER_LDAP_MODE == "stub":
            self.adapter = StubLdapAdapter()
        else:
            # LDAP integration point: this project intentionally ships a stub adapter
            # for local/dev/test bootstrap. In production, mount a real adapter
            # through this hook.
            self.adapter = StubLdapAdapter()

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
        except ValueError:
            return None
        profile = get_or_create_profile(identity.username)
        if profile.is_disabled:
            return None
        return User.objects.get(username=identity.username)

    def get_user(self, user_id: int) -> User | None:
        try:
            return get_user_model().objects.get(pk=user_id)
        except get_user_model().DoesNotExist:
            return None
