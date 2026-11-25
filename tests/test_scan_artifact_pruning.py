"""ScanTailor-Web stage files of superseded revisions are deleted (#204)."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest
from django.core.management import CommandError, call_command
from django.test import Client, override_settings
from PIL import Image, ImageDraw

from ebook_server.core import scan_artifact_pruning, scan_lifecycle, scantailor
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    ScanJob,
    ScanPage,
    ScanTailorStageState,
)
from ebook_server.storage import LocalFilesystemStorage
from tests.test_scan_jobs import (
    _create_scan_job,
    _create_scan_job_with_book,
    _upload_scan_page,
)

ONE_DAY = 24 * 3600


@pytest.fixture(autouse=True)
def _isolated_app_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # The sweep walks the whole scan storage: never point it at shared test data.
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    return tmp_path


def _scan_root(tmp_path: Path) -> Path:
    return tmp_path / "scan-artifacts"


def _page_png() -> bytes:
    image = Image.new("L", (360, 240), 255)
    draw = ImageDraw.Draw(image)
    for top in range(30, 210, 20):
        draw.rectangle((30, top, 330, top + 8), fill=0)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _job_with_page(user: str) -> tuple[Client, ScanJob, ScanPage]:
    client, job = _create_scan_job(user)
    response = _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=_page_png(), filename="p.png"
    )
    assert response.status_code in {200, 201}
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    return client, job, page


def _stage_url(job: ScanJob, page: ScanPage, stage: str) -> str:
    return f"/scan/jobs/{job.id}/pages/{page.id}/stages/{stage}/"


def _save_deskew(client: Client, job: ScanJob, page: ScanPage, angle: float) -> None:
    response = client.post(
        _stage_url(job, page, "deskew"),
        data=json.dumps({"parameters": {"mode": "manual", "manual_angle": angle}}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content


def _files_under(root: Path) -> set[str]:
    if not root.exists():
        return set()
    return {path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()}


def _stage_dir(tmp_path: Path, job: ScanJob, page: ScanPage, stage: str) -> Path:
    return _scan_root(tmp_path) / "scan-jobs" / str(job.id) / str(page.id) / stage


def _write(root: Path, identifier: str, data: bytes = b"x" * 100, *, age: int = ONE_DAY) -> None:
    path = root / identifier
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    if age:
        old = path.stat().st_mtime - age
        os.utime(path, (old, old))


@pytest.mark.django_db(transaction=True)
def test_saving_many_edits_keeps_only_the_current_revision(tmp_path: Path) -> None:
    client, job, page = _job_with_page("prune-many-edits")
    for angle in (0.5, 1.0, 1.5, 2.0, 2.5):
        _save_deskew(client, job, page, angle)

    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert state.status == ScanTailorStageState.StageStatus.SUCCESS
    assert state.revision >= 5
    assert _files_under(_stage_dir(tmp_path, job, page, "deskew")) == {
        f"v{state.revision}/work.png",
        f"v{state.revision}/preview.jpg",
        f"v{state.revision}/thumbnail.jpg",
    }
    assert state.output_storage_identifier.endswith(f"/v{state.revision}/work.png")

    # The page still shows its result.
    preview = client.get(_stage_url(job, page, "deskew") + "preview/")
    assert preview.status_code == 200
    assert preview["Content-Type"] == "image/jpeg"


@pytest.mark.django_db(transaction=True)
def test_closed_job_removes_its_revision_but_keeps_referenced_files(tmp_path: Path) -> None:
    client, job, page = _job_with_page("prune-invalidated")
    _save_deskew(client, job, page, 1.0)
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    current = state.output_storage_identifier
    root = _scan_root(tmp_path)
    prefix = f"scan-jobs/{job.id}/{page.id}/deskew"
    stale = f"{prefix}/v{state.revision + 1}"
    for name in ("work.png", "preview.jpg", "thumbnail.jpg"):
        _write(root, f"{stale}/{name}", age=0)
    # Another row (of another stage) still refers to one of them.
    other = scantailor.get_or_create_stage_state(
        scan_job_id=str(job.id), page_id=str(page.id), stage="select_content"
    )
    other.input_storage_identifier = f"{stale}/work.png"
    other.save(update_fields=["input_storage_identifier"])

    job_row = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.STAGE,
        state=ProcessingJob.JobState.RUNNING,
        details={
            "scan_tailor_stage_state_id": state.id,
            "stage": "deskew",
            "stage_revision": state.revision + 1,
        },
    )
    scantailor._fail_stage_job(job_row, "boom")

    assert _files_under(root / stale) == {"work.png"}
    # The current revision is untouched.
    assert (root / current).is_file()

    # A superseded job of a revision nobody refers to removes everything it left.
    other.input_storage_identifier = ""
    other.save(update_fields=["input_storage_identifier"])
    superseded = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.STAGE,
        state=ProcessingJob.JobState.QUEUED,
        details={"scan_tailor_stage_state_id": state.id, "stage_revision": state.revision + 1},
    )
    scantailor._supersede_stage_job(superseded, dict(superseded.details), "edited")
    assert not (root / stale).exists()
    assert (root / current).is_file()


@pytest.mark.django_db(transaction=True)
def test_failed_rerun_keeps_the_published_output(tmp_path: Path) -> None:
    client, job, page = _job_with_page("prune-failed-rerun")
    _save_deskew(client, job, page, 1.0)
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    job_row = ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.STAGE,
        state=ProcessingJob.JobState.RUNNING,
        details={"scan_tailor_stage_state_id": state.id, "stage_revision": state.revision},
    )
    scantailor._fail_stage_job(job_row, "boom")
    root = _scan_root(tmp_path)
    assert (root / state.output_storage_identifier).is_file()
    assert (root / state.preview_storage_identifier).is_file()
    assert (root / scantailor.stage_thumbnail_identifier(state)).is_file()


@pytest.mark.django_db(transaction=True)
def test_sweep_deletes_unreferenced_files_and_dry_run_deletes_nothing(tmp_path: Path) -> None:
    client, job, page = _job_with_page("prune-sweep")
    _save_deskew(client, job, page, 1.0)
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    root = _scan_root(tmp_path)
    referenced = {
        state.output_storage_identifier,
        state.preview_storage_identifier,
        scantailor.stage_thumbnail_identifier(state),
    }
    for identifier in referenced:
        old = (root / identifier).stat().st_mtime - ONE_DAY
        os.utime(root / identifier, (old, old))
    prefix = f"scan-jobs/{job.id}/{page.id}"
    orphans = {
        f"{prefix}/deskew/v{state.revision + 7}/work.png": 1000,
        f"{prefix}/output/v3/output.png": 2000,
        f"{prefix}/margins/v2/.{'a' * 32}.output.partial": 300,
        # A deleted job: no rows at all.
        "scan-jobs/20260101T000000Z/00000000-0000-4000-8000-000000000000/deskew/v1/work.png": 400,
    }
    for identifier, size in orphans.items():
        _write(root, identifier, b"o" * size)
    young = f"{prefix}/margins/v9/work.png"
    _write(root, young, age=0)
    # Originals in the job folder never match the stage layout.
    raw_pages = {
        identifier
        for identifier in _files_under(root)
        if identifier.startswith(f"scan-jobs/{job.id}/")
        and not identifier.startswith(f"scan-jobs/{job.id}/{page.id}/")
    }
    assert raw_pages
    before = _files_under(root)

    out = io.StringIO()
    call_command("prune_scan_artifacts", "--dry-run", stdout=out)
    assert _files_under(root) == before
    assert f"would delete {len(orphans)} file(s), {sum(orphans.values())} bytes" in out.getvalue()

    report = scan_artifact_pruning.sweep()
    assert report.files == len(orphans)
    assert report.bytes == sum(orphans.values())
    after = _files_under(root)
    assert after == before - set(orphans)
    assert referenced <= after
    assert raw_pages <= after
    assert young in after
    assert not (root / "scan-jobs" / "20260101T000000Z").exists()


@pytest.mark.django_db(transaction=True)
def test_stage_save_below_free_space_threshold_is_a_readable_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, job, page = _job_with_page("prune-low-space")
    _save_deskew(client, job, page, 1.0)
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    jobs_before = ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.STAGE).count()
    monkeypatch.setattr(LocalFilesystemStorage, "free_bytes", lambda self: 10 * 1024**2)
    with override_settings(BOOK_SCANNER_SCANTAILOR_MIN_FREE_BYTES=1024**3):
        response = client.post(
            _stage_url(job, page, "deskew"),
            data=json.dumps({"parameters": {"mode": "manual", "manual_angle": 3.0}}),
            content_type="application/json",
        )
        batch = client.post(
            f"/scan/jobs/{job.id}/stages/deskew/batch/",
            data=json.dumps({"parameters": {"mode": "manual", "manual_angle": 3.0}}),
            content_type="application/json",
        )
    for refused in (response, batch):
        assert refused.status_code == 507
        assert refused["Content-Type"].startswith("text/plain")
        assert refused.content.decode().startswith("Disk almost full")
    unchanged = ScanTailorStageState.objects.get(pk=state.pk)
    assert unchanged.revision == state.revision
    assert unchanged.parameters == state.parameters
    assert ProcessingJob.objects.filter(job_type=ProcessingJob.JobType.STAGE).count() == jobs_before


@pytest.mark.django_db(transaction=True)
def test_cancelled_job_loses_its_stage_files_but_keeps_raw_pages(tmp_path: Path) -> None:
    client, job, page = _job_with_page("prune-cancel")
    _save_deskew(client, job, page, 1.0)
    root = _scan_root(tmp_path)
    stage_root = root / "scan-jobs" / str(job.id) / str(page.id)
    assert _files_under(stage_root)
    version = page.active_version()
    assert version is not None
    raw = root / version.storage_identifier
    assert raw.is_file()

    scan_lifecycle.abort(str(job.pk))

    assert not _files_under(stage_root)
    assert raw.is_file()
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert state.output_storage_identifier == ""
    assert state.preview_storage_identifier == ""
    assert state.status == ScanTailorStageState.StageStatus.INVALIDATED
    assert state.invalidated_reason == scan_artifact_pruning.JOB_END_REASON


@pytest.mark.django_db(transaction=True)
def test_job_cleanup_skips_jobs_that_have_not_ended(tmp_path: Path) -> None:
    client, job, page = _job_with_page("prune-open-job")
    _save_deskew(client, job, page, 1.0)
    assert scan_artifact_pruning.prune_scan_job_stage_artifacts(str(job.pk)) == 0
    state = ScanTailorStageState.objects.get(scan_job=job, scan_page=page, stage="deskew")
    assert (_scan_root(tmp_path) / state.output_storage_identifier).is_file()
    assert scan_artifact_pruning.prune_scan_job_stage_artifacts("../etc") == 0


@pytest.mark.django_db(transaction=True)
def test_finished_job_loses_its_stage_files_once_the_book_file_exists(tmp_path: Path) -> None:
    book = Book.objects.create(title="Pruned after finish")
    client, job = _create_scan_job_with_book("prune-finish", book_id=book.id)
    _upload_scan_page(
        client, str(job.id), sequence=1, side="single", payload=_page_png(), filename="p.png"
    )
    page = ScanPage.objects.get(scan_job=job, logical_sequence_number=1, side="single")
    _save_deskew(client, job, page, 1.0)
    stage_root = _scan_root(tmp_path) / "scan-jobs" / str(job.id) / str(page.id)
    assert _files_under(stage_root)

    finish = client.post(
        f"/scan/jobs/{job.id}/finish/",
        data={"create_cleaned_variant": "true", "allow_raw_pages": "true"},
    )
    assert finish.status_code == 202, finish.content.decode()

    job.refresh_from_db()
    assert job.state == ScanJob.State.COMPLETE
    assert FileVariant.objects.filter(book=book, source_type=FileVariant.SourceType.CLEANED)
    assert not _files_under(stage_root)
    version = page.active_version()
    assert version is not None
    assert (_scan_root(tmp_path) / version.storage_identifier).is_file()


def _save_select_content(client: Client, job: ScanJob, page: ScanPage) -> None:
    response = client.post(
        _stage_url(job, page, "select_content"),
        data=json.dumps({"parameters": {}}),
        content_type="application/json",
    )
    assert response.status_code == 202, response.content


@pytest.mark.django_db(transaction=True)
def test_rerun_of_an_invalidated_stage_releases_the_upstream_revision_it_used(
    tmp_path: Path,
) -> None:
    client, job, page = _job_with_page("prune-upstream")
    _save_deskew(client, job, page, 1.0)
    _save_select_content(client, job, page)
    first_deskew = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    held_input = first_deskew.output_storage_identifier

    # A new deskew invalidates Select Content, which still names the old deskew output.
    _save_deskew(client, job, page, 2.0)
    content = ScanTailorStageState.objects.get(scan_page=page, stage="select_content")
    assert content.status == ScanTailorStageState.StageStatus.INVALIDATED
    assert content.input_storage_identifier == held_input
    root = _scan_root(tmp_path)
    assert (root / held_input).is_file()  # referenced by another row: kept

    # Rerunning Select Content binds it to the new deskew output; the old one goes.
    _save_select_content(client, job, page)
    deskew = ScanTailorStageState.objects.get(scan_page=page, stage="deskew")
    content.refresh_from_db()
    assert content.status == ScanTailorStageState.StageStatus.SUCCESS
    assert content.input_storage_identifier == deskew.output_storage_identifier
    assert _files_under(_stage_dir(tmp_path, job, page, "deskew")) == {
        f"v{deskew.revision}/work.png",
        f"v{deskew.revision}/preview.jpg",
        f"v{deskew.revision}/thumbnail.jpg",
    }
    assert _files_under(_stage_dir(tmp_path, job, page, "select_content")) == {
        f"v{content.revision}/work.png",
        f"v{content.revision}/preview.jpg",
        f"v{content.revision}/thumbnail.jpg",
    }


@pytest.mark.django_db(transaction=True)
def test_sweep_over_the_entry_limit_fails_and_deletes_nothing(tmp_path: Path) -> None:
    root = _scan_root(tmp_path)
    orphans = [
        f"scan-jobs/20260101T000000Z/00000000-0000-4000-8000-000000000000/deskew/v{n}/work.png"
        for n in range(1, 6)
    ]
    for identifier in orphans:
        _write(root, identifier)
    with pytest.raises(CommandError, match="nothing deleted"):
        call_command("prune_scan_artifacts", "--limit", "3", stdout=io.StringIO())
    assert set(orphans) <= _files_under(root)
