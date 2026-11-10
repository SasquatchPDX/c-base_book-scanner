"""Background-job helper wrappers."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

import structlog
from django.conf import settings
from redis import Redis
from rq import Queue, Retry
from rq.scheduler import SCHEDULER_LOCKING_KEY_TEMPLATE

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class InlineJob:
    result: Any
    id: str = "inline"


class QueueLike(Protocol):
    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any: ...


# Keyword options understood by rq's Queue.enqueue; the inline queue must not
# forward them to the job function.
RQ_ENQUEUE_OPTIONS = frozenset(
    {
        "job_timeout",
        "result_ttl",
        "failure_ttl",
        "ttl",
        "retry",
        "meta",
        "job_id",
        "description",
        "depends_on",
        "at_front",
        "on_success",
        "on_failure",
    },
)


class InlineQueue:
    def enqueue(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> InlineJob:
        job_kwargs = {k: v for k, v in kwargs.items() if k not in RQ_ENQUEUE_OPTIONS}
        return InlineJob(result=func(*args, **job_kwargs))


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
            # In rq 2.x a retry with an interval is *scheduled*, and scheduled
            # jobs only run when some worker was started with --with-scheduler.
            # Without one, the job would sit in the scheduled registry forever
            # (leaving e.g. a ScanJob stuck in FINALIZING). Only use delayed
            # retries when a scheduler currently holds the queue's lock;
            # otherwise retry immediately, which needs no scheduler.
            interval: Any = 0
            if scheduler_active(self.queue):
                interval = settings.BOOK_SCANNER_QUEUE_RETRY_INTERVALS
            else:
                logger.warning(
                    "rq_scheduler_not_running_retries_immediate",
                    queue=getattr(self.queue, "name", "unknown"),
                    hint="start a worker with --with-scheduler for delayed retries",
                )
            kwargs["retry"] = Retry(max=settings.BOOK_SCANNER_QUEUE_RETRY_MAX, interval=interval)
        return self.queue.enqueue(func, *args, **kwargs)


DEFAULT_OCR_JOB_TIMEOUT_SECONDS = 2 * 60 * 60
STALE_JOB_MARGIN_SECONDS = 300


def tunable_setting(name: str, default: Any) -> Any:
    """Django setting, then process environment, then default.

    Lets optional tuning knobs work even before they are wired into settings.py.
    """
    value = getattr(settings, name, None)
    if value is None:
        value = os.environ.get(name)
    return default if value in (None, "") else value


def ocr_job_timeout_seconds() -> int:
    """RQ per-job timeout for OCR jobs; must exceed the OCR polling deadline."""
    return max(
        60,
        int(
            float(
                tunable_setting(
                    "BOOK_SCANNER_OCR_JOB_TIMEOUT_SECONDS", DEFAULT_OCR_JOB_TIMEOUT_SECONDS
                )
            )
        ),
    )


def job_timeout_seconds(job_type: str) -> int:
    if job_type == "ocr":
        return ocr_job_timeout_seconds()
    return int(settings.BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS)


def stale_after(job_type: str) -> timedelta:
    return timedelta(seconds=job_timeout_seconds(job_type) + STALE_JOB_MARGIN_SECONDS)


def is_stale_running(job: Any) -> bool:
    """True for a RUNNING ProcessingJob whose worker must have died.

    A live job cannot outlive its RQ timeout, so once started_at is older than
    timeout + margin the worker was killed (OOM, restart) and the row is orphaned.
    """
    from django.utils import timezone

    if job.state != "running":
        return False
    if job.started_at is None:
        return True
    return bool(timezone.now() - job.started_at > stale_after(job.job_type))


def scheduler_active(queue: Queue) -> bool:
    """True when an rq scheduler (worker --with-scheduler) holds this queue's lock."""
    try:
        return bool(queue.connection.exists(SCHEDULER_LOCKING_KEY_TEMPLATE % queue.name))
    except Exception:  # redis hiccup: assume no scheduler, retry immediately
        return False


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
