"""Offline coverage of the site acceptance kit (#162).

The library journey and the archive-write failure drill run end to end against the
real Django application through an in-process WSGI transport (real CSRF, session and
review views), with inline jobs, a temporary archive and a stub converter. The site
run replaces those with the deployment's front end, workers, mount and converter.
"""

from __future__ import annotations

import json
import shutil
import zipfile
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from django.conf import settings as django_settings
from django.core.management import call_command
from django.core.management.base import CommandError
from django.core.wsgi import get_wsgi_application

from ebook_server.core import intake
from ebook_server.core import site_acceptance as sa
from ebook_server.core.models import (
    FileVariant,
    ImportJob,
    ReviewTask,
    SourceLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

REHEARSAL_OUTPUT = """\
Rehearsing migrations: source book_scanner -> scratch book_scanner_rehearsal_1
==> pg_dump source (read-only)
==> PASS: pg_dump source (read-only) (1.250s)
==> PASS: manage.py migrate (42.017s)

=== Migration rehearsal report ===
source database:  book_scanner
scratch database: book_scanner_rehearsal_1 (same-server)
PASS  pg_dump source (read-only)             (1.250s)
PASS  createdb scratch                       (0.300s)
PASS  pg_restore into scratch                (3.900s)
PASS  pre-migration snapshot                 (0.800s)
PASS  manage.py migrate                      (42.017s)
PASS  migrate --check (nothing unapplied)    (0.700s)
PASS  makemigrations --check                 (0.900s)
PASS  scan job integrity + sample URLs       (2.100s)
PASS  total                                  (52.150s)
RESULT: PASS
dropped scratch database book_scanner_rehearsal_1
"""


def _recorder(tmp_path: Path) -> tuple[sa.Report, sa.Recorder]:
    report = sa.Report(tmp_path / "acceptance.json")
    run = report.start_run("run-1", {"revision": {"commit": "abc123"}})
    return report, sa.Recorder(report, run, echo=lambda _line: None)


# --- Report and recorder ---------------------------------------------------------


def test_recorder_keeps_every_outcome_and_never_upgrades_unknowns(tmp_path: Path) -> None:
    report, recorder = _recorder(tmp_path)
    with recorder.check("ok") as details:
        details["sha256"] = "f" * 64
    with recorder.check("skipped"):
        raise sa.CheckNotRun("precondition missing")
    with recorder.check("contradicted"):
        sa.expect(False, "observed something else")
    with recorder.check("crashed"):
        raise RuntimeError("boom")
    with recorder.check("unconfirmed") as details:
        details["_incomplete"] = "nobody answered"
    recorder.artifact(sa.Artifact("ok", "file", "f" * 64, 3, 7))

    statuses = {name: result.status for name, result in recorder.results.items()}
    assert statuses == {
        "ok": sa.PASS,
        "skipped": sa.NOT_RUN,
        "contradicted": sa.FAIL,
        "crashed": sa.FAIL,
        "unconfirmed": sa.INCOMPLETE,
    }
    assert recorder.results["crashed"].error == "RuntimeError: boom"
    assert recorder.finish() == sa.FAIL

    saved = json.loads(report.path.read_text("utf-8"))
    assert saved["runs"][0]["status"] == sa.FAIL
    assert [check["name"] for check in saved["runs"][0]["checks"]] == list(statuses)
    markdown = report.markdown_path.read_text("utf-8")
    assert "## Run run-1: FAIL" in markdown
    assert "| contradicted | FAIL |" in markdown
    assert f"`{'f' * 64}`" in markdown

    # A second invocation appends to the same report instead of replacing it.
    again = sa.Report(report.path)
    again.start_run("run-2", {})
    assert [run["run_id"] for run in json.loads(report.path.read_text())["runs"]] == [
        "run-1",
        "run-2",
    ]


@pytest.mark.parametrize(
    ("statuses", "expected"),
    [
        ([sa.PASS, sa.PASS], sa.PASS),
        ([sa.PASS, sa.NOT_RUN], sa.INCOMPLETE),
        ([sa.PASS, sa.INCOMPLETE], sa.INCOMPLETE),
        ([sa.NOT_RUN, sa.FAIL], sa.FAIL),
        ([], sa.INCOMPLETE),
    ],
)
def test_run_status_is_pass_only_when_every_check_passed(
    statuses: list[str], expected: str
) -> None:
    assert sa.run_status({"checks": [{"status": status} for status in statuses]}) == expected


def test_report_refuses_a_foreign_json_file(tmp_path: Path) -> None:
    path = tmp_path / "other.json"
    path.write_text('{"something": "else"}')
    with pytest.raises(ValueError, match="not a site acceptance report"):
        sa.Report(path)


def test_generated_collection_is_unique_per_run_and_role() -> None:
    token, books = sa.generate_collection("20261125T100000Z-abc123")
    other_token, other = sa.generate_collection("20261125T100000Z-def456")
    assert token == "saabc123" and other_token == "sadef456"
    assert set(books) == set(sa.BOOK_ROLES)
    hashes = {book.sha256 for book in [*books.values(), *other.values()]}
    assert len(hashes) == 2 * len(sa.BOOK_ROLES)
    assert all(token in book.title and token in book.filename for book in books.values())


# --- Migration rehearsal ------------------------------------------------------------


def test_rehearsal_report_parsing_reads_only_the_final_report() -> None:
    parsed = sa.parse_rehearsal_report(REHEARSAL_OUTPUT)
    assert parsed["result"] == "PASS"
    assert parsed["source_database"] == "book_scanner"
    assert parsed["migrate_seconds"] == pytest.approx(42.017)
    assert parsed["total_seconds"] == pytest.approx(52.15)
    assert len(parsed["steps"]) == 9  # the "==> PASS:" progress lines are not counted
    assert sa.parse_rehearsal_report("could not parse the database URLs\n")["result"] is None


@pytest.mark.django_db
@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize(("exit_status", "expected"), [(0, sa.PASS), (1, sa.FAIL)])
def test_rehearsal_runs_the_script_and_records_its_timings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exit_status: int, expected: str
) -> None:
    output = tmp_path / "output.txt"
    output.write_text(
        REHEARSAL_OUTPUT if exit_status == 0 else REHEARSAL_OUTPUT.replace("RESULT: PASS", "")
    )
    scripts = tmp_path / "checkout" / "scripts"
    scripts.mkdir(parents=True)
    script = scripts / "rehearse_migrations.sh"
    # Shell builtins only: the fake must not depend on the test runner's PATH.
    script.write_text(
        f'while IFS= read -r line; do echo "$line"; done < "{output.as_posix()}"\n'
        f'echo "args=$*"\nexit {exit_status}\n',
        newline="\n",
    )
    monkeypatch.setenv("SOURCE_DATABASE_URL", "postgresql://reader@db/book_scanner")
    monkeypatch.setenv("ADMIN_DATABASE_URL", "postgresql://admin@db/postgres")
    _report, recorder = _recorder(tmp_path)

    sa.run_migration_rehearsal(
        recorder, log_path=tmp_path / "rehearsal.log", sample=5, script=script
    )

    result = recorder.results["migration-rehearsal"]
    assert result.status == expected
    assert result.details["exit_status"] == exit_status
    assert result.details["wall_seconds"] >= 0
    assert "args=--sample 5" in (tmp_path / "rehearsal.log").read_text()
    if expected == sa.PASS:
        assert result.details["migrate_seconds"] == pytest.approx(42.017)
        assert result.details["pending_migrations"] == []


