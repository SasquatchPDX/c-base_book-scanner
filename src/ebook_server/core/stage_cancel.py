"""Cooperative cancellation of a running ScanTailor stage job (#209).

A newer edit of a stage bumps its revision. The worker running the older revision
checks for that between its pipeline steps, and long steps (full-resolution dewarp
sampling, Output thresholding) call :func:`checkpoint` between tiles or column
blocks. When the run is no longer current, :class:`StageSuperseded` stops it before
it spends time on work whose result would be discarded.

The check is installed per run through a context variable, so the image code stays
free of database access and runs unchanged outside a worker (tests, tools).
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final

#: Minimum seconds between two checks from :func:`checkpoint` (each check is a query).
CHECK_INTERVAL_SECONDS: Final[float] = 0.2


class StageSuperseded(Exception):
    """The running stage job was superseded by a newer revision of its stage."""


class _Check:
    def __init__(self, check: Callable[[], bool]) -> None:
        self.check = check
        self.last = 0.0

    def __call__(self, *, force: bool) -> None:
        now = time.monotonic()
        if not force and now - self.last < CHECK_INTERVAL_SECONDS:
            return
        self.last = now
        if not self.check():
            raise StageSuperseded("stage revision changed while the job was running")


_current: ContextVar[_Check | None] = ContextVar("scantailor_stage_cancel", default=None)


@contextmanager
def cancellable(is_current: Callable[[], bool]) -> Iterator[None]:
    """Run the block with ``is_current`` as the check behind :func:`checkpoint`."""
    token = _current.set(_Check(is_current))
    try:
        yield
    finally:
        _current.reset(token)


def checkpoint(*, force: bool = False) -> None:
    """Raise :class:`StageSuperseded` if the running stage job is no longer current.

    Without ``force`` the check runs at most every :data:`CHECK_INTERVAL_SECONDS`, so
    loops can call it per tile or per column block. Outside :func:`cancellable` it
    does nothing.
    """
    check = _current.get()
    if check is not None:
        check(force=force)
