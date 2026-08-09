"""Background-job helper wrappers."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from django.conf import settings
from redis import Redis
from rq import Queue


@dataclass(frozen=True)
class InlineJob:
    result: Any
    id: str = "inline"


class QueueLike(Protocol):
    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any: ...


class InlineQueue:
    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> InlineJob:
        return InlineJob(result=func(*args, **kwargs))


def get_queue() -> QueueLike | Queue | None:
    if not settings.BOOK_SCANNER_QUEUE_URL:
        return None
    if settings.BOOK_SCANNER_QUEUE_URL.startswith("inline://"):
        return InlineQueue()
    connection = Redis.from_url(settings.BOOK_SCANNER_QUEUE_URL)
    return Queue(connection=connection)


def enqueue(func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Dispatch job to queue when queue is configured."""

    queue = get_queue()
    if queue is None:
        return func(*args, **kwargs)
    return queue.enqueue(func, *args, **kwargs)
