"""Scan job identifiers (#126, libreflip contract §4.1, decision D1).

A ``ScanJob`` primary key is an opaque, immutable string of one of two forms:

* a job created by the libreflip scanner uses the scanner-assigned ID
  ``YYYYMMDDTHHMMSSZ[-N]`` (for example ``20261117T143201Z`` or ``20261117T143201Z-2``);
* a job created in the web UI gets a server-generated UUID in its canonical string form
  (lowercase, hyphenated).
"""

from __future__ import annotations

import re
import uuid
from typing import Final

DEVICE_JOB_ID_PATTERN: Final = r"[0-9]{8}T[0-9]{6}Z(?:-[1-9][0-9]{0,3})?"
UUID_PATTERN: Final = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
_DEVICE_JOB_ID_RE: Final = re.compile(DEVICE_JOB_ID_PATTERN)
MAX_SCAN_JOB_ID_LENGTH: Final = 64


def new_scan_job_id() -> str:
    """Primary key for a web-created scan job: a canonical UUID string."""
    return str(uuid.uuid4())


def is_device_job_id(value: str) -> bool:
    """True when ``value`` is a scanner-assigned job ID (§4.1)."""
    return _DEVICE_JOB_ID_RE.fullmatch(value) is not None


def normalize_scan_job_id(value: object) -> str | None:
    """Return the canonical form of a scan job ID, or ``None`` when it is not one.

    Scanner IDs are returned unchanged. Anything ``uuid.UUID`` accepts (uppercase, no
    hyphens, braces) becomes the canonical lowercase hyphenated form that is stored.
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text or len(text) > MAX_SCAN_JOB_ID_LENGTH:
        return None
    if is_device_job_id(text):
        return text
    try:
        return str(uuid.UUID(text))
    except (TypeError, ValueError, AttributeError):
        return None


class ScanJobIdConverter:
    """URL path converter ``<scan_job_id:job_id>``: a canonical UUID or a scanner ID."""

    regex = f"(?:{UUID_PATTERN}|{DEVICE_JOB_ID_PATTERN})"

    def to_python(self, value: str) -> str:
        return value

    def to_url(self, value: object) -> str:
        return str(value)
