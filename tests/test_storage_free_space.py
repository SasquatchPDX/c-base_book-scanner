"""Free-space reserve on the archive volume (#22)."""

from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import override_settings

from ebook_server import storage as storage_module
from ebook_server.core import intake, views
from ebook_server.core.models import ImportJob, SourceLocation
from ebook_server.storage import (
    InsufficientStorageError,
    LocalFilesystemStorage,
    ensure_free_space,
    free_bytes,
)

RESERVE = 10_000


def _fake_free(monkeypatch: pytest.MonkeyPatch, free: int | None) -> None:
    monkeypatch.setattr(storage_module, "free_bytes", lambda _path: free)


def _source(tmp_path: Path, sizes: list[int]) -> SourceLocation:
    root = tmp_path / "incoming"
    root.mkdir()
    for index, size in enumerate(sizes):
        (root / f"book-{index}.txt").write_text("x" * size)
    return SourceLocation.objects.create(
        name=f"src-{tmp_path.name}",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.MOVE,
        default_source_type=SourceLocation.SourceType.NETWORK_SOURCE,
    )


@pytest.fixture
def archive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(root)),
    )
    monkeypatch.setattr("ebook_server.core.identification._provider_instances", lambda: [])
    return root


def test_free_bytes_uses_nearest_existing_parent(tmp_path: Path) -> None:
    available = free_bytes(tmp_path / "not" / "created" / "yet")
    assert available is not None and available > 0


@override_settings(BOOK_SCANNER_STORAGE_MIN_FREE_BYTES=RESERVE)
def test_ensure_free_space_enforces_reserve(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _fake_free(monkeypatch, RESERVE + 500)
    ensure_free_space(tmp_path, 500)
    with pytest.raises(InsufficientStorageError, match="BOOK_SCANNER_STORAGE_MIN_FREE_BYTES"):
        ensure_free_space(tmp_path, 501)

    _fake_free(monkeypatch, None)  # unknown free space is not an error
    ensure_free_space(tmp_path, 10**12)


def test_reserve_zero_disables_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _fake_free(monkeypatch, 0)
    ensure_free_space(tmp_path, 10**12)


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_STORAGE_MIN_FREE_BYTES=RESERVE)
def test_archive_refuses_to_breach_reserve_and_keeps_move_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: Path
) -> None:
    location = _source(tmp_path, [600])
    job = intake.create_source_import_job(next(Path(location.root_path).iterdir()), location)
    _fake_free(monkeypatch, RESERVE + 100)

    with pytest.raises(InsufficientStorageError):
        intake.process_import_job(str(job.id))

    job.refresh_from_db()
    assert job.state == ImportJob.State.FAILED
    assert "insufficient free space" in job.last_error
    assert Path(job.source_path).exists()
    assert not archive.exists() or not any(archive.rglob("*.txt"))


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_STORAGE_MIN_FREE_BYTES=RESERVE)
def test_budget_subtracts_reserve_and_pending_imports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: Path
) -> None:
    del archive
    location = _source(tmp_path, [300])
    intake.create_source_import_job(next(Path(location.root_path).iterdir()), location)
    _fake_free(monkeypatch, RESERVE + 1_000)

    budget = intake.archive_space_budget()

    assert budget is not None
    assert budget.remaining == 700


@pytest.mark.django_db(transaction=True)
@override_settings(BOOK_SCANNER_STORAGE_MIN_FREE_BYTES=RESERVE)
def test_ingest_sources_stops_queueing_when_archive_would_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, archive: Path
) -> None:
    del archive
    location = _source(tmp_path, [100, 100, 100])
    _fake_free(monkeypatch, RESERVE + 250)
    out, err = StringIO(), StringIO()

    call_command("ingest_sources", "--source-id", str(location.id), stdout=out, stderr=err)

    assert ImportJob.objects.filter(source_location=location).count() == 2
    assert "ingest queued=2" in out.getvalue()
    assert "insufficient free space" in err.getvalue()


@override_settings(BOOK_SCANNER_STORAGE_MIN_FREE_BYTES=RESERVE)
def test_health_reports_low_free_space_as_degraded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(views, "free_bytes", lambda _path: RESERVE - 1)
    with override_settings(APP_DATA_DIR=tmp_path):
        result = views._check_storage()

    assert result.status == "degraded"
    assert result.details is not None
    assert result.details["free_bytes"] == RESERVE - 1
    assert result.details["min_free_bytes"] == RESERVE