def test_rehearsal_without_database_urls_is_not_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SOURCE_DATABASE_URL", raising=False)
    _report, recorder = _recorder(tmp_path)
    sa.run_migration_rehearsal(recorder, log_path=tmp_path / "log")
    assert recorder.results["migration-rehearsal"].status == sa.NOT_RUN


# --- Alerting -------------------------------------------------------------------------


class FakeAlertmanager:
    """Alertmanager v2 API subset: an alert is active until its endsAt has passed."""

    def __init__(self) -> None:
        self.posts: list[list[dict[str, Any]]] = []
        self.alerts: dict[str, dict[str, Any]] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/status":
            return httpx.Response(
                200, json={"versionInfo": {"version": "0.28.1"}, "cluster": {"status": "ready"}}
            )
        if request.method == "POST" and request.url.path == "/api/v2/alerts":
            payload = json.loads(request.content)
            self.posts.append(payload)
            for alert in payload:
                self.alerts[alert["labels"]["acceptance_run"]] = alert
            return httpx.Response(200)
        assert request.url.path == "/api/v2/alerts"
        filters = request.url.params.get_list("filter")
        assert f'alertname="{sa.TEST_ALERT_NAME}"' in filters
        now = datetime.now(tz=UTC)
        active = [
            {**alert, "status": {"state": "active"}}
            for alert in self.alerts.values()
            if datetime.fromisoformat(alert["endsAt"].replace("Z", "+00:00")) > now
        ]
        return httpx.Response(200, json=active)


