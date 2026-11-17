"""The scanner API key: generate, hash and verify (#125, contract §3/§7.2).

There is exactly one scanner, so there is exactly one key and no device registry. The key
is 32 random bytes, base64url-encoded without padding (``secrets.token_urlsafe``). serif
never stores it: the setting ``BOOK_SCANNER_SCANNER_API_KEY_SHA256`` holds its SHA-256 as
lowercase hex, and a presented key is checked by hashing it and comparing the digests with
``secrets.compare_digest``. A high-entropy key needs no slow hash.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final

from django.conf import settings

KEY_BYTES: Final = 32
# token_urlsafe(32) yields 43 characters; anything far longer is not our key and is
# rejected before hashing.
MAX_KEY_LENGTH: Final = 256
_KEY_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]+$")
SETTING_NAME: Final = "BOOK_SCANNER_SCANNER_API_KEY_SHA256"


def generate_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def configured_key_hash() -> str:
    """The configured key hash, or ``""`` when the scanner API is disabled."""
    return str(getattr(settings, SETTING_NAME, "") or "")


def scanner_api_enabled() -> bool:
    return bool(configured_key_hash())


def is_scanner_key(key: str | None) -> bool:
    """Whether ``key`` is the configured scanner key (always ``False`` when disabled)."""
    expected = configured_key_hash()
    if not expected or not key or len(key) > MAX_KEY_LENGTH or not _KEY_PATTERN.match(key):
        return False
    return secrets.compare_digest(hash_key(key), expected)


def bearer_token(authorization: str) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header value, else ``None``."""
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
