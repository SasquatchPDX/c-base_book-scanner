"""Admin intake-progress page per intake channel (#7)."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from ebook_server.core import intake_progress
from ebook_server.core.models import (
    ImportJob,
    ScanJob,
    SourceLocation,
    UserRole,
    get_or_create_profile,
)


def _client(username: str, *, admin: bool) -> Client:
    profile = get_or_create_profile(username)
    if admin:
        profile.role = UserRole.ADMIN
        profile.save(update_fields=["role"])
        profile.user.is_staff = True
        profile.user.save(update_fields=["is_staff"])
    client = Client()
    client.force_login(profile.user)
    return client


def _job(source: SourceLocation | None, state: str, size: int) -> ImportJob:
    return ImportJob.objects.create(
        uploader=get_user_model().objects.get_or_create(username="importer")[0],
        source_location=source,
        source_type=(
            ImportJob.SourceType.NETWORK_SOURCE if source else ImportJob.SourceType.UPLOAD
        ),
        original_filename="x.epub",
        source_path="/x.epub",
        source_size=size,
        state=state,
    )


@pytest.fixture
def library(tmp_path: Path) -> SourceLocation:
    nas = SourceLocation.objects.create(
        name="nas", root_path=str(tmp_path), transfer_mode=SourceLocation.TransferMode.COPY
    )
    _job(nas, ImportJob.State.DISCOVERED, 1000)
    _job(nas, ImportJob.State.HASHING, 500)
    _job(nas, ImportJob.State.COMPLETE, 10)
    _job(nas, ImportJob.State.LOW_CONFIDENCE, 10)
    _job(nas, ImportJob.State.FAILED, 10)
    _job(None, ImportJob.State.COMPLETE, 10)
    _job(None, ImportJob.State.DISCOVERED, 42)
    ScanJob.objects.create(state=ScanJob.State.CAPTURING)
    return nas


@pytest.mark.django_db
def test_progress_counts_each_channel(library: SourceLocation) -> None:
    progress = intake_progress.intake_progress()
    (row,) = progress["sources"]
    assert row["source_location_id"] == library.id
    assert row["pending_files"] == 2
    assert row["pending_bytes"] == 1500
    assert (row["needs_review"], row["complete"], row["failed"]) == (1, 1, 1)
    assert row["percent_done"] == 20
    assert progress["uploads"]["pending_files"] == 1
    assert progress["uploads"]["pending_bytes"] == 42
    assert progress["uploads"]["complete"] == 1
    assert progress["scan_jobs"]["capturing"] == 1


@pytest.mark.django_db
def test_admin_sees_progress_page_and_json(library: SourceLocation) -> None:
    client = _client("intake-admin", admin=True)
    html = client.get("/library/intake/?view=html")
    assert html.status_code == 200
    body = html.content.decode()
    assert "Intake progress" in body and "nas" in body and "to copy" in body
    data = client.get("/library/intake/").json()
    assert data["sources"][0]["pending_files"] == 2


@pytest.mark.django_db
def test_progress_page_is_admin_only(library: SourceLocation) -> None:
    assert _client("member", admin=False).get("/library/intake/").status_code == 403
    anonymous = Client().get("/library/intake/")
    assert anonymous.status_code == 302


@pytest.mark.django_db
def test_cli_status_reuses_the_same_data(library: SourceLocation) -> None:
    stdout = io.StringIO()
    call_command("ingest_sources", "--status", "--json", stdout=stdout)
    (row,) = json.loads(stdout.getvalue())
    assert row == intake_progress.source_status(library)


@pytest.mark.django_db
def test_progress_query_count_does_not_grow_with_sources(
    library: SourceLocation, tmp_path: Path
) -> None:
    def _queries() -> int:
        with CaptureQueriesContext(connection) as captured:
            intake_progress.intake_progress()
        return len(captured.captured_queries)

    few = _queries()
    for index in range(10):
        extra = SourceLocation.objects.create(name=f"extra-{index}", root_path=str(tmp_path))
        _job(extra, ImportJob.State.COMPLETE, 1)
    assert _queries() == few
    rows = intake_progress.intake_progress()["sources"]
    assert len(rows) == 11
    assert next(r for r in rows if r["source_location_id"] == library.id) == (
        intake_progress.source_status(library)
    )
