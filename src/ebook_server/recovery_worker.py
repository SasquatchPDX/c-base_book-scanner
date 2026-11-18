"""RQ's normal worker with application-state recovery for killed work horses."""

from __future__ import annotations

import os
from typing import Any

import django
from rq import Worker

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "ebook_server.settings")
django.setup()

from ebook_server.core.worker_failures import work_horse_killed  # noqa: E402


class RecoveryWorker(Worker):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        kwargs.setdefault("work_horse_killed_handler", work_horse_killed)
        super().__init__(*args, **kwargs)
