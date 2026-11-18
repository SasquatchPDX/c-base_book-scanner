"""Run through manage.py shell on a disposable seeded PostgreSQL database (#167)."""

import json
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import uuid4

from django.conf import settings
from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import close_old_connections, connection
from django.test import Client, override_settings

from ebook_server.core.models import Book, BookSearchDocument, ImportJob, get_or_create_profile
from ebook_server.core.search import DEFAULT_SORT, Cursor, get_search_backend
from ebook_server.settings import settings as application_settings


def run_benchmark():
    if (
        connection.vendor != "postgresql"
        or connection.settings_dict["NAME"] != "bench167"
        or settings.BOOK_SCANNER_QUEUE_URL
        or application_settings.environment != "development"
        or Book.objects.count() != 150_000
        or ImportJob.objects.exclude(source_path__startswith="/synthetic/").exists()
    ):
        raise RuntimeError("Use a fresh 150,000-book bench167 development database without Redis.")
    rounds = int(os.environ.get("BENCHMARK_ROUNDS", "20"))
    if rounds < 20:
        raise ValueError("Use at least 20 samples for p95.")
    user = get_or_create_profile("synthetic-benchmark").user
    reader = Client()
    reader.force_login(user)
    anchor = Book.objects.order_by("-created_at", "-pk")[99_999]
    cursor = Cursor(DEFAULT_SORT, anchor.created_at.isoformat(), anchor.pk).encode()
    workloads = {
        "ranked": {"q": "garden"},
        "facets": {"format": "epub", "language": "de", "facets": "genre,format,language"},
        "cursor": {"cursor": cursor},
    }
    plans = {}
    timings = {}
    stop, ready = Event(), Event()
    prefix = uuid4().hex[:8]

    def import_books():
        close_old_connections()
        uploader = Client()
        uploader.force_login(user)
        durations, index_lags = [], []
        try:
            while not stop.is_set():
                title = f"Library Benchmark {prefix} Book {len(durations)}"
                data = f"Title: {title}\nAuthor: Ada Digital\n\nA synthetic work.\n".encode()
                started = time.monotonic()
                response = uploader.post(
                    "/imports/upload/",
                    {"file": SimpleUploadedFile(f"{title}.txt", data, content_type="text/plain")},
                )
                if response.status_code != 201:
                    raise RuntimeError(f"Upload failed ({response.status_code})")
                job = ImportJob.objects.get(pk=response.json()["job_id"])
                if job.state != ImportJob.State.COMPLETE:
                    raise RuntimeError(f"Unexpected import state {job.state}")
                row = BookSearchDocument.objects.get(book_id=job.book_id)
                index_lags.append((row.indexed_at - row.book.created_at).total_seconds() * 1000)
                durations.append((time.monotonic() - started) * 1000)
                ready.set()
                stop.wait(0.5)
        finally:
            ready.set()
            connection.close()
        return {
            "count": len(durations),
            "import_ms": summary(durations),
            "book_to_index_ms": summary(index_lags),
        }

    def sample(key, parameters, capture=None):
        queries = []

        def sql_timer(execute, sql, params, many, context):
            start = time.perf_counter()
            result = execute(sql, params, many, context)
            if sql.lstrip().upper().startswith("SELECT"):
                queries.append((time.perf_counter() - start, sql, params))
            return result

        start = time.perf_counter()
        with connection.execute_wrapper(sql_timer):
            response = reader.get("/library/books/", parameters, HTTP_ACCEPT="application/json")
        elapsed = (time.perf_counter() - start) * 1000
        if response.status_code != 200 or not response.json()["items"]:
            raise RuntimeError(f"Invalid benchmark response for {key}: {response.status_code}")
        if capture:
            duration, sql, params = max(queries, key=lambda query: query[0])
            with connection.cursor() as db:
                db.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql, params)
                plans[f"{capture}/{key}"] = {
                    "query_ms": round(duration * 1000, 1),
                    "plan": [row[0] for row in db.fetchall()],
                }
        return elapsed

    def summary(samples):
        return {
            "p50": round(statistics.median(samples), 1),
            "p95": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 1),
        }

    with (
        override_settings(
            ALLOWED_HOSTS=["testserver"],
            CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}},
            BOOK_SCANNER_METADATA_PROVIDERS=["tests.browser_fixtures.BrowserMetadataProvider"],
            BOOK_SCANNER_WRITE_BOOK_OPF=False,
            BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_ENABLED=True,
            BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS=0,
            BOOK_SCANNER_STORAGE_MIN_FREE_BYTES=0,
        ),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        try:
            if get_search_backend().name != "postgresql":
                raise RuntimeError("Finish rebuilding the PostgreSQL search index first.")
            for phase in ("baseline", "with_intake"):
                if phase == "with_intake":
                    writer = pool.submit(import_books)
                    if not ready.wait(60):
                        raise RuntimeError("Writer did not complete its first upload")
                    if writer.done():
                        writer.result()
                timings[phase] = {}
                for condition in ("app_cache_cold", "warm"):
                    samples = {key: [] for key in workloads}
                    for _ in range(rounds):
                        for key, parameters in workloads.items():
                            if condition == "app_cache_cold":
                                cache.clear()
                            samples[key].append(sample(key, parameters))
                    timings[phase][condition] = {
                        key: summary(observations) for key, observations in samples.items()
                    }
                for key, parameters in workloads.items():
                    sample(key, parameters, capture=phase)
        finally:
            stop.set()
    print(
        json.dumps(
            {
                "rounds": rounds,
                "postgres_version": connection.pg_version,
                "timings_ms": timings,
                "intake": writer.result(),
                "slowest_query_plans": plans,
            },
            indent=2,
        )
    )


run_benchmark()
