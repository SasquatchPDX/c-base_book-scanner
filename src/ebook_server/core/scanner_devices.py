"""Scanner device API keys: issue, rotate, revoke and verify (Sprint020 H, contract §3).

A key is 32 random bytes, base64url-encoded without padding (``secrets.token_urlsafe``).
The database keeps only its SHA-256 as lowercase hex. A device is looked up by that hash
and the stored hash is compared with ``secrets.compare_digest``.
"""

from __future__ import annotations

import hashlib
import re
import secrets
from typing import Final

from django.db import transaction
from django.utils import timezone

from .models import ScannerDevice

KEY_BYTES: Final = 32
# token_urlsafe(32) yields 43 characters; anything far longer is not one of our keys
# and is rejected before hashing.
MAX_KEY_LENGTH: Final = 256
_KEY_PATTERN: Final = re.compile(r"^[A-Za-z0-9_-]+$")


class ScannerDeviceError(Exception):
    """A management operation on a scanner device could not be carried out."""


def generate_key() -> str:
    return secrets.token_urlsafe(KEY_BYTES)


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("ascii")).hexdigest()


def create_device(scanner_id: str, *, name: str = "") -> tuple[ScannerDevice, str]:
    """Create a device and return it with its plaintext key (shown to the operator once)."""
    key = generate_key()
    with transaction.atomic():
        if ScannerDevice.objects.filter(scanner_id=scanner_id).exists():
            raise ScannerDeviceError(f"scanner device {scanner_id!r} already exists")
        device = ScannerDevice(scanner_id=scanner_id, name=name, key_sha256=hash_key(key))
        device.full_clean()
        device.save()
    return device, key


def rotate_key(scanner_id: str) -> tuple[ScannerDevice, str]:
    """Replace a device's key; the old key stops working immediately.

    Rotating a revoked device re-activates it with the fresh key.
    """
    key = generate_key()
    with transaction.atomic():
        device = ScannerDevice.objects.select_for_update().filter(scanner_id=scanner_id).first()
        if device is None:
            raise ScannerDeviceError(f"unknown scanner device {scanner_id!r}")
        device.key_sha256 = hash_key(key)
        device.is_active = True
        device.rotated_at = timezone.now()
        device.save(update_fields=["key_sha256", "is_active", "rotated_at"])
    return device, key


def revoke_device(scanner_id: str) -> ScannerDevice:
    with transaction.atomic():
        device = ScannerDevice.objects.select_for_update().filter(scanner_id=scanner_id).first()
        if device is None:
            raise ScannerDeviceError(f"unknown scanner device {scanner_id!r}")
        device.is_active = False
        device.save(update_fields=["is_active"])
    return device


def device_for_key(key: str) -> ScannerDevice | None:
    """Return the device owning ``key`` (active or revoked), or ``None``."""
    if not key or len(key) > MAX_KEY_LENGTH or not _KEY_PATTERN.match(key):
        return None
    digest = hash_key(key)
    device = ScannerDevice.objects.filter(key_sha256=digest).first()
    if device is None or not secrets.compare_digest(device.key_sha256, digest):
        return None
    return device


def authenticate_key(key: str) -> ScannerDevice | None:
    """Return the active device owning ``key``, or ``None``."""
    device = device_for_key(key)
    if device is None or not device.is_active:
        return None
    return device


def bearer_token(authorization: str) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header value, else ``None``."""
    scheme, _, token = authorization.strip().partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
