"""Run Version 1 site acceptance checks and record them in a report (#162)."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from pathlib import Path
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from ebook_server.core import site_acceptance as sa

CHECKS = ("migrations", "library", "archive-failure", "alert", "scrape")


def _ask(question: str) -> bool | None:
    """Ask the operator a yes/no question on the terminal; None when there is none."""
    if not sys.stdin.isatty():
        return None
    while True:
        answer = input(f"{question} [yes/no] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


class Command(BaseCommand):
    help = (
        "Run site acceptance checks against this deployment (web front end, database, "
        "workers, storage, LDAP, alerting) and append the observations to a report."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--report", required=True, type=Path, help="Report JSON path.")
        parser.add_argument(
            "--check",
            action="append",
            choices=CHECKS,
            required=True,
            help="Check group to run; repeat for several (see docs/operations.md).",
        )
        parser.add_argument("--base-url", help="Front-end origin members use, e.g. https://...")
        parser.add_argument("--ca-bundle", help="CA certificate file for the HTTPS endpoints.")
        parser.add_argument(
            "--scratch-dir",
            type=Path,
            help="Writable folder on the source share for the generated test collection.",
        )
        parser.add_argument(
            "--incoming-source-id",
            type=int,
            help="Use this configured incoming source (default: a scratch incoming folder).",
        )
        parser.add_argument("--conversion-target", default="epub")
        parser.add_argument("--job-timeout", type=float, default=600.0)
        parser.add_argument("--alertmanager-url")
        parser.add_argument("--prometheus-url")
        parser.add_argument(
            "--no-prompt",
            action="store_true",
            help="Do not ask for delivery confirmation (the alert check stays INCOMPLETE).",
        )
        parser.add_argument("--rehearsal-sample", type=int)

    def handle(self, *args: Any, **options: Any) -> None:
        del args
        checks: list[str] = options["check"]
        report_path: Path = options["report"]
        verify: str | bool = options["ca_bundle"] or True
        try:
            report = sa.Report(report_path)
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        run_id = sa.new_run_id()
        run = report.start_run(run_id, {**sa.run_header(), "requested_checks": checks})
        recorder = sa.Recorder(report, run, echo=self.stdout.write)
        self.stdout.write(f"site acceptance run {run_id} -> {report_path}")

        if "migrations" in checks:
            sa.run_migration_rehearsal(
                recorder,
                log_path=report_path.with_name(f"{report_path.stem}.rehearsal-{run_id}.log"),
                sample=options["rehearsal_sample"],
            )
        if "library" in checks or "archive-failure" in checks:
            self._library(recorder, run_id, options, verify, checks)
        if "alert" in checks:
            if not options["alertmanager_url"]:
                recorder.not_run("alert-delivery", "--alertmanager-url not given")
            else:
                sa.run_alert_test(
                    recorder,
                    run_id,
                    alertmanager_url=options["alertmanager_url"],
                    confirm=(lambda _question: None) if options["no_prompt"] else _ask,
                    token=os.environ.get("SITE_ACCEPTANCE_ALERTMANAGER_TOKEN", ""),
                    verify=verify,
                )
        if "scrape" in checks:
            if not options["prometheus_url"]:
                recorder.not_run("metrics-scrape", "--prometheus-url not given")
            else:
                sa.run_scrape_check(
                    recorder, prometheus_url=options["prometheus_url"], verify=verify
                )

        status = recorder.finish()
        self.stdout.write(f"RESULT: {status} ({report_path}, {report.markdown_path})")
        if status == sa.FAIL:
            raise CommandError("site acceptance failed", returncode=1)
        if status != sa.PASS:
            raise CommandError("site acceptance incomplete", returncode=3)

    def _library(
        self,
        recorder: sa.Recorder,
        run_id: str,
        options: dict[str, Any],
        verify: str | bool,
        checks: list[str],
    ) -> None:
        password = os.environ.get("SITE_ACCEPTANCE_PASSWORD", "")
        username = os.environ.get("SITE_ACCEPTANCE_USERNAME", "")
        if username and not password and sys.stdin.isatty() and not options["no_prompt"]:
            password = getpass.getpass(f"Password for {username}: ")
        config = sa.LibraryConfig(
            base_url=options["base_url"] or "",
            username=username,
            password=password,
            scratch_dir=options["scratch_dir"] or Path(),
            incoming_source_id=options["incoming_source_id"],
            conversion_target=options["conversion_target"],
            job_timeout=options["job_timeout"],
            verify=verify,
        )
        if not config.base_url or options["scratch_dir"] is None:
            names = ["sign-in"]
            names += list(sa.LIBRARY_CHECKS) if "library" in checks else []
            names += [sa.ARCHIVE_FAILURE_CHECK] if "archive-failure" in checks else []
            for name in names:
                recorder.not_run(name, "--base-url and --scratch-dir are required")
            return
        sa.SiteAcceptance(
            recorder,
            config,
            run_id,
            library="library" in checks,
            archive_failure="archive-failure" in checks,
        ).run()
