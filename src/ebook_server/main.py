"""Command entrypoint helpers for the Django application."""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path


def _ensure_default_env() -> None:
    Path(__file__).resolve()
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")


def main(argv: Sequence[str] | None = None) -> None:
    """Run Django management command dispatcher."""

    _ensure_default_env()
    from django.core.management import execute_from_command_line

    args = list(argv) if argv is not None else sys.argv
    if len(args) == 1:
        args.append("check")
    execute_from_command_line(args)


def project_root() -> Path:
    """Return repository root from installed package location."""

    return Path(__file__).resolve().parents[1]


__all__ = ["main", "project_root"]
