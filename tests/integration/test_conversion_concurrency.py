"""Real PostgreSQL delivery races, including files left by superseded attempts."""

from __future__ import annotations

import errno
import os
import threading
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from django.db import connection, connections
from django.utils import timezone

from ebook_server.core import conversion
from ebook_server.core.management.commands.recover_stale_jobs import Command
from ebook_server.core.models import FileVariant, ProcessingJob
from ebook_server.jobs import stale_after
from ebook_server.storage import LocalFilesystemStorage
from tests.test_conversion import StubConverter
from tests.test_conversion_claim import create_conversion

pytestmark = [
    pytest.mark.infrastructure,
    pytest.mark.django_db(transaction=True),
    pytest.mark.skipif(
        os.environ.get("BOOK_SCANNER_RUN_INFRASTRUCTURE_TESTS") != "true",
        reason="requires PostgreSQL infrastructure",
    ),
]
WAIT = 20


def in_thread(target: Callable[[], Any], errors: list[BaseException]) -> threading.Thread:
    def run() -> None:
        try:
            target()
        except BaseException as exc:
            errors.append(exc)
        finally:
            connections.close_all()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread


def expire(job: ProcessingJob) -> None:
    ProcessingJob.objects.filter(pk=job.pk).update(
        started_at=timezone.now() - stale_after(job.job_type) - timedelta(seconds=1)
    )


def assert_only_winner(job: ProcessingJob, storage: LocalFilesystemStorage) -> None:
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.SUCCESS, job.details
    outputs = list(FileVariant.objects.filter(source_variant_id=job.file_variant_id))
    assert len(outputs) == 1
    output = outputs[0]
    assert output.pk == job.details["output_variant_id"]
    assert output.job_id == job.job_id
    assert output.converter_options["request_options"] == {"test": "preserved"}
    assert output.source_variant is not None
    assert output.source_variant.book_id == output.book_id
    with storage.open(output.storage_identifier) as handle:
        assert handle.read() == b"winner"
    with storage.open(output.source_variant.storage_identifier) as handle:
        assert handle.read() == b"original pdf"
    files = [p for p in Path(storage.root).rglob("*") if p.is_file()]
    # Source fixture + immutable archived original + exactly one final output.
    assert sorted(p.read_bytes() for p in files) == [b"original pdf", b"original pdf", b"winner"]
    assert not list(Path(storage.root).glob("_conversion_staging/**/*.*"))


@pytest.mark.parametrize("without_hard_links", [False, True])
def test_simultaneous_deliveries_execute_converter_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, without_hard_links: bool
) -> None:
    assert connection.vendor == "postgresql"
    job, storage = create_conversion(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    barrier = threading.Barrier(6)
    calls: list[int] = []
    deliveries_returned: list[int] = []
    duplicates_returned = threading.Event()

    class BlockingConverter(StubConverter):
        def convert(self, **kwargs: Any) -> None:
            calls.append(1)
            entered.set()
            assert release.wait(WAIT)
            kwargs["destination_path"].write_bytes(b"winner")

    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": BlockingConverter(b"")})
    if without_hard_links:

        def no_link(*args: Any, **kwargs: Any) -> None:
            raise OSError(errno.EOPNOTSUPP, "mount has no hard links")

        monkeypatch.setattr("ebook_server.storage.os.link", no_link)
    errors: list[BaseException] = []

    def deliver() -> None:
        barrier.wait(WAIT)
        conversion.process_conversion_job(str(job.job_id))
        deliveries_returned.append(1)
        if len(deliveries_returned) >= 5:
            duplicates_returned.set()

    threads = [in_thread(deliver, errors) for _ in range(6)]
    try:
        assert entered.wait(WAIT)
        assert duplicates_returned.wait(WAIT)
        # A separate connection can still lock/read the RUNNING row while converting.
        from django.db import transaction

        with transaction.atomic():
            active = ProcessingJob.objects.select_for_update(nowait=True).get(pk=job.pk)
            assert active.state == ProcessingJob.JobState.RUNNING
        assert calls == [1]
    finally:
        release.set()
        for thread in threads:
            thread.join(WAIT)
    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    assert calls == [1]
    assert_only_winner(job, storage)
    conversion.process_conversion_job(str(job.job_id))
    assert calls == [1]


@pytest.mark.parametrize("old_fails", [False, True])
def test_superseded_converter_cannot_publish_or_fail_new_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, old_fails: bool
) -> None:
    assert connection.vendor == "postgresql"
    job, storage = create_conversion(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls: list[int] = []

    class TakeoverConverter(StubConverter):
        def convert(self, **kwargs: Any) -> None:
            calls.append(1)
            if len(calls) == 1:
                entered.set()
                assert release.wait(WAIT)
                if old_fails:
                    raise conversion.ConversionError("obsolete worker failed")
                kwargs["destination_path"].write_bytes(b"loser")
            else:
                kwargs["destination_path"].write_bytes(b"winner")

    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": TakeoverConverter(b"")})
    errors: list[BaseException] = []
    old = in_thread(lambda: conversion.process_conversion_job(str(job.job_id)), errors)
    try:
        assert entered.wait(WAIT)
        expire(job)
        snapshot = ProcessingJob.objects.get(pk=job.pk)
        conversion.process_conversion_job(str(job.job_id))
        job.refresh_from_db()
        winner_details = dict(job.details)
        Command()._fail(snapshot, "operator selected the earlier stale attempt")
        assert_only_winner(job, storage)
    finally:
        release.set()
        old.join(WAIT)
    assert not errors and not old.is_alive()
    assert_only_winner(job, storage)
    assert job.details == winner_details


def test_takeover_during_network_staging_never_promotes_old_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert connection.vendor == "postgresql"
    job, storage = create_conversion(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    original_store = LocalFilesystemStorage.store_atomic
    first: list[int] = []

    def blocked_store(self: LocalFilesystemStorage, source: str, destination: str) -> str:
        if not first:
            first.append(1)
            entered.set()
            assert release.wait(WAIT)
        return original_store(self, source, destination)

    monkeypatch.setattr(LocalFilesystemStorage, "store_atomic", blocked_store)
    errors: list[BaseException] = []
    old = in_thread(lambda: conversion.process_conversion_job(str(job.job_id)), errors)
    try:
        assert entered.wait(WAIT)
        expire(job)
        monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": StubConverter(b"winner")})
        conversion.process_conversion_job(str(job.job_id))
        job.refresh_from_db()
        winner_details = dict(job.details)
    finally:
        release.set()
        old.join(WAIT)
    assert not errors and not old.is_alive()
    assert_only_winner(job, storage)
    assert job.details == winner_details
