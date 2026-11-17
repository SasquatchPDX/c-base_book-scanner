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


NGINX_DIR = PROJECT_ROOT / "deploy" / "nginx"
SCANNER_API_BLOCK = re.compile(r"location /api/scanner/ \{(?P<body>[^}]*)\}")


def _nginx_directives(name: str) -> str:
    """The config without comments, so commented examples do not count."""
    text = (NGINX_DIR / name).read_text(encoding="utf-8")
    return "\n".join(line.split("#", 1)[0] for line in text.splitlines())


def _server_blocks(config: str) -> list[str]:
    starts = [match.start() for match in re.finditer(r"^server \{", config, re.MULTILINE)]
    return [
        config[start:end] for start, end in zip(starts, [*starts[1:], len(config)], strict=True)
    ]


def test_plain_http_examples_never_serve_the_scanner_api() -> None:
    for name in ("book-scanner.conf", "integration.conf"):
        blocks = SCANNER_API_BLOCK.findall(_nginx_directives(name))
        assert [body.split() for body in blocks] == [["return", "403;"]], name


def test_scanner_tls_front_end_is_tls13_only_and_fails_closed() -> None:
    config = _nginx_directives("serif-lan-tls.conf")
    servers = _server_blocks(config)
    tls = [server for server in servers if re.search(r"listen \S*443 ssl;", server)]
    plain = [server for server in servers if server not in tls]

    assert len(tls) == 1
    assert len(plain) == 1
    assert re.findall(r"ssl_protocols ([^;]*);", config) == ["TLSv1.3"]
    # No upstream of its own: it reuses book-scanner.conf's.
    assert "upstream " not in config
    assert "book_scanner_asgi" in _nginx_directives("book-scanner.conf")
    for server in servers:
        assert "server_tokens off;" in server
        # Fail closed: only `deny all;` until the operator adds LAN ranges.
        assert re.findall(r"^\s*(allow|deny)\s+([^;]*);", server, re.MULTILINE) == [("deny", "all")]

    (tls_server,) = tls
    assert "client_max_body_size 150m;" in tls_server.split("location", 1)[0]
    (scanner_api,) = SCANNER_API_BLOCK.findall(tls_server)
    assert "client_max_body_size 64m;" in scanner_api
    assert "proxy_pass http://book_scanner_asgi;" in scanner_api
    assert "proxy_http_version 1.1;" in scanner_api
    assert "X-Forwarded-For" in scanner_api
    assert "X-Forwarded-Proto" in scanner_api

    (plain_server,) = plain
    assert [body.split() for body in SCANNER_API_BLOCK.findall(plain_server)] == [
        ["return", "403;"]
    ]
    # A bare `return` would run before allow/deny and answer outside hosts.
    root_location = re.search(r"location / \{([^}]*)\}", plain_server)
    assert root_location is not None
    assert "return" not in root_location.group(1)
