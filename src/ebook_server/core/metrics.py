"""Prometheus metrics derived from persistent shared application state."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime, timedelta
from typing import Any

from django.conf import settings
from django.db.models import Count
from django.utils import timezone
from prometheus_client.core import GaugeMetricFamily
from redis import Redis
from rq import Queue

from ebook_server.jobs import llm_queue_name

from .models import (
    Book,
    FileVariant,
    ImportJob,
    LLMSuggestion,
    MetadataCandidate,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    ScanTailorStageState,
)

JOB_DURATION_BUCKETS: tuple[float, ...] = (1, 5, 15, 60, 300, 900, 1800, 3600)
CONFIDENCE_BUCKETS: tuple[float, ...] = (0.25, 0.5, 0.55, 0.7, 0.86, 0.95, 1.0)
RECENT_WINDOWS: tuple[tuple[str, timedelta], ...] = (
    ("1h", timedelta(hours=1)),
    ("24h", timedelta(hours=24)),
)


def _group_count_metrics(
    *,
    name: str,
    documentation: str,
    rows: Iterable[dict[str, Any]],
    label_names: Sequence[str],
) -> GaugeMetricFamily:
    metric = GaugeMetricFamily(name, documentation, labels=list(label_names))
    for row in rows:
        metric.add_metric(
            [str(row[label]) for label in label_names],
            float(row["total"]),
        )
    return metric


def _bucket_population(values: Iterable[float], bounds: Sequence[float]) -> list[tuple[str, int]]:
    counts = [0 for _ in bounds]
    infinity_count = 0
    for value in values:
        infinity_count += 1
        for index, bound in enumerate(bounds):
            if value <= bound:
                counts[index] += 1
    return [(str(bound), counts[index]) for index, bound in enumerate(bounds)] + [
        ("+Inf", infinity_count),
    ]


class PersistentStateCollector:
    """Collect bounded-cardinality metrics visible across web and worker processes."""

    def collect(self) -> Iterator[GaugeMetricFamily]:
        collection = GaugeMetricFamily(
            "ebook_server_collection_objects",
            "Current canonical collection object counts.",
            labels=["object"],
        )
        collection.add_metric(["books"], Book.objects.count())
        collection.add_metric(
            ["ready_file_variants"],
            FileVariant.objects.filter(
                processing_state=FileVariant.ProcessingState.COMPLETE,
            ).count(),
        )
        yield collection

        yield _group_count_metrics(
            name="ebook_server_processing_jobs",
            documentation="Current processing job count by type and state.",
            rows=ProcessingJob.objects.values("job_type", "state").annotate(total=Count("id")),
            label_names=("job_type", "state"),
        )
        yield _group_count_metrics(
            name="ebook_server_import_jobs",
            documentation="Current import job count by state.",
            rows=ImportJob.objects.values("state").annotate(total=Count("id")),
            label_names=("state",),
        )
        yield _group_count_metrics(
            name="ebook_server_review_tasks",
            documentation="Current review task count by type and state.",
            rows=ReviewTask.objects.values("task_type", "state").annotate(total=Count("id")),
            label_names=("task_type", "state"),
        )
        yield _group_count_metrics(
            name="ebook_server_scan_jobs",
            documentation="Current scan job count by state.",
            rows=ScanJob.objects.values("state").annotate(total=Count("id")),
            label_names=("state",),
        )
        yield _group_count_metrics(
            name="ebook_server_scantailor_stages",
            documentation="Current ScanTailor page-stage count by stage and status.",
            rows=ScanTailorStageState.objects.values("stage", "status").annotate(
                total=Count("id"),
            ),
            label_names=("stage", "status"),
        )

        now = timezone.now()
        yield self._oldest_active_processing_job_metric(now)
        yield self._recent_processing_failure_metric(now)
        yield self._recent_import_failure_metric(now)
        yield self._recent_scantailor_failure_metric(now)

        duration_values: dict[tuple[str, str], list[float]] = defaultdict(list)
        jobs = (
            ProcessingJob.objects.exclude(started_at=None)
            .exclude(finished_at=None)
            .values_list(
                "job_type",
                "state",
                "started_at",
                "finished_at",
            )
        )
        for job_type, state, started_at, finished_at in jobs.iterator():
            if started_at is None or finished_at is None:
                continue
            duration_values[(job_type, state)].append(
                max(0.0, (finished_at - started_at).total_seconds()),
            )
        duration_metric = GaugeMetricFamily(
            "ebook_server_processing_job_duration_bucket_population",
            "Current processing-job duration distribution; buckets are population gauges.",
            labels=["job_type", "state", "le"],
        )
        for (job_type, state), durations in duration_values.items():
            for upper_bound, count in _bucket_population(durations, JOB_DURATION_BUCKETS):
                duration_metric.add_metric([job_type, state, upper_bound], count)
        yield duration_metric

        confidence_values: dict[str, list[float]] = defaultdict(list)
        candidates = MetadataCandidate.objects.values_list("provider_id", "score")
        for provider_id, score in candidates.iterator():
            confidence_values[provider_id].append(float(score))
        confidence_metric = GaugeMetricFamily(
            "ebook_server_metadata_confidence_bucket_population",
            "Current metadata candidate confidence distribution by provider.",
            labels=["provider", "le"],
        )
        for provider_id, scores in confidence_values.items():
            for upper_bound, count in _bucket_population(scores, CONFIDENCE_BUCKETS):
                confidence_metric.add_metric([provider_id, upper_bound], count)
        yield confidence_metric

        yield self._queue_metric()
        yield from self._llm_metrics(now)

    @staticmethod
    def _llm_metrics(now: datetime) -> Iterator[GaugeMetricFamily]:
        """LLM identification assist (#26): request outcomes and whether it helps."""
        yield _group_count_metrics(
            name="ebook_server_llm_requests",
            documentation="LLM identification requests by trigger and outcome.",
            rows=LLMSuggestion.objects.values("trigger", "outcome").annotate(total=Count("id")),
            label_names=("trigger", "outcome"),
        )
        recent = GaugeMetricFamily(
            "ebook_server_llm_requests_recent",
            "LLM identification requests within a bounded recent window, by outcome.",
            labels=["outcome", "window"],
        )
        for window, delta in RECENT_WINDOWS:
            rows = (
                LLMSuggestion.objects.filter(created_at__gte=now - delta)
                .values("outcome")
                .annotate(total=Count("id"))
            )
            for row in rows:
                recent.add_metric([str(row["outcome"]), window], float(row["total"]))
        yield recent

        # Did the assist help? Resolution of review tasks the LLM was asked about.
        resolutions: dict[str, int] = defaultdict(int)
        tasks = (
            ReviewTask.objects.filter(
                llm_suggestions__isnull=False,
                state=ReviewTask.State.RESOLVED,
            )
            .distinct()
            .select_related("selected_candidate")
        )
        for task in tasks.iterator():
            if task.decision == ReviewTask.Decision.ACCEPT_CANDIDATE:
                candidate = task.selected_candidate
                raw = candidate.raw_payload if candidate is not None else None
                llm_found = isinstance(raw, dict) and raw.get("_hint_source") == "llm"
                resolutions["accepted_llm_candidate" if llm_found else "accepted_other"] += 1
            elif task.decision == ReviewTask.Decision.MANUAL_ENTRY:
                resolutions["manual_entry"] += 1
            elif task.decision == ReviewTask.Decision.MARK_UNIDENTIFIED:
                resolutions["unidentified"] += 1
            else:
                resolutions["other"] += 1
        assisted = GaugeMetricFamily(
            "ebook_server_llm_assisted_reviews",
            "Resolved review tasks that had an LLM suggestion, by how they were resolved.",
            labels=["resolution"],
        )
        for resolution, total in sorted(resolutions.items()):
            assisted.add_metric([resolution], float(total))
        yield assisted

    @staticmethod
    def _oldest_active_processing_job_metric(now: datetime) -> GaugeMetricFamily:
        metric = GaugeMetricFamily(
            "ebook_server_processing_job_oldest_age_seconds",
            "Age of the oldest queued or running processing job.",
            labels=["job_type", "state"],
        )
        oldest: dict[tuple[str, str], float] = {}
        active = ProcessingJob.objects.filter(
            state__in=[ProcessingJob.JobState.QUEUED, ProcessingJob.JobState.RUNNING],
        ).values_list("job_type", "state", "scheduled_at", "started_at")
        for job_type, state, scheduled_at, started_at in active.iterator():
            reference = (
                started_at
                if state == ProcessingJob.JobState.RUNNING and started_at is not None
                else scheduled_at
            )
            age = max(0.0, (now - reference).total_seconds())
            key = (job_type, state)
            oldest[key] = max(age, oldest.get(key, 0.0))
        for (job_type, state), age in oldest.items():
            metric.add_metric([job_type, state], age)
        return metric

    @staticmethod
    def _recent_processing_failure_metric(now: datetime) -> GaugeMetricFamily:
        metric = GaugeMetricFamily(
            "ebook_server_processing_job_failures_recent",
            "Processing jobs that failed within a bounded recent window.",
            labels=["job_type", "window"],
        )
        for window, delta in RECENT_WINDOWS:
            rows = (
                ProcessingJob.objects.filter(
                    state=ProcessingJob.JobState.FAILED,
                    finished_at__gte=now - delta,
                )
                .values("job_type")
                .annotate(total=Count("id"))
            )
            for row in rows:
                metric.add_metric([str(row["job_type"]), window], float(row["total"]))
        return metric

    @staticmethod
    def _recent_import_failure_metric(now: datetime) -> GaugeMetricFamily:
        metric = GaugeMetricFamily(
            "ebook_server_import_failures_recent",
            "Import jobs that failed within a bounded recent window.",
            labels=["source_type", "window"],
        )
        for window, delta in RECENT_WINDOWS:
            rows = (
                ImportJob.objects.filter(
                    state=ImportJob.State.FAILED,
                    updated_at__gte=now - delta,
                )
                .values("source_type")
                .annotate(total=Count("id"))
            )
            for row in rows:
                metric.add_metric([str(row["source_type"]), window], float(row["total"]))
        return metric

    @staticmethod
    def _recent_scantailor_failure_metric(now: datetime) -> GaugeMetricFamily:
        metric = GaugeMetricFamily(
            "ebook_server_scantailor_failures_recent",
            "ScanTailor stages that failed within a bounded recent window.",
            labels=["stage", "window"],
        )
        for window, delta in RECENT_WINDOWS:
            rows = (
                ScanTailorStageState.objects.filter(
                    status=ScanTailorStageState.StageStatus.FAILED,
                    updated_at__gte=now - delta,
                )
                .values("stage")
                .annotate(total=Count("id"))
            )
            for row in rows:
                metric.add_metric([str(row["stage"]), window], float(row["total"]))
        return metric

    @staticmethod
    def _queue_metric() -> GaugeMetricFamily:
        metric = GaugeMetricFamily(
            "ebook_server_queue",
            "RQ queue health and depth.",
            labels=["queue", "measure"],
        )
        queue_url = settings.BOOK_SCANNER_QUEUE_URL
        queue_names = [settings.BOOK_SCANNER_QUEUE_NAME]
        if getattr(settings, "BOOK_SCANNER_LLM_ENABLED", False):
            llm_queue = llm_queue_name()
            if llm_queue not in queue_names:
                queue_names.append(llm_queue)
        for queue_name in queue_names:
            PersistentStateCollector._add_queue_samples(metric, queue_url, queue_name)
        return metric

    @staticmethod
    def _add_queue_samples(
        metric: GaugeMetricFamily,
        queue_url: str | None,
        queue_name: str,
    ) -> None:
        if not queue_url:
            metric.add_metric([queue_name, "enabled"], 0)
            metric.add_metric([queue_name, "up"], 0)
            metric.add_metric([queue_name, "depth"], 0)
            return
        if queue_url.startswith("inline://"):
            metric.add_metric([queue_name, "enabled"], 0)
            metric.add_metric([queue_name, "up"], 1)
            metric.add_metric([queue_name, "depth"], 0)
            return
        try:
            connection = Redis.from_url(queue_url)
            queue = Queue(name=queue_name, connection=connection)
            depth = queue.count
        except Exception:  # pragma: no cover - infrastructure-specific failure
            metric.add_metric([queue_name, "enabled"], 1)
            metric.add_metric([queue_name, "up"], 0)
            metric.add_metric([queue_name, "depth"], 0)
            return
        metric.add_metric([queue_name, "enabled"], 1)
        metric.add_metric([queue_name, "up"], 1)
        metric.add_metric([queue_name, "depth"], depth)
