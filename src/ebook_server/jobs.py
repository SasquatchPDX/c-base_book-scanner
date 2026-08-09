"""Background-job helper wrappers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings
from redis import Redis
from rq import Queue, Retry


@dataclass(frozen=True)
class InlineJob:
    result: Any
    id: str = "inline"


class QueueLike(Protocol):
    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any: ...


class InlineQueue:
    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> InlineJob:
        return InlineJob(result=func(*args, **kwargs))


@dataclass(frozen=True)
class ConfiguredQueue:
    """RQ adapter that applies resource limits and retry policy to every job."""

    queue: Queue

    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        kwargs.setdefault(
            "job_timeout",
            settings.BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS,
        )
        kwargs.setdefault("result_ttl", settings.BOOK_SCANNER_QUEUE_RESULT_TTL_SECONDS)
        kwargs.setdefault("failure_ttl", settings.BOOK_SCANNER_QUEUE_FAILURE_TTL_SECONDS)
        if "retry" not in kwargs and settings.BOOK_SCANNER_QUEUE_RETRY_MAX > 0:
            kwargs["retry"] = Retry(
                max=settings.BOOK_SCANNER_QUEUE_RETRY_MAX,
                interval=settings.BOOK_SCANNER_QUEUE_RETRY_INTERVALS,
            )
        return self.queue.enqueue(func, *args, **kwargs)


def get_queue() -> QueueLike | None:
    if not settings.BOOK_SCANNER_QUEUE_URL:
        return None
    if settings.BOOK_SCANNER_QUEUE_URL.startswith("inline://"):
        return InlineQueue()
    connection = Redis.from_url(settings.BOOK_SCANNER_QUEUE_URL)
    queue = Queue(
        name=settings.BOOK_SCANNER_QUEUE_NAME,
        connection=connection,
        default_timeout=settings.BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS,
    )
    return ConfiguredQueue(queue)


def enqueue(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Dispatch job to queue when queue is configured."""

    queue = get_queue()
    if queue is None:
        return func(*args, **kwargs)
    return queue.enqueue(func, *args, **kwargs)
