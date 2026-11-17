"""Client-side rate limiting for outbound metadata-provider requests.

Public bibliographic APIs (Open Library, and any provider added later) expect bulk
clients to pace themselves. Limits are enforced *before* a request is sent, keyed by
``provider_id``, as a minimum interval between consecutive requests.

RQ workers run as several processes, so when a Redis URL is configured the next free
request slot is reserved atomically in Redis (using Redis server time, so worker clock
skew does not matter). Without Redis (development, tests, inline queue) a thread-safe
in-process limiter is used instead. If Redis is configured but unreachable, the call
falls back to the in-process limiter rather than failing the lookup.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Protocol

import structlog
from django.conf import settings as django_settings

logger = structlog.get_logger(__name__)

DEFAULT_REQUESTS_PER_SECOND = 1.0
DEFAULT_MAX_WAIT_SECONDS = 60.0
REDIS_KEY_PREFIX = "ebook-server:ratelimit:"

# Reserve the next request slot for KEYS[1].
# ARGV[1] = interval in ms, ARGV[2] = max wait in ms.
# Returns the wait in ms, or -1 when the wait would exceed the maximum (nothing reserved).
_RESERVE_SLOT_LUA = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
local interval = tonumber(ARGV[1])
local next_free = tonumber(redis.call('GET', KEYS[1]) or '0')
local slot = math.max(next_free, now)
local wait = slot - now
if wait > tonumber(ARGV[2]) then
  return -1
end
redis.call('SET', KEYS[1], slot + interval, 'PX', wait + interval + 1000)
return wait
"""


class RateLimitWaitExceeded(OSError):
    """The next request slot is further away than the configured maximum wait.

    Subclasses ``OSError`` so provider code treats it like any other failed request.
    """


class RateLimiter(Protocol):
    def acquire(self, key: str, min_interval: float) -> float:
        """Block until ``key`` may issue a request; return the seconds waited."""


class LocalRateLimiter:
    """Thread-safe per-process minimum-interval limiter."""

    def __init__(
        self,
        *,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_wait = max_wait
        self._clock = clock
        self._sleep = sleep
        self._lock = threading.Lock()
        self._next_free: dict[str, float] = {}

    def acquire(self, key: str, min_interval: float) -> float:
        with self._lock:
            now = self._clock()
            slot = max(self._next_free.get(key, now), now)
            wait = slot - now
            if wait > self.max_wait:
                raise RateLimitWaitExceeded(f"rate limit wait {wait:.1f}s exceeds maximum")
            self._next_free[key] = slot + min_interval
        if wait > 0:
            self._sleep(wait)
        return wait


class RedisRateLimiter:
    """Minimum-interval limiter shared by all processes using the same Redis."""

    def __init__(
        self,
        client: Any,
        *,
        max_wait: float = DEFAULT_MAX_WAIT_SECONDS,
        fallback: RateLimiter | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.max_wait = max_wait
        self._client = client
        self._script = client.register_script(_RESERVE_SLOT_LUA)
        self._fallback = fallback or LocalRateLimiter(max_wait=max_wait, sleep=sleep)
        self._sleep = sleep

    def acquire(self, key: str, min_interval: float) -> float:
        try:
            wait_ms = int(
                self._script(
                    keys=[REDIS_KEY_PREFIX + key],
                    args=[max(1, round(min_interval * 1000)), round(self.max_wait * 1000)],
                ),
            )
        except Exception as exc:  # Redis outage must not block metadata lookups.
            logger.warning(
                "rate_limiter_redis_unavailable",
                key=key,
                error_type=type(exc).__name__,
            )
            return self._fallback.acquire(key, min_interval)
        if wait_ms < 0:
            raise RateLimitWaitExceeded("rate limit wait exceeds maximum")
        wait = wait_ms / 1000
        if wait > 0:
            self._sleep(wait)
        return wait


_limiter: RateLimiter | None = None
_limiter_lock = threading.Lock()


def _build_limiter() -> RateLimiter:
    max_wait = float(
        getattr(
            django_settings,
            "BOOK_SCANNER_METADATA_RATE_LIMIT_MAX_WAIT_SECONDS",
            DEFAULT_MAX_WAIT_SECONDS,
        ),
    )
    redis_url = getattr(django_settings, "BOOK_SCANNER_QUEUE_URL", None)
    if redis_url:
        from redis import Redis

        return RedisRateLimiter(Redis.from_url(redis_url), max_wait=max_wait)
    return LocalRateLimiter(max_wait=max_wait)


def get_rate_limiter() -> RateLimiter:
    global _limiter
    with _limiter_lock:
        if _limiter is None:
            _limiter = _build_limiter()
        return _limiter


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """Replace the process-wide limiter (tests); ``None`` rebuilds it from settings."""
    global _limiter
    with _limiter_lock:
        _limiter = limiter


def provider_requests_per_second(provider_id: str) -> float:
    """Configured request rate for ``provider_id``; ``<= 0`` disables limiting."""
    per_provider: Mapping[str, float] = getattr(
        django_settings,
        "BOOK_SCANNER_METADATA_RATE_LIMITS",
        {},
    )
    if provider_id in per_provider:
        return float(per_provider[provider_id])
    return float(
        getattr(
            django_settings,
            "BOOK_SCANNER_METADATA_RATE_LIMIT_DEFAULT",
            DEFAULT_REQUESTS_PER_SECOND,
        ),
    )


def throttle_provider(provider_id: str) -> float:
    """Wait for ``provider_id``'s next request slot; call before every outbound request.

    Raises ``RateLimitWaitExceeded`` (an ``OSError``) when the slot is further away than
    ``BOOK_SCANNER_METADATA_RATE_LIMIT_MAX_WAIT_SECONDS``.
    """
    requests_per_second = provider_requests_per_second(provider_id)
    if requests_per_second <= 0:
        return 0.0
    try:
        waited = get_rate_limiter().acquire(
            f"metadata:{provider_id}",
            1.0 / requests_per_second,
        )
    except RateLimitWaitExceeded:
        logger.warning("metadata_provider_rate_limit_wait_exceeded", provider_id=provider_id)
        raise
    if waited > 0:
        logger.debug(
            "metadata_provider_throttled",
            provider_id=provider_id,
            wait_seconds=round(waited, 3),
        )
    return waited