@pytest.mark.parametrize(
    ("answers", "expected"),
    [
        ([True, True], sa.PASS),
        ([None, None], sa.INCOMPLETE),
        ([True, False], sa.FAIL),
    ],
)
def test_alert_check_fires_resolves_and_needs_operator_confirmation(
    tmp_path: Path, answers: list[bool | None], expected: str
) -> None:
    manager = FakeAlertmanager()
    questions: list[str] = []

    def confirm(question: str) -> bool | None:
        questions.append(question)
        return answers[len(questions) - 1]

    _report, recorder = _recorder(tmp_path)
    sa.run_alert_test(
        recorder,
        "run-1",
        alertmanager_url="https://alertmanager.example",
        confirm=confirm,
        transport=httpx.MockTransport(manager),
        timeout=5,
        interval=0.01,
    )

    result = recorder.results["alert-delivery"]
    assert result.status == expected
    assert len(manager.posts) == 2
    fired, resolved = (post[0] for post in manager.posts)
    assert fired["labels"]["alertname"] == sa.TEST_ALERT_NAME
    assert fired["endsAt"] > fired["startsAt"]
    assert resolved["endsAt"] <= datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")
    assert "FIRING" in questions[0] and "RESOLVED" in questions[1]
    assert result.details["alertmanager_version"] == "0.28.1"


def test_alert_is_resolved_even_when_it_never_shows_up(tmp_path: Path) -> None:
    class Blackhole(FakeAlertmanager):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.method == "GET" and request.url.path == "/api/v2/alerts":
                return httpx.Response(200, json=[])
            return super().__call__(request)

    manager = Blackhole()
    _report, recorder = _recorder(tmp_path)
    sa.run_alert_test(
        recorder,
        "run-1",
        alertmanager_url="https://alertmanager.example",
        confirm=lambda _question: True,
        transport=httpx.MockTransport(manager),
        timeout=0.05,
        interval=0.01,
    )
    assert recorder.results["alert-delivery"].status == sa.FAIL
    assert "become active" in recorder.results["alert-delivery"].error
    assert len(manager.posts) == 2  # the cleanup post ended the test alert


@pytest.mark.parametrize(("value", "expected"), [("1", sa.PASS), ("0", sa.FAIL)])
def test_scrape_check_reads_the_up_series(tmp_path: Path, value: str, expected: str) -> None:
    def prometheus(request: httpx.Request) -> httpx.Response:
        assert request.url.params["query"] == 'up{job="book-scanner"}'
        result = [{"metric": {"instance": "library:443"}, "value": [0, value]}]
        return httpx.Response(200, json={"status": "success", "data": {"result": result}})

    _report, recorder = _recorder(tmp_path)
    sa.run_scrape_check(
        recorder,
        prometheus_url="https://prometheus.example",
        transport=httpx.MockTransport(prometheus),
    )
    assert recorder.results["metrics-scrape"].status == expected


# --- Library journey and archive-write failure ----------------------------------------


def _minimal_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="content.opf" media-type="application/oebps-package+xml"/>'
            "</rootfiles></container>",
        )
        archive.writestr("content.opf", "<package/>")


class FakePandoc:
    name = "pandoc"
    binary = "pandoc"

    def is_available(self) -> bool:
        return True

    def version(self) -> str:
        return "pandoc fake-1"

    def convert(self, *, destination_path: Path, **_kwargs: Any) -> None:
        _minimal_epub(destination_path)


