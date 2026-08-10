from __future__ import annotations

import hashlib
import json
import operator
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

import httpx
import pytest
from django.conf import settings as django_settings
from django.contrib.auth import authenticate
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.http import StreamingHttpResponse
from django.test import Client, override_settings
from django.utils import timezone
from redis import Redis
from rq.job import JobStatus

import ebook_server.core.intake as intake
from ebook_server.core.auth import Ldap3Adapter, LdapAuthenticationError
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanTailorStageState,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.jobs import ConfiguredQueue, get_queue
from ebook_server.storage import LocalFilesystemStorage

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]


def _run_rq_worker(queue_name: str, *, environment: dict[str, str] | None = None) -> None:
    rq_executable = Path(sys.executable).with_name("rq")
    worker = subprocess.run(
        [
            str(rq_executable),
            "worker",
            "--burst",
            "--url",
            os.environ["BOOK_SCANNER_REDIS_URL"],
            "--config",
            "ebook_server.rq_settings",
            queue_name,
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment or os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=45,
        check=False,
    )
    assert worker.returncode == 0, worker.stdout + worker.stderr


def _worker_environment_for_test_database() -> dict[str, str]:
    environment = os.environ.copy()
    database_url = urlparse(environment["BOOK_SCANNER_DATABASE_URL"])
    test_database_name = str(connection.settings_dict["NAME"])
    environment["BOOK_SCANNER_DATABASE_URL"] = database_url._replace(
        path=f"/{test_database_name}",
    ).geturl()
    return environment


def _wait_for_http(url: str, process: subprocess.Popen[str], *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "no response"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            process_output = process.stdout.read() if process.stdout is not None else ""
            last_error = f"process exited {process.returncode}: {process_output[-2000:]}"
            break
        try:
            response = httpx.get(url, timeout=0.75)
            if response.status_code < 500:
                return
            last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = str(exc)
        time.sleep(0.1)
    raise AssertionError(f"Uvicorn did not become ready at {url}: {last_error}")


@contextmanager
def _running_uvicorn() -> Iterator[subprocess.Popen[str]]:
    environment = _worker_environment_for_test_database()
    environment["BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_INTERVAL_SECONDS"] = "0.2"
    environment["BOOK_SCANNER_SCANTAILOR_EVENT_STREAM_MAX_EVENTS"] = "30"
    uvicorn_executable = Path(sys.executable).with_name("uvicorn")
    process = subprocess.Popen(
        [
            str(uvicorn_executable),
            "ebook_server.asgi:application",
            "--host",
            "0.0.0.0",
            "--port",
            "58081",
            "--log-level",
            "warning",
            "--no-access-log",
            "--no-server-header",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_http("http://127.0.0.1:58081/healthz/", process)
        yield process
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


def _next_sse_event(lines: Iterable[str]) -> tuple[str, dict[str, object]]:
    event_name = ""
    data_lines: list[str] = []
    for line in lines:
        if line.startswith("event:"):
            event_name = line.removeprefix("event:").strip()
        elif line.startswith("data:"):
            data_lines.append(line.removeprefix("data:").strip())
        elif not line and event_name:
            payload = json.loads("\n".join(data_lines))
            assert isinstance(payload, dict)
            return event_name, payload
    raise AssertionError("SSE stream ended before an event was received")


@pytest.mark.django_db(transaction=True)
def test_postgresql_migrations_and_row_locking() -> None:
    assert connection.vendor == "postgresql"
    assert connection.features.has_select_for_update
    assert MigrationRecorder(connection).migration_qs.filter(app="core").count() >= 15

    book = Book.objects.create(title="Infrastructure Test Book")
    with transaction.atomic():
        locked = Book.objects.select_for_update().get(pk=book.pk)
        assert locked.title == "Infrastructure Test Book"


@pytest.mark.django_db(transaction=True)
def test_live_ldap_search_bind_and_django_authentication() -> None:
    identity = Ldap3Adapter().authenticate("fry", "fry")

    assert identity.username == "fry"
    assert identity.email == "fry@planetexpress.com"
    user = authenticate(request=None, username="fry", password="fry")
    assert user is not None
    assert user.username == "fry"
    assert user.email == "fry@planetexpress.com"

    with pytest.raises(LdapAuthenticationError, match="LDAP bind failed"):
        Ldap3Adapter().authenticate("fry", "incorrect-password")
    assert authenticate(request=None, username="fry", password="incorrect-password") is None


def test_redis_queue_is_consumed_by_real_rq_worker() -> None:
    queue_url = os.environ["BOOK_SCANNER_REDIS_URL"]
    parsed_url = urlparse(queue_url)
    assert parsed_url.hostname in {"127.0.0.1", "localhost"}
    assert parsed_url.path == "/15"

    redis_connection = Redis.from_url(queue_url)
    redis_connection.flushdb()
    try:
        configured_queue = get_queue()
        assert isinstance(configured_queue, ConfiguredQueue)
        job = configured_queue.enqueue(operator.add, 19, 23)

        _run_rq_worker(configured_queue.queue.name)
        job.refresh()
        assert job.get_status(refresh=True) == JobStatus.FINISHED
        assert job.return_value() == 42
    finally:
        redis_connection.flushdb()
        redis_connection.close()


@pytest.mark.django_db(transaction=True)
def test_member_conversion_runs_through_redis_rq_and_real_pandoc() -> None:
    pandoc_binary = shutil.which("pandoc")
    assert pandoc_binary is not None

    storage_root = Path(os.environ["BOOK_SCANNER_STORAGE_ROOT"])
    django_settings.APP_DATA_DIR = storage_root
    storage = LocalFilesystemStorage(str(storage_root / "library-artifacts"))
    location = StorageLocation.objects.create(
        identifier="default",
        backend=StorageLocation.BackendType.LOCAL,
        base_path="default",
    )
    profile = get_or_create_profile("pandoc-member")
    book = Book.objects.create(title="Pinned Pandoc Acceptance Book")
    source_payload = (
        b"Pinned Pandoc Acceptance Book\n\n"
        b"This text becomes a real EPUB through Redis and an RQ worker.\n"
    )
    source_staging_path = storage.resolve_location("integration/source.txt")
    source_staging_path.parent.mkdir(parents=True, exist_ok=True)
    source_staging_path.write_bytes(source_payload)
    source_identifier = storage.generate_destination(
        f"books/{book.id}/incoming",
        "source.txt",
    )
    storage.store(str(source_staging_path), source_identifier)
    source_variant = FileVariant.objects.create(
        book=book,
        format="txt",
        mime_type="text/plain",
        original_filename="source.txt",
        storage_location=location,
        storage_identifier=source_identifier,
        size_bytes=len(source_payload),
        sha256=hashlib.sha256(source_payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
        uploader=profile.user,
        imported_at=timezone.now(),
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )

    redis_connection = Redis.from_url(os.environ["BOOK_SCANNER_REDIS_URL"])
    redis_connection.flushdb()
    try:
        client = Client()
        client.force_login(profile.user)
        requested = client.post(
            f"/library/books/{book.id}/convert/",
            data=json.dumps(
                {
                    "source_variant_id": source_variant.id,
                    "target_format": "epub",
                },
            ),
            content_type="application/json",
        )

        assert requested.status_code == 202
        job = ProcessingJob.objects.get(job_id=requested.json()["job_id"])
        assert job.state == ProcessingJob.JobState.QUEUED

        queue = get_queue()
        assert isinstance(queue, ConfiguredQueue)
        _run_rq_worker(
            queue.queue.name,
            environment=_worker_environment_for_test_database(),
        )

        job.refresh_from_db()
        assert job.state == ProcessingJob.JobState.SUCCESS
        output_variant = FileVariant.objects.get(pk=job.details["output_variant_id"])
        assert output_variant.book_id == book.id
        assert output_variant.source_variant_id == source_variant.id
        assert output_variant.source_type == FileVariant.SourceType.CONVERTED
        assert output_variant.format == "epub"
        assert output_variant.converter_tool == "pandoc"
        assert output_variant.converter_version == "pandoc 3.9.0.2"
        assert output_variant.job_id == job.job_id
        assert storage.hash(source_identifier) == source_variant.sha256
        intake.validate_container_signature(
            str(storage.resolve_location(output_variant.storage_identifier)),
            "epub",
        )

        downloaded = client.get(
            f"/library/books/{book.id}/download/?variant_id={output_variant.id}",
        )
        assert downloaded.status_code == 200
        downloaded_stream = cast(StreamingHttpResponse, downloaded)
        assert (
            b"".join(cast(Iterable[bytes], downloaded_stream.streaming_content))
            == storage.resolve_location(
                output_variant.storage_identifier,
            ).read_bytes()
        )
    finally:
        redis_connection.flushdb()
        redis_connection.close()


@pytest.mark.django_db(transaction=True)
def test_member_calibre_conversions_produce_valid_pdf_and_mobi() -> None:
    pandoc_binary = shutil.which("pandoc")
    calibre_binary = shutil.which("ebook-convert")
    assert pandoc_binary is not None
    assert calibre_binary is not None

    storage_root = Path(os.environ["BOOK_SCANNER_STORAGE_ROOT"])
    django_settings.APP_DATA_DIR = storage_root
    storage = LocalFilesystemStorage(str(storage_root / "library-artifacts"))
    location = StorageLocation.objects.create(
        identifier="default",
        backend=StorageLocation.BackendType.LOCAL,
        base_path="default",
    )
    profile = get_or_create_profile("calibre-member")
    book = Book.objects.create(title="Pinned Calibre Acceptance Book")
    source_text = storage_root / "calibre-source.txt"
    source_epub = storage_root / "calibre-source.epub"
    source_text.write_text(
        "# Pinned Calibre Acceptance Book\n\n"
        "This EPUB becomes both a PDF and a MOBI through real Calibre workers.\n",
        encoding="utf-8",
    )
    generated = subprocess.run(
        [
            pandoc_binary,
            str(source_text),
            "--metadata",
            f"title={book.title}",
            "-o",
            str(source_epub),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert generated.returncode == 0, generated.stdout + generated.stderr
    source_payload = source_epub.read_bytes()
    source_identifier = storage.generate_destination(
        f"books/{book.id}/incoming",
        "source.epub",
    )
    storage.store(str(source_epub), source_identifier)
    source_variant = FileVariant.objects.create(
        book=book,
        format="epub",
        mime_type="application/epub+zip",
        original_filename="source.epub",
        storage_location=location,
        storage_identifier=source_identifier,
        size_bytes=len(source_payload),
        sha256=hashlib.sha256(source_payload).hexdigest(),
        source_type=FileVariant.SourceType.UPLOAD,
        uploader=profile.user,
        imported_at=timezone.now(),
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )

    redis_connection = Redis.from_url(os.environ["BOOK_SCANNER_REDIS_URL"])
    redis_connection.flushdb()
    try:
        client = Client()
        client.force_login(profile.user)
        outputs: dict[str, FileVariant] = {}
        for target_format in ("pdf", "mobi"):
            requested = client.post(
                f"/library/books/{book.id}/convert/",
                data=json.dumps(
                    {
                        "source_variant_id": source_variant.id,
                        "target_format": target_format,
                    },
                ),
                content_type="application/json",
            )
            assert requested.status_code == 202
            job = ProcessingJob.objects.get(job_id=requested.json()["job_id"])
            assert job.state == ProcessingJob.JobState.QUEUED

            queue = get_queue()
            assert isinstance(queue, ConfiguredQueue)
            _run_rq_worker(
                queue.queue.name,
                environment=_worker_environment_for_test_database(),
            )

            job.refresh_from_db()
            assert job.state == ProcessingJob.JobState.SUCCESS, job.details
            output_variant = FileVariant.objects.get(pk=job.details["output_variant_id"])
            outputs[target_format] = output_variant
            assert output_variant.book_id == book.id
            assert output_variant.source_variant_id == source_variant.id
            assert output_variant.source_type == FileVariant.SourceType.CONVERTED
            assert output_variant.format == target_format
            assert output_variant.converter_tool == "calibre"
            assert output_variant.converter_version == "ebook-convert (calibre 9.11.0)"
            assert output_variant.job_id == job.job_id
            output_path = storage.resolve_location(output_variant.storage_identifier)
            intake.validate_container_signature(str(output_path), target_format)
            assert hashlib.sha256(output_path.read_bytes()).hexdigest() == output_variant.sha256

        downloaded = client.get(
            f"/library/books/{book.id}/download/?variant_id={outputs['mobi'].id}",
        )
        assert downloaded.status_code == 200
        downloaded_stream = cast(StreamingHttpResponse, downloaded)
        assert (
            b"".join(cast(Iterable[bytes], downloaded_stream.streaming_content))
            == storage.resolve_location(
                outputs["mobi"].storage_identifier,
            ).read_bytes()
        )
    finally:
        redis_connection.flushdb()
        redis_connection.close()


@pytest.mark.django_db(transaction=True)
def test_readiness_uses_live_postgresql_redis_and_storage(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        response = Client().get("/healthz/dependencies/")

    assert response.status_code == 200
    payload = response.json()
    dependencies = {item["name"]: item["status"] for item in payload["dependencies"]}
    assert payload["status"] == "ready"
    assert dependencies == {"database": "ok", "storage": "ok", "queue": "ok"}


@pytest.mark.django_db(transaction=True)
def test_authenticated_sse_streams_through_nginx_and_uvicorn() -> None:
    profile = get_or_create_profile("sse-proxy-member")
    job = ScanJob.objects.create(
        operator=profile.user,
        scanner_id="sse-proxy-test",
        provisional_title="SSE proxy acceptance",
    )
    page = ScanPage.objects.create(
        scan_job=job,
        logical_sequence_number=1,
        side=ScanPage.Side.SINGLE,
    )
    stage_state = ScanTailorStageState.objects.create(
        scan_job=job,
        scan_page=page,
        stage=ScanTailorStageState.Stage.PAGE_SPLIT,
        status=ScanTailorStageState.StageStatus.QUEUED,
        revision=1,
    )
    django_client = Client()
    django_client.force_login(profile.user)
    session_cookie = django_client.cookies[django_settings.SESSION_COOKIE_NAME].value

    proxy_port = os.environ["BOOK_SCANNER_INTEGRATION_PROXY_PORT"]
    proxy_base_url = f"http://127.0.0.1:{proxy_port}"
    event_path = f"/scan/jobs/{job.id}/workspace/events/"
    with _running_uvicorn() as uvicorn_process:
        _wait_for_http(f"{proxy_base_url}/healthz/", uvicorn_process)

        unauthorized = httpx.get(
            f"{proxy_base_url}{event_path}",
            follow_redirects=False,
            timeout=3,
        )
        assert unauthorized.status_code == 302
        assert unauthorized.headers["location"].startswith("/accounts/login/")

        started_at = time.monotonic()
        with httpx.Client(
            cookies={django_settings.SESSION_COOKIE_NAME: session_cookie},
            timeout=httpx.Timeout(5.0, read=3.0),
        ).stream("GET", f"{proxy_base_url}{event_path}") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["cache-control"] == "no-cache, no-transform"
            assert response.headers["x-accel-buffering"] == "no"

            lines = response.iter_lines()
            event_name, initial_payload = _next_sse_event(lines)
            assert time.monotonic() - started_at < 2.0
            assert event_name == "workspace"
            assert initial_payload["job_id"] == str(job.id)
            initial_stages = initial_payload["stages"]
            assert isinstance(initial_stages, list)
            assert initial_stages[0]["revision"] == 1

            stage_state.status = ScanTailorStageState.StageStatus.RUNNING
            stage_state.revision = 2
            stage_state.save(update_fields=["status", "revision", "updated_at"])

            event_name, updated_payload = _next_sse_event(lines)
            assert event_name == "workspace"
            updated_stages = updated_payload["stages"]
            assert isinstance(updated_stages, list)
            assert updated_stages[0]["revision"] == 2
            assert updated_stages[0]["status"] == ScanTailorStageState.StageStatus.RUNNING

        assert httpx.get(f"{proxy_base_url}/healthz/", timeout=3).status_code == 200
