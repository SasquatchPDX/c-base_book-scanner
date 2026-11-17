"""Books split across per-chapter files in one folder (#60)."""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from io import StringIO
from pathlib import Path
from typing import Any

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from ebook_server.core import conversion, intake
from ebook_server.core.intake import discover_source_jobs_for_location
from ebook_server.core.models import (
    Book,
    FileVariant,
    ImportGroup,
    ImportJob,
    ReviewTask,
    SourceLocation,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.core.multipart import detect_multipart_groups
from ebook_server.storage import LocalFilesystemStorage

SAMPLES = Path(__file__).parent / "fixtures" / "real_world_samples"

ABSOLUTE_JAVA = [f"ch{n:02d}_noPW.pdf" for n in range(1, 21)] + [
    f"Appendix{n}_noPW.pdf" for n in range(1, 6)
]
CISSP = (
    ["FM.pdf"]
    + [f"ch{n:02d}.pdf" for n in range(1, 13)]
    + ["glossary.pdf", "Index.pdf", "AppA.pdf"]
)


# --- detector ---------------------------------------------------------------------


def test_detects_absolute_java_chapters_then_appendices() -> None:
    [group] = detect_multipart_groups(ABSOLUTE_JAVA)

    assert group.format_name == "pdf"
    assert [part.filename for part in group.parts] == ABSOLUTE_JAVA
    assert [part.index for part in group.parts] == list(range(1, 26))
    assert group.parts[0].label == "Chapter 1"
    assert group.parts[-1].label == "Appendix 5"
    assert group.missing_numbers == ()


def test_detects_cissp_with_front_and_back_matter_in_reading_order() -> None:
    [group] = detect_multipart_groups(CISSP)

    assert [part.filename for part in group.parts] == (
        ["FM.pdf"]
        + [f"ch{n:02d}.pdf" for n in range(1, 13)]
        + ["AppA.pdf", "glossary.pdf", "Index.pdf"]
    )
    assert [part.label for part in group.parts][:2] == ["Front matter", "Chapter 1"]
    assert group.parts[-3].label == "Appendix A"


def test_false_alarm_single_complete_novel_is_not_a_split_work() -> None:
    # #60: one complete novel in a folder named "CHAPTER ONE"; only the file counts.
    names = ["Harry Potter and the Sorcerer's Stone - CHAPTER ONE.txt", "metadata.opf"]

    assert detect_multipart_groups(names) == []


@pytest.mark.parametrize(
    "names",
    [
        ["Saga Book 1.epub", "Saga Book 2.epub", "Saga Book 3.epub"],  # series volumes
        ["Saga Vol 1.pdf", "Saga Vol 2.pdf", "Saga Vol 3.pdf"],
        ["ch01.pdf", "ch02.pdf"],  # fewer than three numbered parts
        ["ch01.pdf", "ch02.txt", "ch03.epub"],  # no consistent format
        ["ch01.pdf", "ch01 (1).pdf", "ch02.pdf"],  # duplicate numbers
        ["Index.pdf", "glossary.pdf", "AppA.pdf", "AppB.pdf"],  # no numbered core
        ["Chinese Cooking.pdf", "Chess Openings.pdf", "Partisan Review.pdf"],
        ["henry4 part1.txt"],
    ],
)
def test_detector_rejects_non_split_folders(names: list[str]) -> None:
    assert detect_multipart_groups(names) == []


def test_detector_reports_numbering_gaps_and_roman_parts() -> None:
    [group] = detect_multipart_groups(
        ["Novel Part I.txt", "Novel Part II.txt", "Novel Part IV.txt"]
    )

    assert [part.label for part in group.parts] == ["Part 1", "Part 2", "Part 4"]
    assert group.missing_numbers == (3,)


def test_detector_keeps_formats_apart() -> None:
    names = [f"Chapter {n}.txt" for n in range(1, 4)] + [f"Chapter {n}.epub" for n in range(1, 4)]

    groups = detect_multipart_groups(names)

    assert sorted(group.format_name for group in groups) == ["epub", "txt"]
    assert all(len(group.parts) == 3 for group in groups)


def test_real_world_sample_precision() -> None:
    """Only the two known per-chapter books in the real listings are flagged."""
    flagged: dict[str, int] = {}
    for csv_name in ("ebooks-share-filetree.csv", "calibre-ebooks-share-filetree.csv"):
        folders: dict[str, list[str]] = defaultdict(list)
        with (SAMPLES / csv_name).open(encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if intake._is_supported_file(Path(row["filename"])):
                    folders[row["folder"]].append(row["filename"])
        for folder, names in folders.items():
            for group in detect_multipart_groups(names):
                flagged[folder] = len(group.parts)

    assert flagged == {
        "misc-01/Books/Absolute Java": 25,
        "misc-01/Books/All in One CISSP Exam Guide": 16,
    }


# --- intake and review --------------------------------------------------------------


def _stub_provider(monkeypatch: pytest.MonkeyPatch, *, title: str, authors: list[str]) -> None:
    class StubProvider:
        provider_id = "stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, Any]]:
            del title, author, isbn, folder_hint
            return [{"title": candidate_title, "author_name": authors}]

    candidate_title = title
    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )


def _library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> LocalFilesystemStorage:
    storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    monkeypatch.setattr("ebook_server.core.intake._default_storage", lambda: storage)
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    return storage


def _split_book_folder(root: Path) -> tuple[Path, list[Path]]:
    folder = root / "Learning Widgets"
    folder.mkdir(parents=True)
    parts = []
    for name in ("ch01.txt", "ch02.txt", "ch03.txt", "AppA.txt"):
        path = folder / name
        path.write_text(f"Learning Widgets, {name}: plain text of this part of the book.\n")
        parts.append(path)
    return folder, parts


def _location(root: Path, mode: str) -> SourceLocation:
    return SourceLocation.objects.create(
        name=f"src-{root.name}-{mode}",
        root_path=str(root),
        transfer_mode=mode,
        recursive=True,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
    )


def _ingest(location: SourceLocation) -> None:
    call_command("ingest_sources", "--source-id", str(location.id), stdout=StringIO())


def _admin_client(username: str = "multipart-admin") -> Client:
    profile = get_or_create_profile(username, role="admin")
    client = Client()
    client.force_login(profile.user)
    return client


@pytest.mark.django_db(transaction=True)
def test_split_folder_opens_one_multipart_review_and_keeps_copy_sources(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _folder, parts = _split_book_folder(root)
    (root / "Other Book.txt").write_text("A different, complete book.\n")
    location = _location(root, SourceLocation.TransferMode.COPY)

    _ingest(location)

    group = ImportGroup.objects.get()
    assert group.state == ImportGroup.State.PENDING
    jobs = list(group.import_jobs.order_by("part_index"))
    assert [job.original_filename for job in jobs] == [
        "ch01.txt",
        "ch02.txt",
        "ch03.txt",
        "AppA.txt",
    ]
    assert [job.part_label for job in jobs] == ["Chapter 1", "Chapter 2", "Chapter 3", "Appendix A"]
    for job in jobs:
        assert job.state == ImportJob.State.LOW_CONFIDENCE
        assert storage.hash(job.storage_identifier) == job.sha256
        assert not ReviewTask.objects.filter(
            import_job=job, task_type=ReviewTask.TaskType.NEEDS_ATTENTION
        ).exists()
    assert all(path.exists() for path in parts)  # COPY never removes sources

    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    assert task.import_job_id == jobs[0].id
    assert task.state == ReviewTask.State.OPEN
    assert task.inferred_title == "Learning Widgets"
    assert task.import_job is not None
    assert task.import_job.metadata_candidates.count() == 1
    assert len(task.embedded_metadata["parts"]) == 4

    # The unrelated book in the share root went through the ordinary pipeline.
    other = ImportJob.objects.get(original_filename="Other Book.txt")
    assert other.import_group_id is None
    assert ReviewTask.objects.filter(import_job=other).count() <= 1
    assert ReviewTask.objects.filter(task_type=ReviewTask.TaskType.MULTIPART_WORK).count() == 1


@pytest.mark.django_db(transaction=True)
def test_move_source_parts_are_removed_only_after_verified_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _folder, parts = _split_book_folder(root)
    location = _location(root, SourceLocation.TransferMode.MOVE)
    monkeypatch.setattr(intake, "_verify_archive", lambda *_args: False)

    jobs = discover_source_jobs_for_location(location)
    for job in jobs:
        with pytest.raises(RuntimeError, match="archive verification failed"):
            intake.process_import_job(str(job.id))

    assert all(path.exists() for path in parts)
    assert set(ImportJob.objects.values_list("state", flat=True)) == {ImportJob.State.FAILED}
    assert not ReviewTask.objects.exists()

    monkeypatch.undo()
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    with override_settings(BOOK_SCANNER_QUEUE_RETRY_MAX=3):
        for job in jobs:
            intake.process_import_job(str(job.id))

    assert not any(path.exists() for path in parts)
    assert set(ImportJob.objects.values_list("state", flat=True)) == {
        ImportJob.State.LOW_CONFIDENCE
    }
    assert ReviewTask.objects.filter(task_type=ReviewTask.TaskType.MULTIPART_WORK).count() == 1


@pytest.mark.django_db(transaction=True)
def test_confirming_creates_one_book_with_ordered_part_variants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _split_book_folder(root)
    _ingest(_location(root, SourceLocation.TransferMode.COPY))
    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    assert task.import_job is not None
    jobs = {job.original_filename: job for job in ImportJob.objects.all()}
    client = _admin_client()

    # Reviewer moves the appendix to the front and relabels it.
    order = [
        {"import_job_id": str(jobs["AppA.txt"].id), "part_index": 1, "part_label": "Preface"},
        {"import_job_id": str(jobs["ch01.txt"].id), "part_index": 2, "part_label": "Chapter 1"},
        {"import_job_id": str(jobs["ch02.txt"].id), "part_index": 3, "part_label": "Chapter 2"},
        {"import_job_id": str(jobs["ch03.txt"].id), "part_index": 4, "part_label": "Chapter 3"},
    ]
    response = client.post(
        f"/reviews/{task.id}/multipart/order/",
        data=json.dumps({"parts": order}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    detail = client.get(f"/reviews/{task.id}/").json()
    assert [part["filename"] for part in detail["multipart"]["parts"]][0] == "AppA.txt"

    candidate = task.import_job.metadata_candidates.get()
    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        data=json.dumps({"candidate_id": candidate.id}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content

    book = Book.objects.get()
    assert book.title == "Learning Widgets"
    variants = list(book.file_variants.order_by("part_index"))
    assert [(v.part_index, v.part_label, v.original_filename) for v in variants] == [
        (1, "Preface", "AppA.txt"),
        (2, "Chapter 1", "ch01.txt"),
        (3, "Chapter 2", "ch02.txt"),
        (4, "Chapter 3", "ch03.txt"),
    ]
    assert len({v.sha256 for v in variants}) == 4
    assert set(ImportJob.objects.values_list("state", flat=True)) == {ImportJob.State.COMPLETE}
    assert set(ImportJob.objects.values_list("book_id", flat=True)) == {book.id}
    group = ImportGroup.objects.get()
    assert group.state == ImportGroup.State.CONFIRMED
    assert group.book_id == book.id
    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.ACCEPT_CANDIDATE
    assert sorted(task.decision_payload["multipart_confirmed"]["file_variant_ids"]) == sorted(
        v.id for v in variants
    )

    payload = client.get(f"/library/books/{book.id}/").json()
    assert [variant["part_label"] for variant in payload["variants"]] == [
        "Preface",
        "Chapter 1",
        "Chapter 2",
        "Chapter 3",
    ]
    # A single chapter is never offered or accepted as a whole-book conversion.
    assert conversion.conversion_options_for_book(book) == []
    with pytest.raises(conversion.ConversionError):
        conversion.request_conversion(source_variant=variants[0], target_format="epub")


@pytest.mark.django_db(transaction=True)
def test_manual_entry_confirms_parts_and_is_refused_while_parts_are_in_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _split_book_folder(root)
    _ingest(_location(root, SourceLocation.TransferMode.COPY))
    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    client = _admin_client()

    late = ImportJob.objects.get(original_filename="ch03.txt")
    ImportJob.objects.filter(pk=late.pk).update(state=ImportJob.State.HASHING)
    response = client.post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps({"title": "Learning Widgets", "authors": ["Ada Example"]}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert not Book.objects.exists()

    ImportJob.objects.filter(pk=late.pk).update(state=ImportJob.State.LOW_CONFIDENCE)
    response = client.post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps({"title": "Learning Widgets", "authors": ["Ada Example"]}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    book = Book.objects.get()
    assert book.file_variants.filter(part_index__isnull=False).count() == 4


@pytest.mark.django_db(transaction=True)
def test_rejecting_grouping_identifies_each_file_independently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Unrelated Title", authors=["Someone Else"])
    root = tmp_path / "share"
    _folder, parts = _split_book_folder(root)
    _ingest(_location(root, SourceLocation.TransferMode.COPY))
    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    client = _admin_client()

    response = client.post(f"/reviews/{task.id}/multipart/reject/", data={"notes": "anthology"})
    assert response.status_code == 200, response.content

    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.SPLIT_PARTS
    assert ImportGroup.objects.get().state == ImportGroup.State.REJECTED
    per_file = ReviewTask.objects.filter(task_type=ReviewTask.TaskType.NEEDS_ATTENTION)
    assert sorted(t.source_filename for t in per_file) == sorted(p.name for p in parts)
    assert all(t.state == ReviewTask.State.OPEN for t in per_file)
    assert not Book.objects.exists()
    assert not FileVariant.objects.exists()
    assert all(path.exists() for path in parts)

    # A second rejection of the same group is refused.
    response = client.post(f"/reviews/{task.id}/multipart/reject/")
    assert response.status_code == 400


@pytest.mark.django_db(transaction=True)
def test_false_alarm_chapter_named_folder_is_an_ordinary_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Unrelated Title", authors=["Someone Else"])
    root = tmp_path / "share"
    folder = root / "CHAPTER ONE" / "Harry Potter and the Sorcerer's Stone (196)"
    folder.mkdir(parents=True)
    (folder / "Harry Potter and the Sorcerer's Stone - CHAPTER ONE.txt").write_text(
        "CHAPTER ONE\nThe whole novel, through THE END.\n"
    )
    (folder / "metadata.opf").write_text("<package/>")

    _ingest(_location(root, SourceLocation.TransferMode.COPY))

    assert not ImportGroup.objects.exists()
    job = ImportJob.objects.get()
    assert job.import_group_id is None
    assert ReviewTask.objects.get().task_type == ReviewTask.TaskType.NEEDS_ATTENTION


@pytest.mark.django_db(transaction=True)
def test_detection_can_be_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Unrelated Title", authors=["Someone Else"])
    root = tmp_path / "share"
    _split_book_folder(root)

    with override_settings(BOOK_SCANNER_MULTIPART_DETECTION=False):
        _ingest(_location(root, SourceLocation.TransferMode.COPY))

    assert not ImportGroup.objects.exists()
    assert ReviewTask.objects.filter(task_type=ReviewTask.TaskType.NEEDS_ATTENTION).count() == 4


@pytest.mark.django_db(transaction=True)
def test_preview_reports_multipart_membership(tmp_path: Path) -> None:
    root = tmp_path / "share"
    _split_book_folder(root)
    location = _location(root, SourceLocation.TransferMode.COPY)
    stdout = StringIO()

    call_command(
        "ingest_sources", "--source-id", str(location.id), "--preview", "--json", stdout=stdout
    )

    rows = {Path(row["path"]).name: row for row in json.loads(stdout.getvalue())}
    assert rows["ch02.txt"]["part_label"] == "Chapter 2"
    assert rows["AppA.txt"]["part_label"] == "Appendix A"
    assert len({row["multipart_group"] for row in rows.values()}) == 1
    assert not ImportJob.objects.exists()


# --- review findings on PR #101 -----------------------------------------------------


def _process_all(jobs: list[ImportJob]) -> None:
    for job in jobs:
        intake.process_import_job(str(job.id))


@pytest.mark.django_db(transaction=True)
def test_byte_budget_partial_discovery_waits_for_every_detected_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _folder, parts = _split_book_folder(root)
    location = _location(root, SourceLocation.TransferMode.COPY)
    sizes = [path.stat().st_size for path in parts]

    # The budget fits exactly one file: one job for a detected four-part work.
    budget = intake.ByteBudget(remaining=max(sizes))
    first = discover_source_jobs_for_location(location, byte_budget=budget)
    assert len(first) == 1 and budget.exhausted
    _process_all(first)

    group = ImportGroup.objects.get()
    assert len(group.detection["parts"]) == 4
    assert all("source_path" in part for part in group.detection["parts"])
    # Three detected files have no job yet, so nothing may be reviewed or confirmed.
    assert not ReviewTask.objects.exists()

    rest = discover_source_jobs_for_location(location)
    assert len(rest) == 3
    assert {job.import_group_id for job in rest} == {group.id}
    _process_all(rest)

    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    assert task.embedded_metadata["expected_part_count"] == 4
    assert task.embedded_metadata["missing_parts"] == []
    response = _admin_client().post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps({"title": "Learning Widgets", "authors": ["Ada Example"]}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    assert Book.objects.get().file_variants.filter(part_index__isnull=False).count() == 4


@pytest.mark.django_db(transaction=True)
def test_detected_part_not_imported_yet_blocks_confirmation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _split_book_folder(root)
    _ingest(_location(root, SourceLocation.TransferMode.COPY))
    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    # A job of the group vanishes from the database (e.g. a pruned job): its file is
    # still expected, so the group is no longer complete.
    ImportJob.objects.filter(original_filename="ch03.txt").delete()

    response = _admin_client().post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps({"title": "Learning Widgets"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert b"ch03.txt" in response.content
    assert not Book.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_missing_source_failure_settles_group_and_needs_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _folder, parts = _split_book_folder(root)
    location = _location(root, SourceLocation.TransferMode.COPY)
    jobs = discover_source_jobs_for_location(location)
    by_name = {job.original_filename: job for job in jobs}
    _process_all([by_name[name] for name in ("ch01.txt", "ch02.txt", "ch03.txt")])
    assert not ReviewTask.objects.exists()

    # The last source disappears before its job runs: the job FAILS on the
    # missing-source path, which must still settle the group.
    appendix = next(path for path in parts if path.name == "AppA.txt")
    appendix.unlink()
    intake.process_import_job(str(by_name["AppA.txt"].id))
    assert ImportJob.objects.get(pk=by_name["AppA.txt"].pk).state == ImportJob.State.FAILED

    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    assert task.state == ReviewTask.State.OPEN
    assert [part["filename"] for part in task.embedded_metadata["missing_parts"]] == ["AppA.txt"]
    client = _admin_client()
    detail = client.get(f"/reviews/{task.id}/").json()["multipart"]
    assert [part["filename"] for part in detail["missing_parts"]] == ["AppA.txt"]
    assert detail["incomplete_acknowledged"] is False

    manual = {"title": "Learning Widgets", "authors": ["Ada Example"]}
    response = client.post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps(manual),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert b"acknowledge" in response.content
    assert not Book.objects.exists()

    response = client.post(f"/reviews/{task.id}/multipart/acknowledge-incomplete/")
    assert response.status_code == 200, response.content
    response = client.post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps(manual),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    book = Book.objects.get()
    assert sorted(book.file_variants.values_list("original_filename", flat=True)) == [
        "ch01.txt",
        "ch02.txt",
        "ch03.txt",
    ]
    task.refresh_from_db()
    assert task.decision_payload["multipart_confirmed"]["missing_parts"] == ["AppA.txt"]

    # The file shows up again later: an independent import, not a new one-file group.
    appendix.write_text("Learning Widgets, AppA.txt: found again.\n")
    _ingest(location)
    late = ImportJob.objects.filter(original_filename="AppA.txt").latest("created_at")
    assert late.import_group_id is None
    assert ImportGroup.objects.count() == 1
    assert ReviewTask.objects.filter(
        import_job=late, task_type=ReviewTask.TaskType.NEEDS_ATTENTION
    ).exists()


@pytest.mark.django_db(transaction=True)
def test_new_missing_part_needs_a_fresh_acknowledgement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _folder, parts = _split_book_folder(root)
    jobs = discover_source_jobs_for_location(_location(root, SourceLocation.TransferMode.COPY))
    next(path for path in parts if path.name == "AppA.txt").unlink()
    _process_all(jobs)
    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    client = _admin_client()
    assert client.post(f"/reviews/{task.id}/multipart/acknowledge-incomplete/").status_code == 200

    ImportJob.objects.filter(original_filename="ch03.txt").update(
        state=ImportJob.State.FAILED, last_error="archive lost"
    )
    response = client.post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps({"title": "Learning Widgets"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert b"ch03.txt" in response.content


def _reject_during(
    monkeypatch: pytest.MonkeyPatch, name: str, task: ReviewTask, done: list[bool]
) -> None:
    from ebook_server.core import multipart

    original = getattr(intake, name)

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if not done:
            done.append(True)
            multipart.reject_grouping(review_task=task, reviewer=None, notes="anthology")
        return original(*args, **kwargs)

    monkeypatch.setattr(intake, name, wrapped)


@pytest.mark.parametrize("window", ["extract_metadata", "_archive_awaiting_review"])
@pytest.mark.django_db(transaction=True)
def test_rejecting_while_a_new_part_is_processing_identifies_it_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, window: str
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Unrelated Title", authors=["Someone Else"])
    root = tmp_path / "share"
    folder, parts = _split_book_folder(root)
    location = _location(root, SourceLocation.TransferMode.COPY)
    _process_all(discover_source_jobs_for_location(location))
    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    assert task.import_job is not None

    # A newly added chapter joins the pending group; the reviewer rejects the grouping
    # while that part's job is running (before or after its group check).
    (folder / "ch04.txt").write_text("Learning Widgets, ch04.txt: a late chapter.\n")
    [new_job] = discover_source_jobs_for_location(location)
    assert new_job.import_group_id == task.import_job.import_group_id
    done: list[bool] = []
    _reject_during(monkeypatch, window, task, done)
    intake.process_import_job(str(new_job.id))
    assert done

    assert ImportGroup.objects.get().state == ImportGroup.State.REJECTED
    per_file = ReviewTask.objects.filter(task_type=ReviewTask.TaskType.NEEDS_ATTENTION)
    assert sorted(t.source_filename for t in per_file) == sorted(
        [path.name for path in parts] + ["ch04.txt"]
    )
    # Exactly one individual review per file, the late part included.
    assert per_file.filter(import_job=new_job).count() == 1
    new_job.refresh_from_db()
    assert new_job.state == ImportJob.State.LOW_CONFIDENCE
    assert new_job.storage_identifier


@pytest.mark.django_db(transaction=True)
def test_reorder_rejects_a_position_held_by_an_unsubmitted_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _library(tmp_path, monkeypatch)
    _stub_provider(monkeypatch, title="Learning Widgets", authors=["Ada Example"])
    root = tmp_path / "share"
    _split_book_folder(root)
    _ingest(_location(root, SourceLocation.TransferMode.COPY))
    task = ReviewTask.objects.get(task_type=ReviewTask.TaskType.MULTIPART_WORK)
    jobs = {job.original_filename: job for job in ImportJob.objects.all()}
    client = _admin_client()

    # ch02 holds position 2; moving only the appendix there would duplicate it.
    response = client.post(
        f"/reviews/{task.id}/multipart/order/",
        data=json.dumps({"parts": [{"import_job_id": str(jobs["AppA.txt"].id), "part_index": 2}]}),
        content_type="application/json",
    )
    assert response.status_code == 400
    assert ImportJob.objects.get(pk=jobs["AppA.txt"].pk).part_index == 4

    # Swapping two parts in one submission is fine.
    response = client.post(
        f"/reviews/{task.id}/multipart/order/",
        data=json.dumps(
            {
                "parts": [
                    {"import_job_id": str(jobs["AppA.txt"].id), "part_index": 2},
                    {"import_job_id": str(jobs["ch02.txt"].id), "part_index": 4},
                ]
            }
        ),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    positions = dict(ImportJob.objects.values_list("original_filename", "part_index"))
    assert positions == {"ch01.txt": 1, "AppA.txt": 2, "ch03.txt": 3, "ch02.txt": 4}
