"""Version 1 site acceptance on a configured deployment (#162).

``manage.py site_acceptance`` runs these checks on the application VM, with the
deployment's environment loaded, against the real web front end, database, workers,
storage mount, LDAP directory and alerting route. Every check writes what it observed
(revision, timings, job ids and states, artifact checksums) into one JSON report and a
Markdown rendering of it. Nothing is inferred: a check that did not run is recorded as
``NOT_RUN``, and one that needs an operator's answer it did not get is ``INCOMPLETE``.

The test collection is generated per run: small TXT books whose text and title carry a
unique run token, so they never collide with real books or with earlier runs.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import subprocess
import time
import traceback
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from typing import Any, Final
from unittest import mock

import httpx
from django.conf import settings
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor

from ebook_server.storage import LocalFilesystemStorage

from . import archive_layout, intake
from .models import FileVariant, ImportJob, ReviewTask, SourceLocation
from .storage_registry import archive_root, filesystem_backend_for_location

PASS: Final = "PASS"
FAIL: Final = "FAIL"
NOT_RUN: Final = "NOT_RUN"
INCOMPLETE: Final = "INCOMPLETE"

REPORT_SCHEMA: Final = 1
PROJECT_ROOT: Final = Path(__file__).resolve().parents[3]
REHEARSAL_SCRIPT: Final = PROJECT_ROOT / "scripts" / "rehearse_migrations.sh"
TEST_ALERT_NAME: Final = "BookScannerSiteAcceptanceTest"
SCRATCH_PREFIX: Final = "site-acceptance-"
TEST_AUTHOR: Final = "Site Acceptance"

# Import states a worker leaves a job in once it has let go of it (lease released).
SETTLED_IMPORT_STATES: Final = frozenset(
    {
        ImportJob.State.LOW_CONFIDENCE,
        ImportJob.State.COMPLETE,
        ImportJob.State.FAILED,
        ImportJob.State.CANCELLED,
    }
)

LIBRARY_CHECKS: Final = (
    "web-upload",
    "review",
    "deduplication",
    "incoming-discovery",
    "copy",
    "move",
    "search",
    "conversion",
    "authenticated-download",
)
ARCHIVE_FAILURE_CHECK: Final = "archive-write-failure"


class CheckFailed(Exception):
    """An observation contradicted the expected outcome."""


class CheckNotRun(Exception):
    """A precondition is missing, so the check did not run."""


class SimulatedArchiveWriteError(OSError):
    """Raised by the fault-injecting archive backend of the archive-failure check."""


@dataclass
class CheckResult:
    name: str
    status: str
    started_at: str
    duration_seconds: float
    details: dict[str, Any] = field(default_factory=dict)
    error: str = ""


@dataclass
class Artifact:
    check: str
    role: str
    sha256: str
    size_bytes: int | None = None
    file_variant_id: int | None = None


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def new_run_id() -> str:
    return f"{utc_now():%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def revision_info(project_root: Path = PROJECT_ROOT) -> dict[str, Any]:
    """The deployed code revision as git reports it, else BOOK_SCANNER_REVISION; never guessed."""

    def git(*args: str) -> str | None:
        try:
            result = subprocess.run(
                ["git", "-C", str(project_root), *args],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return result.stdout.strip() if result.returncode == 0 else None

    commit = git("rev-parse", "HEAD")
    if commit:
        status = git("status", "--porcelain", "--untracked-files=no")
        return {
            "commit": commit,
            "source": "git",
            "dirty": bool(status) if status is not None else None,
            "describe": git("describe", "--always", "--dirty", "--tags") or commit[:12],
        }
    from_environment = os.environ.get("BOOK_SCANNER_REVISION", "")
    return {
        "commit": from_environment or "unknown",
        "source": "environment" if from_environment else "unknown",
    }


def unapplied_migrations() -> list[str]:
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    return [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan]


def run_header() -> dict[str, Any]:
    return {
        "revision": revision_info(),
        "host": platform.node(),
        "environment": getattr(settings, "BOOK_SCANNER_ENVIRONMENT", ""),
        "ldap_mode": getattr(settings, "BOOK_SCANNER_LDAP_MODE", ""),
        "unapplied_migrations": unapplied_migrations(),
    }


# --- Report -----------------------------------------------------------------------


class Report:
    """One acceptance report file (JSON plus a Markdown rendering); runs are appended."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, Any] = {"schema": REPORT_SCHEMA, "issue": 162, "runs": []}
        if path.exists():
            loaded = json.loads(path.read_text("utf-8"))
            if loaded.get("schema") != REPORT_SCHEMA or not isinstance(loaded.get("runs"), list):
                raise ValueError(f"{path} is not a site acceptance report")
            self.data = loaded

    @property
    def markdown_path(self) -> Path:
        return self.path.with_suffix(".md")

    def start_run(self, run_id: str, header: dict[str, Any]) -> dict[str, Any]:
        run: dict[str, Any] = {
            "run_id": run_id,
            "started_at": utc_now().isoformat(),
            "finished_at": None,
            "status": None,
            **header,
            "checks": [],
            "artifacts": [],
        }
        self.data["runs"].append(run)
        self.save()
        return run

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        for target, text in (
            (self.path, json.dumps(self.data, indent=2) + "\n"),
            (self.markdown_path, render_markdown(self.data)),
        ):
            staged = target.with_name(f".{target.name}.tmp")
            staged.write_text(text, "utf-8")
            os.replace(staged, target)


