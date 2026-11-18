"""End-to-end run of ``scripts/rehearse_migrations.sh`` against the CI PostgreSQL.

A source database is created at the pre-0041 state (``core.0040_cover_thumbnail``, UUID
scan job keys) and seeded with scan jobs, pages, todos and ScanTailor stage states,
including Page Split rows that migration 0049 retires. The script then dumps it,
restores it into a scratch database, migrates the copy to the latest state and checks
it. The test asserts a PASS report, that the source database was left at 0040 and that
the scratch database was dropped.

The PostgreSQL client tools must match the server (17). When the host has none of that
version, the tools run from the pinned server image through ``docker run``.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires the pinned Docker Compose integration environment",
    ),
]

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = PROJECT_ROOT / "scripts" / "rehearse_migrations.sh"
PG_TOOLS = ("pg_dump", "pg_restore", "createdb", "dropdb")

SEED = """
import uuid
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from tests.test_scanjob_string_pk_migration import BEFORE, _seed

executor = MigrationExecutor(connection)
executor.migrate(BEFORE)
apps = executor.loader.project_state(BEFORE).apps
_seed(apps)
Job = apps.get_model("core", "ScanJob")
Page = apps.get_model("core", "ScanPage")
Todo = apps.get_model("core", "ScanTodo")
Stage = apps.get_model("core", "ScanTailorStageState")
jobs = Job.objects.bulk_create([Job(id=uuid.uuid4()) for _ in range(40)])
pages = Page.objects.bulk_create(
    [
        Page(id=uuid.uuid4(), scan_job=job, logical_sequence_number=number)
        for job in jobs
        for number in range(3)
    ]
)
Todo.objects.bulk_create([Todo(scan_job_id=p.scan_job_id, scan_page=p) for p in pages[::2]])
# Every third page has a Page Split row that feeds its Deskew row. Migration 0049 deletes
# exactly these (#195); the integrity check must accept that and nothing more.
splits = {
    p.id: Stage.objects.create(scan_job_id=p.scan_job_id, scan_page=p, stage="page_split")
    for p in pages[::3]
}
Stage.objects.bulk_create(
    [
        Stage(
            scan_job_id=p.scan_job_id,
            scan_page=p,
            stage="deskew",
            input_stage_state=splits.get(p.id),
        )
        for p in pages
    ]
)
"""


def _with_database(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, "/" + name, parts.query, ""))


def _postgres_image() -> str:
    compose = (PROJECT_ROOT / "compose.integration.yml").read_text(encoding="utf-8")
    match = re.search(r"image:\s*(postgres:\S+)", compose)
    assert match, "compose.integration.yml has no postgres image"
    return match.group(1)


def _pg_bindir(tmp_path: Path, work_dir: Path) -> str:
    configured = os.environ.get("BOOK_SCANNER_REHEARSAL_PG_BINDIR")
    if configured:
        return configured
    if shutil.which("docker") is None:
        pytest.fail("docker is required to run PostgreSQL 17 client tools")
    bindir = tmp_path / "pg-bin"
    bindir.mkdir()
    image = _postgres_image()
    for tool in PG_TOOLS:
        wrapper = bindir / tool
        wrapper.write_text(
            "#!/bin/sh\n"
            "exec docker run --rm --network host -e PGPASSWORD -e PGOPTIONS "
            f'-v "{work_dir}:{work_dir}" "{image}" {tool} "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IXUSR)
    return str(bindir)


def _database_exists(admin_url: str, name: str) -> bool:
    with psycopg.connect(admin_url, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


def _subprocess_env(**overrides: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_VERSION"}
    env.update(overrides)
    return env


def test_rehearse_migrations_script_passes_on_a_pre_0041_copy(tmp_path: Path) -> None:
    base_url = os.environ["BOOK_SCANNER_DATABASE_URL"]
    admin_url = _with_database(base_url, "postgres")
    suffix = uuid.uuid4().hex[:10]
    source_name = f"rehearsal_source_{suffix}"
    scratch_name = f"rehearsal_scratch_{suffix}"
    source_url = _with_database(base_url, source_name)
    work_dir = tmp_path / "work"
    work_dir.mkdir()
    pg_bindir = _pg_bindir(tmp_path, work_dir)

    with psycopg.connect(admin_url, autocommit=True) as conn:
        conn.execute(f'CREATE DATABASE "{source_name}"')
    try:
        seeded = subprocess.run(
            ["uv", "run", "python", "manage.py", "shell", "-c", SEED],
            cwd=PROJECT_ROOT,
            env=_subprocess_env(BOOK_SCANNER_DATABASE_URL=source_url),
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert seeded.returncode == 0, seeded.stdout + seeded.stderr

        with psycopg.connect(source_url) as conn:
            (jobs_before,) = conn.execute("SELECT count(*) FROM core_scanjob").fetchone() or (0,)
            (applied,) = conn.execute(
                "SELECT max(name) FROM django_migrations WHERE app = 'core'"
            ).fetchone() or ("",)
        assert jobs_before == 42
        assert applied == "0040_cover_thumbnail"

        result = subprocess.run(
            [str(SCRIPT), "--scratch-name", scratch_name, "--sample", "10"],
            cwd=PROJECT_ROOT,
            env=_subprocess_env(
                SOURCE_DATABASE_URL=source_url,
                ADMIN_DATABASE_URL=admin_url,
                PG_BINDIR=pg_bindir,
                REHEARSAL_WORK_DIR=str(work_dir),
            ),
            capture_output=True,
            text=True,
            timeout=600,
        )
        output = result.stdout + result.stderr
        print(output)
        assert result.returncode == 0, output
        assert "RESULT: PASS" in output
        assert "0041_scanjob_string_pk" in output  # listed as pending
        assert "scan job IDs: 42 before, 42 after" in output
        # 2 (base seed) + 120 Deskew + 40 Page Split rows; only the Page Split rows go.
        assert "core_scantailorstagestate: 162 -> 122 (40 retired row(s) removed)" in output
        assert (
            "core_scantailorstagestate.scan_job_id: 162 -> 122 (40 retired row(s) removed)"
            in output
        )
        assert "sample scan-job URLs checked: 10" in output
        password = urlsplit(base_url).password
        assert password and password not in output

        # The source is untouched and the scratch copy is gone.
        with psycopg.connect(source_url) as conn:
            (still_applied,) = conn.execute(
                "SELECT max(name) FROM django_migrations WHERE app = 'core'"
            ).fetchone() or ("",)
            (jobs_after,) = conn.execute("SELECT count(*) FROM core_scanjob").fetchone() or (0,)
        assert still_applied == "0040_cover_thumbnail"
        assert jobs_after == jobs_before
        assert not _database_exists(admin_url, scratch_name)
        assert not any(work_dir.iterdir()), "the dump was not removed"

        refused = subprocess.run(
            [str(SCRIPT), "--scratch-name", source_name],
            cwd=PROJECT_ROOT,
            env=_subprocess_env(
                SOURCE_DATABASE_URL=source_url,
                ADMIN_DATABASE_URL=admin_url,
                PG_BINDIR=pg_bindir,
            ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert refused.returncode == 3, refused.stdout + refused.stderr
        assert "REFUSED" in refused.stderr
    finally:
        with psycopg.connect(admin_url, autocommit=True) as conn:
            for name in (scratch_name, source_name):
                conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
