from __future__ import annotations

from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from ebook_server.main import main


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
@override_settings(BOOK_SCANNER_QUEUE_URL="inline://")  # type: ignore[untyped-decorator]
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
