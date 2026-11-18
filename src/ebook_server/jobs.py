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
        "on_stopped",
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
        if getattr(func, "__name__", "") in {
            "process_conversion_job",
            "process_ocr_job",
            "process_scan_finalization_job",
        }:
            from ebook_server.core.worker_failures import rq_failure_callback, rq_stopped_callback

            kwargs.setdefault("on_failure", rq_failure_callback)
            kwargs.setdefault("on_stopped", rq_stopped_callback)
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


DEFAULT_LLM_JOB_TIMEOUT_SECONDS = 5 * 60


def llm_job_timeout_seconds() -> int:
    """RQ per-job timeout for LLM identification jobs (one request plus a provider pass)."""
    return max(
        60,
        int(
            float(
                tunable_setting(
                    "BOOK_SCANNER_LLM_JOB_TIMEOUT_SECONDS", DEFAULT_LLM_JOB_TIMEOUT_SECONDS
                )
            )
        ),
    )


def llm_queue_name() -> str:
    """RQ queue for LLM jobs, kept apart so a bulk import cannot starve OCR (#26)."""
    return str(tunable_setting("BOOK_SCANNER_LLM_QUEUE_NAME", "llm"))


def job_timeout_seconds(job_type: str) -> int:
    if job_type == "ocr":
        return ocr_job_timeout_seconds()
    if job_type == "llm_identification":
        return llm_job_timeout_seconds()
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


def rq_pending_job_args() -> tuple[set[str] | None, bool | None]:
    """Return (first positional arg of every live RQ job, scheduler running?).

    "Live" covers the queue itself plus the started, scheduled and deferred
    registries, i.e. every RQ job that may still execute. Job functions take the
    database row id as their first argument, so the set identifies rows that still
    have an RQ job; a batch job taking a list of row ids (``process_stage_jobs``)
    contributes every member (see :func:`rq_job_row_ids`). ``None`` means Redis could
    not be consulted (or the inline queue is in use) and callers must not assume
    anything about queued work.
    """
    queue = get_queue()
    rq_queue = getattr(queue, "queue", None)
    if rq_queue is None:
        return None, None
    # LLM identification jobs run on their own queue (#26); inspect it too.
    rq_queues = [rq_queue]
    llm_queue = getattr(get_queue(llm_queue_name()), "queue", None)
    if llm_queue is not None and llm_queue.name != rq_queue.name:
        rq_queues.append(llm_queue)
    try:
        jobs: list[Any] = []
        for inspected in rq_queues:
            jobs.extend(inspected.get_jobs())
            for registry in _live_registries(inspected):
                jobs.extend(
                    inspected.fetch_job(registry_job_id(member))
                    for member in registry.get_job_ids()
                )
        scheduler = scheduler_active(rq_queue)
    except Exception:
        return None, None
    ids = {row_id for job in jobs if job is not None for row_id in rq_job_row_ids(job)}
    return ids, scheduler


def rq_job_row_ids(job: Any) -> list[str]:
    """Database row ids an RQ job works on: its first positional argument.

    Most job functions take one row id. Batch functions such as
    ``scantailor.process_stage_jobs`` take a list of row ids; each member counts, so a
    healthy queued chunk is never mistaken for a lost job (#195).
    """
    if not job.args:
        return []
    first = job.args[0]
    if isinstance(first, list | tuple | set | frozenset):
        return [str(member) for member in first]
    return [str(first)]


def _live_registries(rq_queue: Queue) -> list[Any]:
    """RQ registries whose jobs may still execute (started, scheduled, deferred)."""
    from rq.registry import (
        DeferredJobRegistry,
        ScheduledJobRegistry,
        StartedJobRegistry,
    )

    return [
        registry_cls(queue=rq_queue)
        for registry_cls in (StartedJobRegistry, ScheduledJobRegistry, DeferredJobRegistry)
    ]


def _rq_registries(rq_queue: Queue) -> list[Any]:
    """Every RQ registry from which a job can still run or be re-run."""
    from rq.registry import (
        DeferredJobRegistry,
        FailedJobRegistry,
        ScheduledJobRegistry,
        StartedJobRegistry,
    )

    return [
        registry_cls(queue=rq_queue)
        for registry_cls in (
            StartedJobRegistry,
            ScheduledJobRegistry,
            DeferredJobRegistry,
            FailedJobRegistry,
        )
    ]


def discard_rq_jobs(func_name: str, first_args: set[str]) -> int | None:
    """Delete RQ jobs running ``func_name`` for the given row ids.

    Removes them from the queue and from the started, scheduled, deferred and failed
    registries, so neither a scheduled retry nor RQ's abandoned-job handling after
    a worker restart can run them again. Returns the number deleted, or ``None``
    when there is no inspectable RQ queue (inline/no queue, or Redis unreachable).
    """
    rq_queue = getattr(get_queue(), "queue", None)
    if rq_queue is None or not first_args:
        return None if rq_queue is None else 0
    try:
        registries = _rq_registries(rq_queue)
        # Registry members are job ids, except the started registry (rq >= 2), which
        # stores "<job_id>:<execution_id>"; keep the raw members to remove them.
        members: dict[str, list[tuple[Any, str]]] = {}
        for job_id in rq_queue.get_job_ids():
            members.setdefault(job_id, [])
        for registry in registries:
            for raw in registry.get_job_ids(cleanup=False):
                members.setdefault(registry_job_id(raw), []).append((registry, raw))
        removed = 0
        for job_id, entries in members.items():
            job = rq_queue.fetch_job(job_id)
            if job is None or job.func_name != func_name:
                continue
            row_ids = rq_job_row_ids(job)
            # A batch job is deleted only when every row it carries is discarded.
            if not row_ids or not set(row_ids) <= first_args:
                continue
            for registry, raw in entries:
                registry.connection.zrem(registry.key, raw)
            rq_queue.remove(job_id)
            job.delete()
            removed += 1
    except Exception:
        logger.warning("rq_discard_failed", func_name=func_name, exc_info=True)
        return None
    return removed


def registry_job_id(member: str) -> str:
    """Job id of an RQ registry member (``<job_id>`` or ``<job_id>:<execution_id>``)."""
    return member.split(":", 1)[0]


def queue_is_async() -> bool:
    """True when jobs go to a real RQ queue rather than running inline."""
    return isinstance(get_queue(), ConfiguredQueue)


def get_queue(name: str | None = None) -> QueueLike | None:
    """The configured queue; ``name`` selects another RQ queue on the same Redis."""
    if not settings.BOOK_SCANNER_QUEUE_URL:
        return None
    if settings.BOOK_SCANNER_QUEUE_URL.startswith("inline://"):
        return InlineQueue()
    connection = Redis.from_url(settings.BOOK_SCANNER_QUEUE_URL)
    queue = Queue(
        name=name or settings.BOOK_SCANNER_QUEUE_NAME,
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