def run_status(run: dict[str, Any]) -> str:
    statuses = [check["status"] for check in run["checks"]]
    if FAIL in statuses:
        return FAIL
    if statuses and all(status == PASS for status in statuses):
        return PASS
    return INCOMPLETE


def _summarize(details: dict[str, Any]) -> str:
    parts = [
        f"{key}={value}"
        for key, value in details.items()
        if not isinstance(value, dict | list) and key != "traceback_tail"
    ]
    return "; ".join(parts)[:500]


def render_markdown(data: dict[str, Any]) -> str:
    lines = ["# Version 1 site acceptance report (#162)", ""]
    for run in data["runs"]:
        revision = run.get("revision", {})
        dirty = " (uncommitted changes)" if revision.get("dirty") else ""
        lines += [
            f"## Run {run['run_id']}: {run.get('status') or run_status(run)}",
            "",
            f"- Revision: `{revision.get('commit', 'unknown')}`{dirty}",
            f"- Host: {run.get('host', '')}; environment: {run.get('environment', '')}; "
            f"authentication: {run.get('ldap_mode', '')}",
            f"- Started {run['started_at']}, finished {run.get('finished_at') or '(unfinished)'}",
            f"- Unapplied migrations at start: {len(run.get('unapplied_migrations', []))}",
            "",
            "| Check | Status | Duration | Observed |",
            "| --- | --- | --- | --- |",
        ]
        for check in run["checks"]:
            observed = check["error"] or _summarize(check["details"])
            observed = observed.replace("|", "\\|").replace("\n", " ")
            lines.append(
                f"| {check['name']} | {check['status']} | "
                f"{check['duration_seconds']:.1f}s | {observed} |"
            )
        if run["artifacts"]:
            lines += [
                "",
                "| Artifact | Check | Variant | Bytes | SHA-256 |",
                "| --- | --- | --- | --- | --- |",
            ]
            for artifact in run["artifacts"]:
                lines.append(
                    f"| {artifact['role']} | {artifact['check']} | "
                    f"{artifact.get('file_variant_id') or ''} | "
                    f"{artifact.get('size_bytes') or ''} | `{artifact['sha256']}` |"
                )
        lines.append("")
    return "\n".join(lines)


class Recorder:
    """Times checks and records their outcome in the current run of a report."""

    def __init__(
        self, report: Report, run: dict[str, Any], *, echo: Callable[[str], None] = print
    ) -> None:
        self.report = report
        self.run = run
        self.echo = echo
        self.results: dict[str, CheckResult] = {}

    @contextmanager
    def check(self, name: str) -> Iterator[dict[str, Any]]:
        details: dict[str, Any] = {}
        started = utc_now()
        clock = time.monotonic()
        status, error = PASS, ""
        self.echo(f"==> {name}")
        try:
            yield details
        except CheckNotRun as exc:
            status, error = NOT_RUN, str(exc)
        except CheckFailed as exc:
            status, error = FAIL, str(exc)
        except Exception as exc:  # every failure belongs in the report
            status, error = FAIL, f"{type(exc).__name__}: {exc}"
            details["traceback_tail"] = traceback.format_exc(limit=4)[-1500:]
        incomplete_reason = details.pop("_incomplete", None)
        if incomplete_reason and status == PASS:
            status, error = INCOMPLETE, str(incomplete_reason)
        result = CheckResult(
            name=name,
            status=status,
            started_at=started.isoformat(),
            duration_seconds=round(time.monotonic() - clock, 3),
            details=details,
            error=error,
        )
        self.results[name] = result
        self.run["checks"].append(asdict(result))
        self.report.save()
        self.echo(f"    {status} ({result.duration_seconds:.1f}s) {error}".rstrip())

    def not_run(self, name: str, reason: str) -> None:
        with self.check(name):
            raise CheckNotRun(reason)

    def passed(self, name: str) -> bool:
        result = self.results.get(name)
        return result is not None and result.status == PASS

    def artifact(self, artifact: Artifact) -> None:
        self.run["artifacts"].append(asdict(artifact))
        self.report.save()

    def finish(self) -> str:
        self.run["finished_at"] = utc_now().isoformat()
        self.run["status"] = run_status(self.run)
        self.report.save()
        return str(self.run["status"])


