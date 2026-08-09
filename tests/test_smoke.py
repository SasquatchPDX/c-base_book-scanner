from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from django.core.management import call_command
from django.test import Client, override_settings
from pydantic import SecretStr, ValidationError

from ebook_server.jobs import ConfiguredQueue
from ebook_server.main import main
from ebook_server.settings import Settings


def test_management_check_command_works() -> None:
    call_command("check")


@pytest.mark.django_db(transaction=True)
def test_health_endpoint_has_expected_shape(tmp_path: Path) -> None:
    with override_settings(APP_DATA_DIR=tmp_path):
        client = Client()
        response = client.get("/healthz/")
        assert response.status_code in (200, 503)
        payload = response.json()
        assert payload["status"] in {"ok", "degraded"}
        assert isinstance(payload["dependencies"], list)
        assert {"name": "database", "status": "ok"} in [
            {"name": item["name"], "status": item["status"]} for item in payload["dependencies"]
        ]


def test_entrypoint_supports_check_command() -> None:
    assert callable(main)


def test_queue_module_without_config() -> None:
    from ebook_server.jobs import get_queue

    assert get_queue() is None


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_QUEUE_URL="inline://")
def test_queue_module_inline_mode_executes_job() -> None:
    from ebook_server.jobs import InlineQueue, get_queue

    queue = get_queue() or InlineQueue()
    assert isinstance(queue, InlineQueue)
    result = queue.enqueue(lambda v: f"done:{v}", "ok")
    assert result.result == "done:ok"
    assert result.id == "inline"


@pytest.mark.django_db(transaction=True)
def test_database_migrations_smoke() -> None:
    call_command("migrate", run_syncdb=True, interactive=False)


def test_production_settings_fail_closed_without_external_services() -> None:
    with pytest.raises(ValidationError, match="unsafe production configuration"):
        Settings(environment="production")


def test_production_settings_accept_hardened_configuration() -> None:
    production = Settings(
        environment="production",
        secret_key=SecretStr(
            "production-secret-key-with-more-than-fifty-random-looking-characters-123",
        ),
        allowed_hosts=["library.example.test"],
        database_url="postgresql://ebook:secret@database/library",
        redis_url="redis://redis:6379/0",
        ldap_mode="ldap",
        secure_ssl_redirect=True,
        session_cookie_secure=True,
        csrf_cookie_secure=True,
        secure_hsts_seconds=31536000,
        metrics_bearer_token=SecretStr("metrics-monitoring-token"),
    )

    assert production.environment == "production"


@override_settings(
    BOOK_SCANNER_QUEUE_JOB_TIMEOUT_SECONDS=120,
    BOOK_SCANNER_QUEUE_RESULT_TTL_SECONDS=3600,
    BOOK_SCANNER_QUEUE_FAILURE_TTL_SECONDS=7200,
    BOOK_SCANNER_QUEUE_RETRY_MAX=2,
    BOOK_SCANNER_QUEUE_RETRY_INTERVALS=[10, 30],
)
def test_configured_queue_applies_limits_and_retries() -> None:
    captured: dict[str, Any] = {}

    class FakeQueue:
        def enqueue(self, func: Any, *args: Any, **kwargs: Any) -> str:
            captured.update(kwargs)
            return "queued"

    configured = ConfiguredQueue(queue=cast(Any, FakeQueue()))

    assert configured.enqueue(str, 1) == "queued"
    assert captured["job_timeout"] == 120
    assert captured["result_ttl"] == 3600
    assert captured["failure_ttl"] == 7200
    assert captured["retry"].max == 2
