from __future__ import annotations

import configparser
import importlib.util
import re
from pathlib import Path

from django.conf import settings

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = PROJECT_ROOT / "deploy" / "systemd"


def _unit(name: str) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    parser.read_string((SYSTEMD_DIR / name).read_text(encoding="utf-8"))
    return parser


def test_web_unit_runs_the_asgi_application_privately() -> None:
    service = _unit("book-scanner-web.service")["Service"]
    exec_start = service["ExecStart"]

    assert "uvicorn ebook_server.asgi:application" in exec_start
    assert importlib.util.find_spec("ebook_server.asgi") is not None
    assert "--host 127.0.0.1" in exec_start
    assert "--reload" not in exec_start
    assert service["User"] != "root"
    assert service["WorkingDirectory"]


def test_worker_unit_runs_rq_with_scheduler_and_django_bootstrap() -> None:
    unit = _unit("book-scanner-worker@.service")
    service = unit["Service"]
    exec_start = service["ExecStart"]

    assert "rq worker" in exec_start
    assert "--with-scheduler" in exec_start
    assert "--config ebook_server.rq_settings" in exec_start
    assert importlib.util.find_spec("ebook_server.rq_settings") is not None
    assert exec_start.split()[-1] == settings.BOOK_SCANNER_QUEUE_NAME
    assert service["KillMode"] == "mixed"
    assert "QT_QPA_PLATFORM=offscreen" in service["Environment"]
    assert service["User"] != "root"


def test_nginx_example_serves_static_url_from_collected_files() -> None:
    config = (PROJECT_ROOT / "deploy" / "nginx" / "book-scanner.conf").read_text(
        encoding="utf-8",
    )
    static_prefix = "/" + settings.STATIC_URL.strip("/") + "/"

    assert re.search(rf"location {re.escape(static_prefix)} \{{\s*alias \S+/;", config)
