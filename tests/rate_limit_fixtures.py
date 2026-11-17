"""Shared fake clock for rate-limiter tests (no real sleeping)."""

from __future__ import annotations


class FakeClock:
    """Monotonic clock that only advances when something "sleeps" on it."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
