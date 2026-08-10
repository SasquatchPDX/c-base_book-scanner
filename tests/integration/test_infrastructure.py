from __future__ import annotations

import operator
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest
from django.contrib.auth import authenticate
from django.db import connection, transaction
from django.db.migrations.recorder import MigrationRecorder
from django.test import Client, override_settings
from redis import Redis
from rq.job import JobStatus

from ebook_server.core.auth import Ldap3Adapter, LdapAuthenticationError
from ebook_server.core.models import Book
from ebook_server.jobs import ConfiguredQueue, get_queue

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]


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

        rq_executable = Path(sys.executable).with_name("rq")
        worker = subprocess.run(
            [
                str(rq_executable),
                "worker",
                "--burst",
                "--url",
                queue_url,
                configured_queue.queue.name,
            ],
            cwd=Path(__file__).resolve().parents[2],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )

        assert worker.returncode == 0, worker.stdout + worker.stderr
        job.refresh()
        assert job.get_status(refresh=True) == JobStatus.FINISHED
        assert job.return_value() == 42
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
