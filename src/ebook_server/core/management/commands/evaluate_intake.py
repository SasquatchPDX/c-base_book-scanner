"""Reproduce labelled intake decisions in an isolated, network-free database."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand, CommandError
from django.db import connections
from django.test import override_settings

from ebook_server.core import identification
from ebook_server.core.intake_evaluation import read_corpus, run_corpus


def fingerprint(path: Path) -> str:
    """Normalize Git line endings so a Windows checkout reproduces Linux results."""
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def isolated_environment(root: Path) -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("BOOK_SCANNER_")
        and key.upper() not in {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"}
    }
    environment.update(
        {
            "DJANGO_SETTINGS_MODULE": "ebook_server.settings",
            "PYTHONPATH": str(settings.BASE_DIR),
            "BOOK_SCANNER_ENVIRONMENT": "test",
            "BOOK_SCANNER_DATABASE_URL": f"sqlite:///{(root / 'evaluation.sqlite3').as_posix()}",
            "BOOK_SCANNER_REDIS_URL": "",
            "BOOK_SCANNER_LDAP_MODE": "stub",
            "BOOK_SCANNER_STORAGE_ROOT": str(root / "storage"),
            "BOOK_SCANNER_STORAGE_REQUIRE_EXISTING_ROOT": "false",
            "BOOK_SCANNER_STORAGE_MIN_FREE_BYTES": "0",
            "BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS": "0",
            "BOOK_SCANNER_WRITE_BOOK_OPF": "false",
            "BOOK_SCANNER_LLM_ENABLED": "false",
            "BOOK_SCANNER_METADATA_PROVIDERS": "[]",
            "BOOK_SCANNER_LOG_LEVEL": "ERROR",
        }
    )
    return environment


def validate_worker_root(root: Path) -> None:
    database = connections["default"].settings_dict
    if (
        os.environ.get("BOOK_SCANNER_ENVIRONMENT") != "test"
        or database["ENGINE"] != "django.db.backends.sqlite3"
        or Path(database["NAME"]).resolve() != root / "evaluation.sqlite3"
        or Path(database["NAME"]).exists()
        or Path(settings.APP_DATA_DIR).resolve() != root / "storage"
        or settings.BOOK_SCANNER_QUEUE_URL
    ):
        raise CommandError("evaluation worker requires its own isolated SQLite/storage root")


class Command(BaseCommand):
    help = (
        "Measure actual intake decisions offline against labelled EPUB/TXT fixtures; "
        "never writes the caller's database."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--corpus",
            default=str(
                Path(settings.BASE_DIR).parent / "tests/fixtures/intake_evaluation/corpus.json"
            ),
        )
        parser.add_argument("--trial-threshold", type=float, default=0.80)
        parser.add_argument("--output", help="write the deterministic JSON scorecard here")
        parser.add_argument("--worker-root", help=argparse.SUPPRESS)

    def handle(self, *args: Any, **options: Any) -> None:
        corpus_path = Path(options["corpus"]).resolve()
        trial = options["trial_threshold"]
        if not 0 < trial <= 1:
            raise CommandError("trial threshold must be in (0, 1]")
        try:
            corpus = read_corpus(corpus_path)
        except (OSError, ValueError) as exc:
            raise CommandError(f"invalid intake corpus: {exc}") from exc
        if options["worker_root"]:
            root = Path(options["worker_root"]).resolve()
            validate_worker_root(root)  # guard before migrations or any fixture writes
            call_command("migrate", interactive=False, verbosity=0, stdout=StringIO())
            baseline = identification.AUTO_ACCEPT_SCORE
            runs = {}
            for name, threshold in (("baseline", baseline), ("trial", trial)):
                with override_settings(APP_DATA_DIR=root / f"storage-{name}"):
                    runs[name] = run_corpus(corpus, root / name, threshold)
            changed = [
                {
                    "case": before["case"],
                    "file": before["file"],
                    "before": before["outcome"],
                    "after": after["outcome"],
                    "before_correct_auto": before["correct_auto"],
                    "after_correct_auto": after["correct_auto"],
                }
                for before, after in zip(
                    runs["baseline"]["rows"], runs["trial"]["rows"], strict=True
                )
                if before["outcome"] != after["outcome"] or before["selected"] != after["selected"]
            ]
            source = Path(identification.__file__).parent
            report = {
                "schema_version": 1,
                "corpus_sha256": fingerprint(corpus_path),
                "rule_sha256": {
                    name: fingerprint(source / name)
                    for name in (
                        "identification.py",
                        "intake.py",
                        "local_identity.py",
                        "intake_evaluation.py",
                    )
                },
                "dependency_lock_sha256": fingerprint(Path(settings.BASE_DIR).parent / "uv.lock"),
                "scope": (
                    "synthetic offline provider snapshots; not field accuracy or provider uptime"
                ),
                **runs,
                "changed_rows": changed,
            }
            (root / "report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            return
        with tempfile.TemporaryDirectory(prefix="ebook-intake-eval-") as workdir:
            root = Path(workdir).resolve()
            result = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "django",
                    "evaluate_intake",
                    "--corpus",
                    str(corpus_path),
                    "--trial-threshold",
                    str(trial),
                    "--worker-root",
                    str(root),
                ],
                cwd=root,
                env=isolated_environment(root),
                capture_output=True,
                text=True,
                timeout=180,
            )
            if result.returncode != 0:
                raise CommandError(f"isolated evaluation failed: {result.stderr[-2000:]}")
            payload = (root / "report.json").read_text(encoding="utf-8")
        if options["output"]:
            Path(options["output"]).write_text(payload, encoding="utf-8")
        else:
            self.stdout.write(payload, ending="")
