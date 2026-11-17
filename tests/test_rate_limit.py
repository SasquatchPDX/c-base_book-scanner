from __future__ import annotations

import json
from typing import Any
from unittest.mock import patch

import pytest
from django.test import override_settings

from ebook_server.core.identification import OpenLibraryMetadataProvider, ProviderRequestError
from ebook_server.core.rate_limit import (
    LocalRateLimiter,
    RateLimitWaitExceeded,
    RedisRateLimiter,
    provider_requests_per_second,
    set_rate_limiter,
    throttle_provider,
)
from tests.rate_limit_fixtures import FakeClock


def test_local_limiter_spaces_requests_per_key(rate_limit_clock: FakeClock) -> None:
    limiter = LocalRateLimiter(clock=rate_limit_clock, sleep=rate_limit_clock.sleep)

    assert limiter.acquire("a", 1.0) == 0
    assert limiter.acquire("a", 1.0) == pytest.approx(1.0)
    assert limiter.acquire("b", 1.0) == 0  # independent key
    rate_limit_clock.now += 5  # idle time is not banked as burst credit
    assert limiter.acquire("a", 1.0) == 0
    assert limiter.acquire("a", 1.0) == pytest.approx(1.0)
    assert rate_limit_clock.sleeps == [pytest.approx(1.0), pytest.approx(1.0)]


def test_local_limiter_refuses_waits_beyond_maximum_without_reserving() -> None:
    clock = FakeClock()
    limiter = LocalRateLimiter(max_wait=1.5, clock=clock, sleep=lambda _: None)
    limiter.acquire("k", 1.0)
    limiter.acquire("k", 1.0)  # reserved slot at +1s (clock not advanced)

    with pytest.raises(RateLimitWaitExceeded):
        limiter.acquire("k", 1.0)  # would be +2s
    clock.now += 0.6
    assert limiter.acquire("k", 1.0) == pytest.approx(1.4)  # refused call reserved nothing


class _FakeRedisScript:
    """Python model of the reservation Lua script, with a controllable server clock."""

    def __init__(self) -> None:
        self.now_ms = 1_000_000
        self.store: dict[str, int] = {}
        self.calls: list[tuple[list[str], list[int]]] = []

    def __call__(self, *, keys: list[str], args: list[int]) -> int:
        self.calls.append((keys, args))
        interval, max_wait = args
        slot = max(self.store.get(keys[0], 0), self.now_ms)
        wait = slot - self.now_ms
        if wait > max_wait:
            return -1
        self.store[keys[0]] = slot + interval
        return wait


class _FakeRedisClient:
    def __init__(self, script: Any) -> None:
        self.script = script

    def register_script(self, source: str) -> Any:
        assert "TIME" in source
        return self.script


def test_redis_limiter_shares_slots_between_processes() -> None:
    script = _FakeRedisScript()
    slept: list[float] = []
    worker_a = RedisRateLimiter(_FakeRedisClient(script), sleep=slept.append)
    worker_b = RedisRateLimiter(_FakeRedisClient(script), sleep=slept.append)

    assert worker_a.acquire("metadata:openlibrary", 1.0) == 0
    assert worker_b.acquire("metadata:openlibrary", 1.0) == pytest.approx(1.0)
    assert worker_a.acquire("metadata:openlibrary", 1.0) == pytest.approx(2.0)
    assert slept == [1.0, 2.0]
    assert script.calls[0] == (["ebook-server:ratelimit:metadata:openlibrary"], [1000, 60000])


def test_redis_limiter_raises_when_wait_exceeds_maximum() -> None:
    script = _FakeRedisScript()
    limiter = RedisRateLimiter(_FakeRedisClient(script), max_wait=0.5, sleep=lambda _: None)
    limiter.acquire("k", 1.0)

    with pytest.raises(RateLimitWaitExceeded):
        limiter.acquire("k", 1.0)


def test_redis_outage_falls_back_to_local_limiter() -> None:
    def broken_script(**_: Any) -> int:
        raise ConnectionError("redis down")

    clock = FakeClock()
    limiter = RedisRateLimiter(
        _FakeRedisClient(broken_script),
        fallback=LocalRateLimiter(clock=clock, sleep=clock.sleep),
    )

    assert limiter.acquire("k", 1.0) == 0
    assert limiter.acquire("k", 1.0) == pytest.approx(1.0)
    assert clock.sleeps == [pytest.approx(1.0)]


@override_settings(
    BOOK_SCANNER_METADATA_RATE_LIMITS={"openlibrary": 2.0, "slow": 0.5, "off": 0},
    BOOK_SCANNER_METADATA_RATE_LIMIT_DEFAULT=0.25,
)
def test_rate_is_configurable_per_provider_with_default(rate_limit_clock: FakeClock) -> None:
    assert provider_requests_per_second("openlibrary") == 2.0
    assert provider_requests_per_second("unlisted") == 0.25

    for _ in range(3):
        throttle_provider("off")
    assert rate_limit_clock.sleeps == []

    throttle_provider("slow")
    throttle_provider("slow")
    throttle_provider("openlibrary")
    throttle_provider("openlibrary")
    assert rate_limit_clock.sleeps == [pytest.approx(2.0), pytest.approx(0.5)]


class _FakeResponse:
    status = 200

    def __init__(self, payload: object) -> None:
        self._body = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@override_settings(BOOK_SCANNER_METADATA_RATE_LIMITS={"openlibrary": 1.0})
def test_open_library_paces_every_request_before_sending(rate_limit_clock: FakeClock) -> None:
    edition = {
        "title": "Good Omens",
        "authors": [{"key": "/authors/OL1A"}, {"key": "/authors/OL2A"}],
    }
    sent_at: list[float] = []

    def opener(request: Any, timeout: float = 0) -> _FakeResponse:
        del timeout
        sent_at.append(rate_limit_clock.now)
        if "/isbn/" in request.full_url:
            return _FakeResponse(edition)
        return _FakeResponse({"name": "Someone"})

    with patch("ebook_server.core.identification.urlopen", opener):
        OpenLibraryMetadataProvider().search("Good Omens", isbn="9780306406157")

    assert len(sent_at) == 3  # edition + two author lookups
    gaps = [later - earlier for earlier, later in zip(sent_at, sent_at[1:], strict=False)]
    assert gaps == [pytest.approx(1.0), pytest.approx(1.0)]


@override_settings(
    BOOK_SCANNER_METADATA_RATE_LIMITS={"openlibrary": 1.0},
    BOOK_SCANNER_METADATA_RATE_LIMIT_DEFAULT=1.0,
)
def test_open_library_treats_excessive_wait_as_failed_request() -> None:
    class _Saturated:
        def acquire(self, key: str, min_interval: float) -> float:
            raise RateLimitWaitExceeded("queue full")

    set_rate_limiter(_Saturated())
    with patch("ebook_server.core.identification.urlopen") as opener:
        # A failed request is reported as an error (#40), never as "no results".
        with pytest.raises(ProviderRequestError, match="rate limited"):
            OpenLibraryMetadataProvider().search("Dune")
    opener.assert_not_called()