@pytest.fixture
def site(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[sa.LibraryConfig]:
    data = tmp_path / "data"
    data.mkdir()
    share = tmp_path / "share"
    share.mkdir()
    monkeypatch.setattr(django_settings, "APP_DATA_DIR", data)
    monkeypatch.setattr("ebook_server.core.identification._provider_instances", lambda: [])
    monkeypatch.setattr("ebook_server.core.conversion.CONVERTER_REGISTRY", {"pandoc": FakePandoc()})
    get_or_create_profile("acceptor", role="admin")
    transport = httpx.WSGITransport(app=get_wsgi_application())
    yield sa.LibraryConfig(
        base_url="http://testserver",
        username="acceptor",
        password="stub-ldap-accepts-this",
        scratch_dir=share,
        job_timeout=5,
        poll_interval=0.01,
        transport=transport,
    )


def _run(tmp_path: Path, config: sa.LibraryConfig, **flags: bool) -> sa.Recorder:
    report, recorder = _recorder(tmp_path)
    sa.SiteAcceptance(recorder, config, "20261125T100000Z-abc123", **flags).run()
    recorder.finish()
    return recorder


@pytest.mark.django_db(transaction=True)
def test_library_journey_records_every_route_with_checksums(
    tmp_path: Path, site: sa.LibraryConfig
) -> None:
    recorder = _run(tmp_path, site, library=True, archive_failure=False)

    results = recorder.results
    failures = {name: r.error for name, r in results.items() if r.status != sa.PASS}
    assert failures == {}
    assert list(results) == ["sign-in", *sa.LIBRARY_CHECKS]
    assert results["deduplication"].details["variants_with_this_sha256"] == 1
    assert results["move"].details["source_exists_after_import"] is False
    assert results["copy"].details["source_exists_after_import"] is True
    assert results["incoming-discovery"].details["configured_incoming_source"] is False
    assert results["conversion"].details["converter_version"] == "pandoc fake-1"
    assert results["authenticated-download"].details["anonymous_status"] == 302
    assert results["authenticated-download"].details["downloaded_variants"] == 2
    roles = [artifact["role"] for artifact in recorder.run["artifacts"]]
    assert roles == [
        "uploaded test book",
        "archived upload",
        "archived incoming import",
        "archived copy import",
        "archived move import",
        "converted epub",
    ]
    # Scratch sources never join later ingest runs.
    assert not SourceLocation.objects.filter(is_active=True).exists()
    assert not ReviewTask.objects.filter(state=ReviewTask.State.OPEN).exists()


@pytest.mark.django_db(transaction=True)
def test_archive_write_failure_keeps_move_source_and_retry_leaves_one_copy(
    tmp_path: Path, site: sa.LibraryConfig
) -> None:
    recorder = _run(tmp_path, site, library=False, archive_failure=True)

    result = recorder.results[sa.ARCHIVE_FAILURE_CHECK]
    assert result.status == sa.PASS, result.error
    after_failure = result.details["after_failure"]
    assert after_failure["state"] == ImportJob.State.FAILED
    assert after_failure["source_exists"] is True
    assert 0 < after_failure["partial_archive_bytes"]
    assert "simulated archive write failure" in after_failure["error"]
    assert result.details["after_retry"]["state"] == ImportJob.State.LOW_CONFIDENCE
    assert len(result.details["archive_files_after_retry"]) == 1
    assert result.details["pending_folder_leftovers"] == []
    job = ImportJob.objects.get(pk=result.details["import_job_id"])
    assert job.state == ImportJob.State.COMPLETE
    assert not Path(job.source_path).exists()
    assert FileVariant.objects.filter(sha256=job.sha256).count() == 1
    # The fault was confined to the drill: the real backend is back in place.
    assert type(intake._default_storage()).__name__ == "LocalFilesystemStorage"


@pytest.mark.django_db(transaction=True)
def test_archive_failure_drill_fails_when_the_move_source_is_lost(
    tmp_path: Path, site: sa.LibraryConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    class LosingStorage(LocalFilesystemStorage):
        def store(self, source: str, destination: str) -> str:
            self._prepare_destination(destination).write_bytes(b"x")
            Path(source).unlink()
            raise sa.SimulatedArchiveWriteError("source lost during the archive write")

    monkeypatch.setattr(sa, "failing_archive_storage", LosingStorage)
    recorder = _run(tmp_path, site, library=False, archive_failure=True)

    result = recorder.results[sa.ARCHIVE_FAILURE_CHECK]
    assert result.status == sa.FAIL
    assert "MOVE source was deleted" in result.error


@pytest.mark.django_db(transaction=True)
def test_failed_sign_in_marks_everything_not_run(tmp_path: Path, site: sa.LibraryConfig) -> None:
    site.password = ""
    recorder = _run(tmp_path, site, library=True, archive_failure=True)
    assert recorder.results["sign-in"].status == sa.NOT_RUN
    assert {r.status for name, r in recorder.results.items() if name != "sign-in"} == {sa.NOT_RUN}


@pytest.mark.django_db(transaction=True)
def test_scratch_dir_inside_the_archive_is_refused(tmp_path: Path, site: sa.LibraryConfig) -> None:
    inside = Path(django_settings.APP_DATA_DIR) / "library-artifacts" / "x"
    inside.mkdir(parents=True)
    site.scratch_dir = inside
    recorder = _run(tmp_path, site, library=True, archive_failure=False)
    assert "archive root" in recorder.results["sign-in"].error


@pytest.mark.django_db
def test_command_without_site_settings_records_not_run_and_exits_incomplete(
    tmp_path: Path,
) -> None:
    report = tmp_path / "site.json"
    with pytest.raises(CommandError) as raised:
        call_command(
            "site_acceptance",
            "--report",
            str(report),
            "--check",
            "library",
            "--check",
            "alert",
            "--no-prompt",
        )
    assert raised.value.returncode == 3
    run = json.loads(report.read_text())["runs"][0]
    assert run["status"] == sa.INCOMPLETE
    assert run["revision"]["commit"]
    assert {check["status"] for check in run["checks"]} == {sa.NOT_RUN}
    assert "SITE_ACCEPTANCE_PASSWORD" not in report.read_text()
