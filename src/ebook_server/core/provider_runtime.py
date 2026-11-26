"""Shared runtime state for metadata-provider lookups (#220, option G2).

Two pieces of state outlive one lookup:

* a **circuit breaker** per provider: after ``failure_threshold`` consecutive failed
  lookups the provider is skipped (reported as an error, "circuit open") until
  ``cooldown_seconds`` have passed, so a dead provider no longer costs its full
  timeout on every book of an import run;
* a **result cache** for identical queries (the same provider, title hints, author,
  ISBN and folder hint), so the books of a series or the other formats of one book
  reuse a lookup instead of repeating it. Failures are never cached.

RQ runs every job in a fresh work horse, so the state lives in Redis when
``BOOK_SCANNER_QUEUE_URL`` is configured, like the provider rate limiter. Without
Redis (development, tests, inline queue) an in-process store is used, and an
unreachable Redis falls back to it instead of failing the lookup.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

import structlog
from django.conf import settings as django_settings

logger = structlog.get_logger(__name__)

REDIS_KEY_PREFIX = "ebook-server:metadata-provider:"
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 120.0
DEFAULT_CACHE_SECONDS = 900.0
# Cached payloads are bounded: a provider answer larger than this is not stored.
MAX_CACHED_BYTES = 512 * 1024


@dataclass(frozen=True)
class RuntimeSettings:
    failure_threshold: int
    cooldown_seconds: float
    cache_seconds: float


def runtime_settings() -> RuntimeSettings:
    def setting(name: str, default: Any) -> Any:
        return getattr(django_settings, f"BOOK_SCANNER_METADATA_PROVIDER_{name}", default)

    return RuntimeSettings(
        failure_threshold=int(setting("BREAKER_FAILURES", DEFAULT_FAILURE_THRESHOLD)),
        cooldown_seconds=float(setting("BREAKER_COOLDOWN_SECONDS", DEFAULT_COOLDOWN_SECONDS)),
        cache_seconds=float(setting("CACHE_SECONDS", DEFAULT_CACHE_SECONDS)),
    )


class ProviderStateStore(Protocol):
    def get(self, key: str) -> str | None: ...

    def set(self, key: str, value: str, ttl_seconds: float) -> None: ...

    def increment(self, key: str, ttl_seconds: float) -> int: ...

    def delete(self, key: str) -> None: ...


class LocalProviderStateStore:
    """Thread-safe in-process store with expiry."""

    def __init__(self, *, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._values: dict[str, tuple[str, float]] = {}

    def _live(self, key: str) -> str | None:
        item = self._values.get(key)
        if item is None:
            return None
        if item[1] <= self._clock():
            del self._values[key]
            return None
        return item[0]

    def get(self, key: str) -> str | None:
        with self._lock:
            return self._live(key)

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        with self._lock:
            self._values[key] = (value, self._clock() + max(ttl_seconds, 0.001))

    def increment(self, key: str, ttl_seconds: float) -> int:
        with self._lock:
            count = int(self._live(key) or 0) + 1
            self._values[key] = (str(count), self._clock() + max(ttl_seconds, 0.001))
            return count

    def delete(self, key: str) -> None:
        with self._lock:
            self._values.pop(key, None)


class RedisProviderStateStore:
    """Store shared by every worker process using the same Redis."""

    def __init__(self, client: Any, fallback: ProviderStateStore | None = None) -> None:
        self._client = client
        self._fallback = fallback or LocalProviderStateStore()

    def _failed(self, exc: Exception) -> None:
        logger.warning("provider_state_redis_unavailable", error_type=type(exc).__name__)

    def get(self, key: str) -> str | None:
        try:
            value = self._client.get(REDIS_KEY_PREFIX + key)
        except Exception as exc:  # Redis outage must not block metadata lookups.
            self._failed(exc)
            return self._fallback.get(key)
        if value is None:
            return None
        return value.decode("utf-8") if isinstance(value, bytes) else str(value)

    def set(self, key: str, value: str, ttl_seconds: float) -> None:
        try:
            self._client.set(REDIS_KEY_PREFIX + key, value, px=max(1, round(ttl_seconds * 1000)))
        except Exception as exc:
            self._failed(exc)
            self._fallback.set(key, value, ttl_seconds)

    def increment(self, key: str, ttl_seconds: float) -> int:
        try:
            pipe = self._client.pipeline()
            pipe.incr(REDIS_KEY_PREFIX + key)
            pipe.pexpire(REDIS_KEY_PREFIX + key, max(1, round(ttl_seconds * 1000)))
            count, _ = pipe.execute()
            return int(count)
        except Exception as exc:
            self._failed(exc)
            return self._fallback.increment(key, ttl_seconds)

    def delete(self, key: str) -> None:
        try:
            self._client.delete(REDIS_KEY_PREFIX + key)
        except Exception as exc:
            self._failed(exc)
            self._fallback.delete(key)


_store: ProviderStateStore | None = None
_store_lock = threading.Lock()


def _build_store() -> ProviderStateStore:
    redis_url = getattr(django_settings, "BOOK_SCANNER_QUEUE_URL", None)
    if redis_url:
        from redis import Redis

        return RedisProviderStateStore(Redis.from_url(redis_url))
    return LocalProviderStateStore()


def get_store() -> ProviderStateStore:
    global _store
    with _store_lock:
        if _store is None:
            _store = _build_store()
        return _store


def set_store(store: ProviderStateStore | None) -> None:
    """Replace the process-wide store (tests); ``None`` rebuilds it from settings."""
    global _store
    with _store_lock:
        _store = store


# --- Circuit breaker ---------------------------------------------------------------------


def _failures_key(provider_id: str) -> str:
    return f"failures:{provider_id}"


def _open_key(provider_id: str) -> str:
    return f"open:{provider_id}"


def breaker_open(provider_id: str) -> bool:
    """Whether ``provider_id`` failed repeatedly and is skipped for now."""
    return get_store().get(_open_key(provider_id)) is not None


def record_failure(provider_id: str) -> bool:
    """Count one failed lookup; returns True when this failure opened the breaker."""
    config = runtime_settings()
    if config.failure_threshold <= 0:
        return False
    store = get_store()
    failures = store.increment(_failures_key(provider_id), config.cooldown_seconds)
    if failures < config.failure_threshold:
        return False
    store.set(_open_key(provider_id), str(failures), config.cooldown_seconds)
    store.delete(_failures_key(provider_id))
    logger.warning(
        "metadata_provider_circuit_opened",
        provider_id=provider_id,
        consecutive_failures=failures,
        cooldown_seconds=config.cooldown_seconds,
    )
    return True


def record_success(provider_id: str) -> None:
    get_store().delete(_failures_key(provider_id))


# --- Result cache ------------------------------------------------------------------------


def query_key(provider_id: str, query: dict[str, Any]) -> str:
    """Stable cache key for one provider query (no raw values, only a digest)."""
    payload = json.dumps({"provider": provider_id, **query}, sort_keys=True, default=str)
    return "result:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached_result(key: str) -> list[dict[str, Any]] | None:
    if runtime_settings().cache_seconds <= 0:
        return None
    value = get_store().get(key)
    if value is None:
        return None
    try:
        rows = json.loads(value)
    except ValueError:
        return None
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        return None
    return rows


def store_result(key: str, rows: list[dict[str, Any]]) -> None:
    ttl = runtime_settings().cache_seconds
    if ttl <= 0:
        return
    try:
        value = json.dumps(rows)
    except (TypeError, ValueError):
        return  # not JSON-safe: simply not reused
    if len(value) > MAX_CACHED_BYTES:
        return
    get_store().set(key, value, ttl)
