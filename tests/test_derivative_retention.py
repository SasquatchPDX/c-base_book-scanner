"""Storage maintenance only touches explicitly proven superseded derivatives."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import timedelta
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.utils import timezone
from PIL import Image

from ebook_server.core import derivative_retention as retirement
from ebook_server.core import storage_inventory
from ebook_server.core.models import (
    Book,
    FileVariant,
    ProcessingJob,
    ScanCover,
    ScanJob,
    ScanLog,
    ScanPage,
    ScanPageVersion,
    ScanTailorStageState,
    SourceLocation,
    StorageLocation,
)
from ebook_server.storage import LocalFilesystemStorage


def make_retirement_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[StorageLocation, LocalFilesystemStorage, ScanTailorStageState, str]:
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    location = StorageLocation.objects.create(identifier="scan", base_path="scan-artifacts")
    storage = LocalFilesystemStorage(str(tmp_path / "scan-artifacts"))
    job = ScanJob.objects.create(state=ScanJob.State.CAPTURED)
    page = ScanPage.objects.create(scan_job=job, logical_sequence_number=1)
    raw = storage.resolve_location("raw/capture.png")
    raw.parent.mkdir(parents=True)
    Image.new("L", (32, 48), 240).save(raw)
    version = ScanPageVersion.objects.create(
        scan_page=page,
        storage_location=location,
        storage_identifier="raw/capture.png",
        checksum_sha256=hashlib.sha256(raw.read_bytes()).hexdigest(),
        mime_type="image/png",
        size_bytes=raw.stat().st_size,
    )
    state = ScanTailorStageState.objects.create(
        scan_job=job,
        scan_page=page,
        stage="deskew",
        revision=2,
        status="pending",
        input_scan_page_version=version,
        input_storage_identifier="raw/capture.png",
        output_storage_location=location,
    )
    prefix = f"scan-jobs/{job.pk}/{page.pk}/deskew"
    old = f"{prefix}/v1/preview.png"
    active = f"{prefix}/v2/output.png"
    for identifier, payload in (
        (old, b"old preview"),
        (active, b"active output"),
        ("uploads/original.epub", b"original upload"),
    ):
        path = storage.resolve_location(identifier)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        timestamp = (timezone.now() - timedelta(days=40)).timestamp()
        os.utime(path, (timestamp, timestamp))
    state.output_storage_identifier = active
    state.output_sha256 = hashlib.sha256(b"active output").hexdigest()
    state.save()
    return location, storage, state, old


def plan_for_case() -> dict[str, Any]:
    return storage_inventory.inventory(locations=["scan"], min_age_days=30)


def snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


@pytest.mark.django_db(transaction=True)
def test_inventory_and_quarantine_dry_runs_preserve_all_bytes_and_database_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, storage, _, old = make_retirement_case(tmp_path, monkeypatch)
    before = snapshot(tmp_path)
    count = StorageLocation.objects.count()
    report = plan_for_case()
    assert report["errors"] == []
    eligible = [row for row in report["findings"] if row["eligible"]]
    assert [row["identifier"] for row in eligible] == [old], [
        r["reason"] for r in report["findings"] if r["identifier"] == old
    ]
    assert eligible[0]["kind"] == "stage_preview" and eligible[0]["age_days"] >= 39
    assert eligible[0]["bytes"] == len(b"old preview")
    assert retirement.collect_plan(report) == [{"identifier": old, "status": "would_quarantine"}]
    output = StringIO()
    call_command("inventory_storage", locations=["scan"], stdout=output)
    assert json.loads(output.getvalue())["errors"] == []
    assert StorageLocation.objects.count() == count
    assert snapshot(tmp_path) == before
    assert storage.exists(old)


@pytest.mark.django_db(transaction=True)
def test_quarantine_and_restore_are_reversible_and_never_purge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location, storage, _, old = make_retirement_case(tmp_path, monkeypatch)
    before = snapshot(tmp_path)
    result = retirement.collect_plan(plan_for_case(), apply=True)[0]
    assert result["status"] == "quarantined"
    assert not storage.exists(old)
    assert storage.exists("raw/capture.png")
    assert retirement.restore_manifest(location, result["manifest"])["status"] == "would_restore"
    assert not storage.exists(old)
    restored = retirement.restore_manifest(location, result["manifest"], apply=True)
    assert restored["status"] == "restored"
    after = snapshot(tmp_path)
    assert all(after[identifier] == data for identifier, data in before.items())
    assert any("manifest.json" in identifier for identifier in after)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    "protection",
    [
        "new_reference",
        "active_job",
        "active_stage",
        "finalizing",
        "source_root",
        "readonly_alias",
        "file_change",
    ],
)
def test_collection_rechecks_new_protection_after_the_inventory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protection: str
) -> None:
    location, storage, state, old = make_retirement_case(tmp_path, monkeypatch)
    report = plan_for_case()
    if protection == "new_reference":
        state.preview_storage_location = location
        state.preview_storage_identifier = old
        state.save()
    elif protection == "active_job":
        ProcessingJob.objects.create(job_type="stage", details={"scan_job_id": state.scan_job_id})
    elif protection == "active_stage":
        state.status = "running"
        state.save()
    elif protection == "finalizing":
        ScanJob.objects.filter(pk=state.scan_job_id).update(state="finalizing")
    elif protection == "source_root":
        SourceLocation.objects.create(name="do-not-touch", root_path=storage.root)
    elif protection == "readonly_alias":
        StorageLocation.objects.create(identifier="indexed", base_path=storage.root, read_only=True)
    else:
        storage.resolve_location(old).write_bytes(b"changed since inventory")
    result = retirement.collect_plan(report, apply=True)[0]
    assert result["status"] == "skipped"
    assert storage.exists(old)
    assert not storage.resolve_location(".quarantine").exists()


@pytest.mark.django_db(transaction=True)
def test_missing_scanner_cover_log_and_rendered_log_are_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location, _, state, _ = make_retirement_case(tmp_path, monkeypatch)
    ScanCover.objects.create(
        scan_job_id=state.scan_job_id,
        position="front",
        storage_location=location,
        storage_identifier="covers/missing.png",
        checksum_sha256="0" * 64,
        mime_type="image/png",
        size_bytes=20,
    )
    ScanLog.objects.create(
        scan_job_id=state.scan_job_id,
        state="stored",
        storage_location=location,
        storage_identifier="logs/missing.log",
        original_filename="scan.log",
        size_bytes=10,
    )
    ScanJob.objects.filter(pk=state.scan_job_id).update(
        scan_jsonl_job_identifier="logs/missing.jsonl"
    )
    report = plan_for_case()
    missing = {row["kind"] for row in report["findings"] if row["status"] == "missing"}
    assert {"scan_cover", "scan_log", "rendered_scan_log"} <= missing
    with pytest.raises(CommandError, match="storage verification failed"):
        call_command("verify_storage", stdout=StringIO(), stderr=StringIO())


@pytest.mark.django_db(transaction=True)
def test_disagreeing_reference_digests_do_not_hide_a_bad_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location, storage, state, _ = make_retirement_case(tmp_path, monkeypatch)
    FileVariant.objects.create(
        book=Book.objects.create(title="Same physical file"),
        format="png",
        storage_location=location,
        storage_identifier=state.output_storage_identifier,
        sha256="0" * 64,
        size_bytes=10,
        source_type="upload",
    )
    report = storage_inventory.inventory(locations=["scan"], verify_hashes=True)
    row = next(
        row for row in report["findings"] if row["identifier"] == state.output_storage_identifier
    )
    assert row["status"] == "hash_mismatch"
    assert storage.hash(state.output_storage_identifier) == state.output_sha256


@pytest.mark.django_db(transaction=True)
@pytest.mark.skipif(
    os.path.normcase("scan") == os.path.normcase("SCAN"), reason="case-sensitive filesystem"
)
def test_case_distinct_paths_do_not_hide_a_missing_reference_or_rebind_a_plan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location, storage, state, _ = make_retirement_case(tmp_path, monkeypatch)
    FileVariant.objects.create(
        book=Book.objects.create(title="Wrong case"),
        format="png",
        storage_location=location,
        storage_identifier=state.output_storage_identifier.replace("output", "OUTPUT"),
        sha256="0" * 64,
        size_bytes=10,
        source_type="upload",
    )
    report = plan_for_case()
    assert any(
        row["status"] == "missing" and "OUTPUT" in row["identifier"] for row in report["findings"]
    )
    report["roots"]["scan"] = storage.root.replace("scan-artifacts", "SCAN-ARTIFACTS")
    with pytest.raises(retirement.RetentionError, match="root changed"):
        retirement.collect_plan(report, apply=True)


@pytest.mark.django_db(transaction=True)
def test_restore_never_overwrites_a_recreated_original_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location, storage, _, old = make_retirement_case(tmp_path, monkeypatch)
    result = retirement.collect_plan(plan_for_case(), apply=True)[0]
    storage.resolve_location(old).write_bytes(b"new writer owns this path")
    with pytest.raises(retirement.RetentionError, match="occupied"):
        retirement.restore_manifest(location, result["manifest"], apply=True)
    assert storage.resolve_location(old).read_bytes() == b"new writer owns this path"
    assert storage.exists(result["manifest"].replace("manifest.json", "artifact"))


@pytest.mark.django_db(transaction=True)
def test_crash_after_move_keeps_a_planned_manifest_that_can_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    location, storage, _, old = make_retirement_case(tmp_path, monkeypatch)
    report = plan_for_case()
    real_replace = LocalFilesystemStorage.replace_bytes

    def failed_journal(self: LocalFilesystemStorage, identifier: str, data: bytes) -> str:
        if identifier.endswith("manifest.json"):
            raise OSError("simulated interruption after move")
        return real_replace(self, identifier, data)

    with monkeypatch.context() as patcher:
        patcher.setattr(LocalFilesystemStorage, "replace_bytes", failed_journal)
        with pytest.raises(retirement.RetentionError, match="retain and inspect"):
            retirement.collect_plan(report, apply=True)
    manifests = list(storage.resolve_location(".quarantine").rglob("manifest.json"))
    assert len(manifests) == 1
    assert json.loads(manifests[0].read_text())["status"] == "planned"
    manifest_id = manifests[0].relative_to(storage.root).as_posix()
    retirement.restore_manifest(location, manifest_id, apply=True)
    assert storage.resolve_location(old).read_bytes() == b"old preview"


@pytest.mark.django_db(transaction=True)
def test_forged_raw_candidate_and_malformed_plan_cannot_move_originals(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, storage, _, old = make_retirement_case(tmp_path, monkeypatch)
    report = plan_for_case()
    raw = "raw/capture.png"
    row = next(row for row in report["findings"] if row["identifier"] == raw)
    row.update(eligible=True, sha256=storage.hash(raw))
    before = snapshot(tmp_path)
    # Validate every candidate before executing even the first valid candidate.
    report["findings"].append({"eligible": True, "location": "scan"})
    with pytest.raises(retirement.RetentionError, match="invalid quarantine candidate"):
        retirement.collect_plan(report, apply=True)
    assert snapshot(tmp_path) == before
    report["findings"].pop()
    results = retirement.collect_plan(report, apply=True)
    assert next(result for result in results if result["identifier"] == raw)["reason"] == (
        "unknown stage ownership"
    )
    assert storage.exists(raw)
    assert not storage.exists(old)


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("corruption", ["non_object", "missing_root", "invalid_digest", "bytes"])
def test_restore_rejects_corrupt_journal_without_touching_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, corruption: str
) -> None:
    location, storage, _, _ = make_retirement_case(tmp_path, monkeypatch)
    result = retirement.collect_plan(plan_for_case(), apply=True)[0]
    path = storage.resolve_location(result["manifest"])
    journal = json.loads(path.read_text(encoding="utf-8"))
    if corruption == "missing_root":
        journal.pop("root")
    elif corruption == "invalid_digest":
        journal["sha256"] = "bad"
    elif corruption == "bytes":
        journal["bytes"] = True
    else:
        journal = []
    path.write_text(json.dumps(journal), encoding="utf-8")
    before = snapshot(tmp_path)
    with pytest.raises(retirement.RetentionError, match="invalid quarantine journal"):
        retirement.restore_manifest(location, result["manifest"], apply=True)
    assert snapshot(tmp_path) == before
