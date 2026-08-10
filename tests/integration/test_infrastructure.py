from __future__ import annotations

import hashlib
import json
import operator
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

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

from ebook_server.core import intake
from ebook_server.core.auth import Ldap3Adapter, LdapAuthenticationError
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
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
def test_readiness_uses_live_postgresql_redis_and_storage(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        response = Client().get("/healthz/dependencies/")

    assert response.status_code == 200
    payload = response.json()
    dependencies = {item["name"]: item["status"] for item in payload["dependencies"]}
    assert payload["status"] == "ready"
    assert dependencies == {"database": "ok", "storage": "ok", "queue": "ok"}