def expect(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def wait_for(
    probe: Callable[[], Any],
    *,
    timeout: float,
    interval: float,
    what: str,
) -> Any:
    """Return the first truthy ``probe()`` result, or fail after ``timeout`` seconds."""
    deadline = time.monotonic() + timeout
    while True:
        value = probe()
        if value:
            return value
        if time.monotonic() >= deadline:
            raise CheckFailed(f"timed out after {timeout:.0f}s waiting for {what}")
        time.sleep(interval)


# --- Test collection --------------------------------------------------------------


@dataclass(frozen=True)
class AcceptanceBook:
    role: str
    title: str
    filename: str
    payload: bytes

    @property
    def sha256(self) -> str:
        return sha256_bytes(self.payload)


BOOK_ROLES: Final = ("upload", "incoming", "copy", "move", "archive-failure")


def generate_collection(run_id: str) -> tuple[str, dict[str, AcceptanceBook]]:
    """A search token and one generated TXT book per intake route, unique to this run."""
    token = "sa" + run_id.rsplit("-", 1)[-1]
    books = {}
    for role in BOOK_ROLES:
        title = f"Site Acceptance {token} {role.replace('-', ' ').title()}"
        text = "\n".join(
            [
                title,
                f"Author: {TEST_AUTHOR}",
                "",
                f"Generated test data for acceptance run {run_id} ({role}). It exercises",
                "intake, archiving, review, search, conversion and download, and may be",
                "deleted once the run is recorded.",
                "",
                "Chapter One",
                "",
                "The quick brown fox jumps over the lazy dog. " * 6,
                "",
            ]
        )
        books[role] = AcceptanceBook(
            role=role,
            title=title,
            filename=f"Site Acceptance {token} {role}.txt",
            payload=text.encode("utf-8"),
        )
    return token, books


def place_source_file(directory: Path, book: AcceptanceBook) -> Path:
    """Write a test book into a source folder, already past the minimum source age."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / book.filename
    if path.exists():
        raise CheckFailed(f"refusing to overwrite an existing file: {path}")
    path.write_bytes(book.payload)
    min_age = float(getattr(settings, "BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS", 60))
    past = time.time() - min_age - 5
    os.utime(path, (past, past))
    return path


# --- Web session ------------------------------------------------------------------


class SiteSession:
    """A member's authenticated HTTP session against the deployment's front end."""

    def __init__(
        self,
        base_url: str,
        *,
        verify: str | bool = True,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        url = httpx.URL(base_url)
        if url.scheme not in ("http", "https") or not url.host or url.userinfo:
            raise CheckNotRun("--base-url must be an http(s) origin without credentials")
        self.origin = base_url.rstrip("/")
        self._options: dict[str, Any] = {
            "base_url": self.origin,
            "verify": verify,
            "timeout": httpx.Timeout(60.0),
            "follow_redirects": False,
            "transport": transport,
        }
        self.client = httpx.Client(**self._options)

    def anonymous(self) -> httpx.Client:
        return httpx.Client(**self._options)

    def _headers(self) -> dict[str, str]:
        return {
            "X-CSRFToken": self.client.cookies.get(settings.CSRF_COOKIE_NAME) or "",
            "Origin": self.origin,
            "Referer": f"{self.origin}/",
            "Accept": "application/json",
        }

    def login(self, username: str, password: str) -> dict[str, Any]:
        page = self.client.get("/accounts/login/")
        expect(page.status_code == 200, f"the sign-in page answered {page.status_code}")
        token = self.client.cookies.get(settings.CSRF_COOKIE_NAME) or ""
        expect(bool(token), "the sign-in page set no CSRF cookie")
        response = self.client.post(
            "/accounts/login/",
            data={"username": username, "password": password, "csrfmiddlewaretoken": token},
            headers={"Origin": self.origin, "Referer": f"{self.origin}/accounts/login/"},
        )
        expect(
            response.status_code == 302,
            f"sign-in was not accepted (HTTP {response.status_code})",
        )
        expect(
            bool(self.client.cookies.get(settings.SESSION_COOKIE_NAME)),
            "no session cookie after sign-in",
        )
        return {"login_status": response.status_code}

    def get_json(self, path: str, params: dict[str, Any] | None = None) -> Any:
        response = self.client.get(path, params=params, headers={"Accept": "application/json"})
        expect(response.status_code == 200, f"GET {path} answered {response.status_code}")
        return response.json()

    def post_json(self, path: str, payload: dict[str, Any], *, expected: int = 200) -> Any:
        response = self.client.post(path, json=payload, headers=self._headers())
        expect(
            response.status_code == expected,
            f"POST {path} answered {response.status_code}: {response.text[:200]}",
        )
        return response.json()

    def upload(self, book: AcceptanceBook, filename: str | None = None) -> str:
        response = self.client.post(
            "/imports/upload/",
            files={"file": (filename or book.filename, book.payload, "text/plain")},
            headers=self._headers(),
        )
        expect(
            response.status_code == 201,
            f"upload answered {response.status_code}: {response.text[:200]}",
        )
        return str(response.json()["job_id"])

    def download(self, book_id: int, variant_id: int) -> tuple[int, str, int]:
        digest = hashlib.sha256()
        size = 0
        with self.client.stream(
            "GET", f"/library/books/{book_id}/download/", params={"variant_id": variant_id}
        ) as response:
            if response.status_code != 200:
                return response.status_code, "", 0
            for chunk in response.iter_bytes():
                digest.update(chunk)
                size += len(chunk)
        return 200, digest.hexdigest(), size

    def close(self) -> None:
        self.client.close()


# --- Library and archive-failure checks --------------------------------------------


@dataclass
class LibraryConfig:
    base_url: str
    username: str
    password: str
    scratch_dir: Path
    incoming_source_id: int | None = None
    conversion_target: str = "epub"
    job_timeout: float = 600.0
    poll_interval: float = 2.0
    verify: str | bool = True
    transport: httpx.BaseTransport | None = None


def validate_scratch_dir(scratch_dir: Path) -> None:
    if not scratch_dir.is_absolute():
        raise CheckNotRun("--scratch-dir must be an absolute path")
    if not scratch_dir.is_dir():
        raise CheckNotRun(f"--scratch-dir does not exist: {scratch_dir}")
    resolved = scratch_dir.resolve()
    archive = archive_root().resolve()
    if resolved == archive or archive in resolved.parents or resolved in archive.parents:
        raise CheckNotRun("--scratch-dir must not be inside, or contain, the archive root")


def import_outcome(job: ImportJob) -> dict[str, Any]:
    return {
        "import_job_id": str(job.id),
        "state": job.state,
        "sha256": job.sha256,
        "book_id": job.book_id,
        "file_variant_id": job.file_variant_id,
        "error": job.last_error,
    }


def verify_variant(variant: FileVariant, expected_sha: str) -> dict[str, Any]:
    """Re-read the archive copy of ``variant`` from its storage and hash it."""
    backend = filesystem_backend_for_location(variant.storage_location)
    expect(backend.exists(variant.storage_identifier), "the archive copy is missing")
    observed = backend.hash(variant.storage_identifier)
    expect(variant.sha256 == expected_sha, "the variant's checksum is not the expected one")
    expect(observed == expected_sha, f"the archive copy hashes to {observed}")
    return {"archive_sha256": observed, "storage_identifier": variant.storage_identifier}


def ingest(location: SourceLocation, *extra: str) -> str:
    """Run ``ingest_sources`` for one source, exactly as an operator or timer would."""
    out = StringIO()
    call_command("ingest_sources", "--source-id", str(location.id), *extra, stdout=out)
    return out.getvalue().strip()


def create_scratch_source(
    name: str,
    root: Path,
    mode: str,
    source_type: str = SourceLocation.SourceType.NETWORK_SOURCE,
) -> SourceLocation:
    root.mkdir(parents=True, exist_ok=False)
    return SourceLocation.objects.create(
        name=name[:128],
        root_path=str(root),
        transfer_mode=mode,
        recursive=True,
        default_source_type=source_type,
        is_active=True,
        ingest_batch_size=0,
        ingest_max_per_hour=0,
    )


def failing_archive_storage(root: str) -> LocalFilesystemStorage:
    """An archive backend whose writes leave a truncated file behind and then fail."""

    class FailingArchiveStorage(LocalFilesystemStorage):
        def _fail(self, source: str, destination: str) -> str:
            dest = self._prepare_destination(destination)
            payload = self._resolve_source(source).read_bytes()
            dest.write_bytes(payload[: len(payload) // 2])
            raise SimulatedArchiveWriteError(
                f"simulated archive write failure after {len(payload) // 2} bytes"
            )

        def store(self, source: str, destination: str) -> str:
            return self._fail(source, destination)

        def copy(self, source: str, destination: str) -> str:
            return self._fail(source, destination)

    return FailingArchiveStorage(root)


@contextmanager
def simulated_archive_write_failure() -> Iterator[None]:
    """Make archive writes fail, in this process only.

    Workers and every import they run keep the real backend; the check runs its one
    failing attempt in-process on a generated file from its own scratch source.
    """
    with mock.patch.object(
        intake, "_default_storage", lambda: failing_archive_storage(str(archive_root()))
    ):
        yield


def folder_files(path: Path) -> list[Path]:
    folder = path.parent
    return sorted(child for child in folder.iterdir() if child.is_file()) if folder.is_dir() else []


class SiteAcceptance:
    """The library journey and the archive-write failure drill, as one member session."""

    def __init__(
        self,
        recorder: Recorder,
        config: LibraryConfig,
        run_id: str,
        *,
        library: bool,
        archive_failure: bool,
    ) -> None:
        self.recorder = recorder
        self.config = config
        self.run_id = run_id
        self.library = library
        self.archive_failure = archive_failure
        self.token, self.books = generate_collection(run_id)
        self.scratch_root = config.scratch_dir / f"{SCRATCH_PREFIX}{run_id}"
        self.session: SiteSession | None = None
        self.sources: list[SourceLocation] = []
        self.upload_variant: FileVariant | None = None

    # helpers

    def _settle(self, job_id: str) -> ImportJob:
        def probe() -> ImportJob | None:
            job = ImportJob.objects.get(pk=job_id)
            return job if job.state in SETTLED_IMPORT_STATES and not job.lease_owner else None

        return wait_for(
            probe,
            timeout=self.config.job_timeout,
            interval=self.config.poll_interval,
            what=f"import job {job_id}",
        )

    def _session(self) -> SiteSession:
        assert self.session is not None
        return self.session

    def _review(self, job: ImportJob, book: AcceptanceBook) -> tuple[FileVariant, dict[str, Any]]:
        """Resolve the import's open review task through the web manual-entry action."""
        tasks = list(
            ReviewTask.objects.filter(
                import_job=job, state__in=(ReviewTask.State.OPEN, ReviewTask.State.CLAIMED)
            )
        )
        expect(len(tasks) == 1, f"expected one open review task, found {len(tasks)}")
        task = tasks[0]
        response = self._session().post_json(
            f"/reviews/{task.id}/manual-entry/",
            {"title": book.title, "authors": [TEST_AUTHOR], "language": "en"},
        )
        resolved = self._settle(str(job.id))
        task.refresh_from_db()
        expect(resolved.state == ImportJob.State.COMPLETE, f"import is {resolved.state}")
        expect(task.state == ReviewTask.State.RESOLVED, f"review task is {task.state}")
        expect(resolved.file_variant_id is not None, "the review linked no file variant")
        variant = FileVariant.objects.select_related("storage_location").get(
            pk=resolved.file_variant_id
        )
        return variant, {
            "review_task_id": task.id,
            "review_decision": task.decision,
            "book_id": response.get("book_id"),
            "file_variant_id": variant.id,
        }

    def _scratch_source(
        self, role: str, mode: str, source_type: str | None = None
    ) -> SourceLocation:
        location = create_scratch_source(
            f"{SCRATCH_PREFIX}{self.run_id}-{role}",
            self.scratch_root / role,
            mode,
            source_type or SourceLocation.SourceType.NETWORK_SOURCE,
        )
        self.sources.append(location)
        return location

    def _record_variant(self, check: str, role: str, variant: FileVariant) -> None:
        self.recorder.artifact(
            Artifact(check, role, variant.sha256, variant.size_bytes, variant.id)
        )

    # flow

    def run(self) -> None:
        names = [*(LIBRARY_CHECKS if self.library else ())]
        if self.archive_failure:
            names.append(ARCHIVE_FAILURE_CHECK)
        try:
            with self.recorder.check("sign-in") as details:
                validate_scratch_dir(self.config.scratch_dir)
                if not self.config.username or not self.config.password:
                    raise CheckNotRun("SITE_ACCEPTANCE_USERNAME/SITE_ACCEPTANCE_PASSWORD not set")
                self.session = SiteSession(
                    self.config.base_url,
                    verify=self.config.verify,
                    transport=self.config.transport,
                )
                details.update(self.session.login(self.config.username, self.config.password))
                details["base_url"] = self.config.base_url
                details["username"] = self.config.username
                self.scratch_root.mkdir()
                details["scratch_root"] = str(self.scratch_root)
            if not self.recorder.passed("sign-in"):
                for name in names:
                    self.recorder.not_run(name, "sign-in did not pass")
                return
            if self.library:
                self.web_upload_and_review()
                self.deduplication()
                self.source_import("incoming-discovery", "incoming")
                self.source_import("copy", "copy")
                self.source_import("move", "move")
                self.search()
                self.conversion_and_download()
            if self.archive_failure:
                self.archive_write_failure()
        finally:
            # Scratch sources never take part in later ingest runs.
            SourceLocation.objects.filter(pk__in=[s.pk for s in self.sources]).update(
                is_active=False
            )
            if self.session is not None:
                self.session.close()

    def web_upload_and_review(self) -> None:
        book = self.books["upload"]
        job: ImportJob | None = None
        with self.recorder.check("web-upload") as details:
            job_id = self._session().upload(book)
            job = self._settle(job_id)
            details.update(import_outcome(job))
            expect(job.sha256 == book.sha256, "the server hashed other bytes than were uploaded")
            expect(
                job.state in (ImportJob.State.LOW_CONFIDENCE, ImportJob.State.COMPLETE),
                f"the upload ended {job.state}: {job.last_error}",
            )
            status = self._session().get_json(f"/imports/{job_id}/")
            expect(status["state"] == job.state, "the import status page disagrees with the job")
            self.recorder.artifact(
                Artifact("web-upload", "uploaded test book", book.sha256, len(book.payload))
            )
        with self.recorder.check("review") as details:
            if job is None or not self.recorder.passed("web-upload"):
                raise CheckNotRun("web-upload did not pass")
            expect(
                job.state == ImportJob.State.LOW_CONFIDENCE,
                f"the generated book ended {job.state} instead of waiting for review",
            )
            variant, review = self._review(job, book)
            details.update(review)
            details.update(verify_variant(variant, book.sha256))
            self.upload_variant = variant
            self._record_variant("review", "archived upload", variant)

    def deduplication(self) -> None:
        with self.recorder.check("deduplication") as details:
            if self.upload_variant is None:
                raise CheckNotRun("review did not pass, so there is no archived original")
            book = self.books["upload"]
            job = self._settle(self._session().upload(book, filename=f"Again {book.filename}"))
            details.update(import_outcome(job))
            expect(job.state == ImportJob.State.COMPLETE, f"the re-upload ended {job.state}")
            expect(
                job.file_variant_id == self.upload_variant.id,
                "the identical upload was not linked to the existing file variant",
            )
            count = FileVariant.objects.filter(sha256=book.sha256).count()
            details["variants_with_this_sha256"] = count
            expect(count == 1, f"{count} file variants hold the same bytes")

    def _incoming_location(self) -> tuple[SourceLocation, Path, bool]:
        if self.config.incoming_source_id is None:
            location = self._scratch_source(
                "incoming",
                SourceLocation.TransferMode.COPY,
                SourceLocation.SourceType.INCOMING_FOLDER,
            )
            return location, Path(location.root_path), False
        location = SourceLocation.objects.get(pk=self.config.incoming_source_id)
        if not location.is_active or location.is_paused:
            raise CheckNotRun(f"incoming source {location.id} is inactive or paused")
        if location.transfer_mode == SourceLocation.TransferMode.INDEX_IN_PLACE:
            raise CheckNotRun(f"incoming source {location.id} indexes in place")
        root = Path(location.root_path)
        folder = root / f"{SCRATCH_PREFIX}{self.run_id}" if location.recursive else root
        return location, folder, True

    def source_import(self, name: str, role: str) -> None:
        book = self.books[role]
        with self.recorder.check(name) as details:
            if role == "incoming":
                location, folder, configured = self._incoming_location()
                details["configured_incoming_source"] = configured
            else:
                mode = {"copy": SourceLocation.TransferMode.COPY}.get(
                    role, SourceLocation.TransferMode.MOVE
                )
                location = self._scratch_source(role, mode)
                folder = Path(location.root_path)
            details["source_location_id"] = location.id
            details["transfer_mode"] = location.transfer_mode
            source_path = place_source_file(folder, book)
            details["ingest_output"] = ingest(location)
            queued = ImportJob.objects.filter(
                source_location=location, original_filename=book.filename
            ).first()
            expect(queued is not None, "discovery queued no import job for the test file")
            assert queued is not None
            job = self._settle(str(queued.id))
            details.update(import_outcome(job))
            expect(job.sha256 == book.sha256, "the import hashed other bytes")
            expect(job.state == ImportJob.State.LOW_CONFIDENCE, f"the import ended {job.state}")
            variant, review = self._review(job, book)
            details.update(review)
            details.update(verify_variant(variant, book.sha256))
            source_exists = source_path.exists()
            details["source_exists_after_import"] = source_exists
            if location.transfer_mode == SourceLocation.TransferMode.MOVE:
                expect(not source_exists, "MOVE kept the source after a verified archive copy")
            else:
                expect(source_exists, f"{location.transfer_mode} removed the source file")
                expect(sha256_file(source_path) == book.sha256, "the source file changed")
            self._record_variant(name, f"archived {role} import", variant)

    def search(self) -> None:
        with self.recorder.check("search") as details:
            if self.upload_variant is None:
                raise CheckNotRun("review did not pass, so there is no book to search for")
            book_id = self.upload_variant.book_id
            session = self._session()

            def probe() -> dict[str, Any] | None:
                found = session.get_json("/library/books/", {"q": self.token})
                ids = [item["id"] for item in found.get("items", [])]
                return found if book_id in ids else None

            found = wait_for(
                probe,
                timeout=min(self.config.job_timeout, 120.0),
                interval=self.config.poll_interval,
                what="the reviewed book to appear in search results",
            )
            details.update({"query": self.token, "result_count": found["count"]})
            details["book_id"] = book_id
            details["titles"] = [item["title"] for item in found["items"]]

    def conversion_and_download(self) -> None:
        converted: FileVariant | None = None
        with self.recorder.check("conversion") as details:
            if self.upload_variant is None:
                raise CheckNotRun("review did not pass, so there is no variant to convert")
            source = self.upload_variant
            session = self._session()
            detail = session.get_json(f"/library/books/{source.book_id}/")
            targets = sorted(
                {
                    option["target_format"]
                    for option in detail.get("conversion_options", [])
                    if int(option["source_variant_id"]) == source.id
                }
            )
            details["offered_targets"] = targets
            target = self.config.conversion_target
            expect(
                target in targets,
                f"no {target} conversion is offered (offered: {targets}); "
                "is the converter installed for the workers?",
            )
            job = session.post_json(
                f"/library/books/{source.book_id}/convert/",
                {"source_variant_id": source.id, "target_format": target},
                expected=202,
            )
            details["conversion_job_id"] = job["job_id"]

            def probe() -> dict[str, Any] | None:
                state = session.get_json(f"/conversions/{job['job_id']}/")
                return state if state["state"] in ("success", "failed") else None

            final = wait_for(
                probe,
                timeout=self.config.job_timeout,
                interval=self.config.poll_interval,
                what=f"conversion job {job['job_id']}",
            )
            details.update({"state": final["state"], "route": final["route"]})
            expect(final["state"] == "success", f"the conversion failed: {final['error']}")
            converted = FileVariant.objects.select_related("storage_location").get(
                pk=int(final["output_variant_id"])
            )
            details.update(
                {
                    "output_variant_id": converted.id,
                    "output_format": converted.format,
                    "converter_tool": converted.converter_tool,
                    "converter_version": converted.converter_version,
                }
            )
            expect(converted.source_variant_id == source.id, "the source variant is not recorded")
            expect(bool(converted.converter_tool), "no converter tool is recorded")
            details.update(verify_variant(converted, converted.sha256))
            self._record_variant("conversion", f"converted {converted.format}", converted)
        with self.recorder.check("authenticated-download") as details:
            if self.upload_variant is None:
                raise CheckNotRun("review did not pass, so there is nothing to download")
            book_id = self.upload_variant.book_id
            with self._session().anonymous() as anonymous:
                denied = anonymous.get(
                    f"/library/books/{book_id}/download/",
                    params={"variant_id": self.upload_variant.id},
                )
            details["anonymous_status"] = denied.status_code
            expect(
                denied.status_code in (401, 403)
                or (
                    denied.status_code == 302
                    and "/accounts/login/" in denied.headers.get("location", "")
                ),
                f"an anonymous download answered {denied.status_code}",
            )
            downloads = []
            for variant in [self.upload_variant, *([converted] if converted else [])]:
                status, digest, size = self._session().download(book_id, variant.id)
                downloads.append(
                    {
                        "variant_id": variant.id,
                        "format": variant.format,
                        "status": status,
                        "sha256": digest,
                        "bytes": size,
                    }
                )
                expect(status == 200, f"downloading variant {variant.id} answered {status}")
                expect(digest == variant.sha256, f"variant {variant.id} downloaded other bytes")
                expect(size == variant.size_bytes, f"variant {variant.id} came back truncated")
            details["downloads"] = downloads
            details["downloaded_variants"] = len(downloads)
            if converted is None:
                details["note"] = "conversion did not pass; only the original was downloaded"

    def archive_write_failure(self) -> None:
        """A MOVE import whose first archive write fails; the retry leaves one copy."""
        book = self.books["archive-failure"]
        with self.recorder.check(ARCHIVE_FAILURE_CHECK) as details:
            location = self._scratch_source("move-failure", SourceLocation.TransferMode.MOVE)
            source_path = place_source_file(Path(location.root_path), book)
            job = intake.create_source_import_job(source_path, location)
            details["import_job_id"] = str(job.id)
            details["source_location_id"] = location.id

            # 1. The first attempt runs here, with archive writes failing part way.
            with simulated_archive_write_failure():
                try:
                    intake.process_import_job(str(job.id))
                except SimulatedArchiveWriteError as exc:
                    details["first_attempt_error"] = str(exc)
                else:
                    raise CheckFailed("the import never reached the simulated archive write")
            job.refresh_from_db()
            storage = intake._default_storage()
            destination = archive_layout.pending_destination(storage, job)
            partial = storage.resolve_location(destination)
            partial_bytes = partial.stat().st_size if partial.exists() else None
            details["after_failure"] = {
                "state": job.state,
                "error": job.last_error,
                "source_exists": source_path.exists(),
                "partial_archive_bytes": partial_bytes,
            }
            expect(job.state == ImportJob.State.FAILED, f"the failed attempt left {job.state}")
            expect(source_path.exists(), "the MOVE source was deleted after a failed archive")
            expect(sha256_file(source_path) == book.sha256, "the MOVE source changed")
            expect(
                partial_bytes is not None and partial_bytes < len(book.payload),
                "the simulated failure left no partial archive file",
            )
            details["source_survived_failure"] = True

            # 2. The operator's retry, through the real queue and workers.
            details["retry_output"] = ingest(location, "--retry-failed")
            job = self._settle(str(job.id))
            details["after_retry"] = import_outcome(job)
            expect(job.state == ImportJob.State.LOW_CONFIDENCE, f"the retry ended {job.state}")
            expect(job.storage_identifier == destination, "the retry archived under another name")
            files = [path.name for path in folder_files(partial)]
            details["archive_files_after_retry"] = files
            expect(len(files) == 1, f"{len(files)} files in the import's archive folder: {files}")
            expect(storage.hash(destination) == book.sha256, "the retried archive copy is corrupt")
            expect(not source_path.exists(), "the MOVE source was kept after a verified retry")

            # 3. Resolving the review keeps exactly that one copy.
            variant, review = self._review(job, book)
            details.update(review)
            details.update(verify_variant(variant, book.sha256))
            kept = filesystem_backend_for_location(variant.storage_location).resolve_location(
                variant.storage_identifier
            )
            leftovers = [path.name for path in folder_files(partial) if path != kept]
            details["pending_folder_leftovers"] = leftovers
            expect(not leftovers, f"files left in the pending archive folder: {leftovers}")
            count = FileVariant.objects.filter(sha256=book.sha256).count()
            details["variants_with_this_sha256"] = count
            expect(count == 1, f"{count} file variants hold the test file")
            self._record_variant(ARCHIVE_FAILURE_CHECK, "archive copy after retry", variant)


# --- Migration rehearsal ------------------------------------------------------------

_REHEARSAL_LINE = re.compile(r"^(PASS|FAIL)\s{2}(.+?)\s+\((\d+)\.(\d{3})s\)\s*$")


def parse_rehearsal_report(output: str) -> dict[str, Any]:
    """Read the PASS/FAIL report that ``scripts/rehearse_migrations.sh`` prints last."""
    steps: list[dict[str, Any]] = []
    result = source = scratch = None
    in_report = False
    for raw in output.splitlines():
        line = raw.rstrip("\r")
        if line.startswith("=== Migration rehearsal report ==="):
            in_report, steps = True, []
        elif not in_report:
            continue
        elif line.startswith("source database:"):
            source = line.split(":", 1)[1].strip()
        elif line.startswith("scratch database:"):
            scratch = line.split(":", 1)[1].strip()
        elif line.startswith("RESULT:"):
            result = line.split(":", 1)[1].strip()
        elif match := _REHEARSAL_LINE.match(line):
            seconds = int(match.group(3)) + int(match.group(4)) / 1000
            steps.append({"status": match.group(1), "step": match.group(2), "seconds": seconds})
    by_step = {step["step"]: step for step in steps}
    return {
        "result": result,
        "source_database": source,
        "scratch_database": scratch,
        "steps": steps,
        "migrate_seconds": by_step.get("manage.py migrate", {}).get("seconds"),
        "total_seconds": by_step.get("total", {}).get("seconds"),
    }


def run_migration_rehearsal(
    recorder: Recorder,
    *,
    log_path: Path,
    sample: int | None = None,
    script: Path = REHEARSAL_SCRIPT,
    bash: str | None = None,
) -> None:
    """Run the existing rehearsal script on a database copy and record its timings."""
    with recorder.check("migration-rehearsal") as details:
        for name in ("SOURCE_DATABASE_URL", "ADMIN_DATABASE_URL"):
            if not os.environ.get(name):
                raise CheckNotRun(f"{name} is not set (see docs/operations.md)")
        details["pending_migrations"] = unapplied_migrations()
        command = [
            bash or shutil.which("bash") or "bash",
            script.as_posix(),
            *(["--sample", str(sample)] if sample is not None else []),
        ]
        started = time.monotonic()
        completed = subprocess.run(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
            cwd=script.parent.parent,
        )
        details["wall_seconds"] = round(time.monotonic() - started, 3)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(completed.stdout, "utf-8")
        details["exit_status"] = completed.returncode
        details["log"] = str(log_path)
        parsed = parse_rehearsal_report(completed.stdout)
        details.update(parsed)
        expect(parsed["result"] is not None, f"the rehearsal printed no report; see {log_path}")
        expect(
            completed.returncode == 0 and parsed["result"] == "PASS",
            f"rehearsal {parsed['result']} (exit {completed.returncode}); see {log_path}",
        )


# --- Alerting -----------------------------------------------------------------------


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def build_test_alert(run_id: str, *, starts: datetime, ends: datetime) -> list[dict[str, Any]]:
    return [
        {
            "labels": {
                "alertname": TEST_ALERT_NAME,
                "severity": "warning",
                "job": "book-scanner",
                "acceptance_run": run_id,
            },
            "annotations": {
                "summary": "Book Scanner site acceptance test alert",
                "description": (
                    f"Test notification for acceptance run {run_id} (#162). "
                    "It resolves by itself; no action is needed."
                ),
            },
            "startsAt": _iso(starts),
            "endsAt": _iso(ends),
        }
    ]


def _active_test_alerts(client: httpx.Client, run_id: str) -> list[dict[str, Any]]:
    response = client.get(
        "/api/v2/alerts",
        params={
            "filter": [f'alertname="{TEST_ALERT_NAME}"', f'acceptance_run="{run_id}"'],
            "active": "true",
        },
    )
    response.raise_for_status()
    return [alert for alert in response.json() if alert.get("status", {}).get("state") == "active"]


# Asks the operator a yes/no question; None when no answer can be collected.
Confirm = Callable[[str], bool | None]


def run_alert_test(
    recorder: Recorder,
    run_id: str,
    *,
    alertmanager_url: str,
    confirm: Confirm,
    token: str = "",
    verify: str | bool = True,
    transport: httpx.BaseTransport | None = None,
    timeout: float = 120.0,
    interval: float = 2.0,
) -> None:
    """Fire a synthetic alert through the site Alertmanager, then resolve it.

    Alertmanager's API shows that the alert was accepted, routed active and cleared;
    only the operator can say whether the recipient got the firing and the resolved
    notification. Without both explicit "yes" answers the check is not PASS.
    """
    with recorder.check("alert-delivery") as details:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        with httpx.Client(
            base_url=alertmanager_url,
            headers=headers,
            verify=verify,
            timeout=30,
            transport=transport,
        ) as client:
            status = client.get("/api/v2/status")
            status.raise_for_status()
            info = status.json()
            details["alertmanager_version"] = info.get("versionInfo", {}).get("version")
            details["cluster_status"] = info.get("cluster", {}).get("status")
            details["alertname"] = TEST_ALERT_NAME
            fired = utc_now()
            resolved_sent = False
            try:
                client.post(
                    "/api/v2/alerts",
                    json=build_test_alert(run_id, starts=fired, ends=fired + timedelta(hours=1)),
                ).raise_for_status()
                details["fired_at"] = fired.isoformat()
                wait_for(
                    lambda: _active_test_alerts(client, run_id),
                    timeout=timeout,
                    interval=interval,
                    what="the test alert to become active",
                )
                details["active_seen_at"] = utc_now().isoformat()
                details["firing_delivery_confirmed"] = confirm(
                    f"Did the site recipient receive the FIRING notification for "
                    f"{TEST_ALERT_NAME} (acceptance_run={run_id})?"
                )
                details["firing_answered_at"] = utc_now().isoformat()
                resolved = utc_now()
                client.post(
                    "/api/v2/alerts", json=build_test_alert(run_id, starts=fired, ends=resolved)
                ).raise_for_status()
                resolved_sent = True
                details["resolved_at"] = resolved.isoformat()
                wait_for(
                    lambda: not _active_test_alerts(client, run_id),
                    timeout=timeout,
                    interval=interval,
                    what="the test alert to clear",
                )
                details["cleared_seen_at"] = utc_now().isoformat()
                details["resolved_delivery_confirmed"] = confirm(
                    "Did the site recipient receive the RESOLVED notification?"
                )
                details["resolved_answered_at"] = utc_now().isoformat()
            finally:
                if not resolved_sent:  # never leave the test alert firing
                    client.post(
                        "/api/v2/alerts",
                        json=build_test_alert(run_id, starts=fired, ends=utc_now()),
                    )
        answers = (details["firing_delivery_confirmed"], details["resolved_delivery_confirmed"])
        if False in answers:
            raise CheckFailed("the operator reported that a notification did not arrive")
        if None in answers:
            details["_incomplete"] = (
                "Alertmanager accepted and cleared the alert, but no operator confirmed "
                "delivery to the recipient"
            )


def run_scrape_check(
    recorder: Recorder,
    *,
    prometheus_url: str,
    verify: str | bool = True,
    transport: httpx.BaseTransport | None = None,
) -> None:
    """Record whether the site Prometheus currently scrapes the application."""
    with recorder.check("metrics-scrape") as details:
        with httpx.Client(
            base_url=prometheus_url, verify=verify, timeout=30, transport=transport
        ) as client:
            response = client.get("/api/v1/query", params={"query": 'up{job="book-scanner"}'})
            response.raise_for_status()
            payload = response.json()
        results = payload.get("data", {}).get("result", [])
        targets = {item["metric"].get("instance", "?"): item["value"][1] for item in results}
        details["targets"] = targets
        details["target_count"] = len(targets)
        expect(bool(targets), 'Prometheus has no up{job="book-scanner"} series')
        down = sorted(instance for instance, value in targets.items() if value != "1")
        expect(not down, f"targets down: {down}")
