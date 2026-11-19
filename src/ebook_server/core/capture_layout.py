"""Legacy ``capture_layout`` input on scan-job creation (#55, removed in #195).

The scanner hardware delivers upright single pages, so ScanTailor-Web has no Page
Split stage and a scan job no longer stores a capture layout. The field stays
*accepted* on job creation so existing clients (the libreflip scanner registering
over ``/api/scanner/v1/``, scripts posting to ``/scan/jobs/``) keep working:

- absent, empty or ``single_page`` (and its old aliases): accepted, nothing stored;
- ``spread`` or anything else: rejected, because two-page images can no longer be
  processed.
"""

from __future__ import annotations

from typing import Final

SINGLE_PAGE: Final[str] = "single_page"

_SINGLE_PAGE_ALIASES: Final[frozenset[str]] = frozenset({"single_page", "single-page", "single"})

UNSUPPORTED_MESSAGE: Final[str] = (
    "capture_layout must be single_page: two-page spreads are not supported "
    "(ScanTailor-Web has no Page Split stage)"
)


def is_accepted(raw: object) -> bool:
    """True when a legacy ``capture_layout`` value may be accepted (and ignored)."""
    if raw is None:
        return True
    if not isinstance(raw, str):
        return False
    value = raw.strip().lower()
    return not value or value in _SINGLE_PAGE_ALIASES
