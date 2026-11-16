"""Files in one folder with the same name and different extensions are one book."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ebook_server.core import intake
from ebook_server.core.identification import IdentificationCandidate
from ebook_server.core.models import (
    Book,
    FileVariant,
    ImportJob,
    ReviewTask,
    SourceLocation,
    StorageLocation,
)
from ebook_server.storage import LocalFilesystemStorage


class _Provider:
    """Stub identification: a fixed score, counting how often a lookup happens."""

    def __init__(self) -> None:
        self.score = 0.95
        self.calls = 0

    def __call__(self, signals: Any) -> list[IdentificationCandidate]:
        del signals
        self.calls += 1
        return [
            IdentificationCandidate(
                provider_id="stub",
                provider_ref="/works/OL1W",
                title="Neuromancer",
                authors=["William Gibson"],
                isbn10=None,
                isbn13=None,
                language="en",
                description=None,
                publisher="Ace",
                edition=None,
                publication_year=1984,
                score=self.score,
                raw={},
            )
        ]


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    storage_root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root)),
    )
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    provider = _Provider()
    monkeypatch.setattr(
        "ebook_server.core.identification.resolve_candidates_with_report",
        lambda signals: (provider(signals), []),
    )
    root = tmp_path / "share"
    location = SourceLocation.objects.create(
        name="member",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
    )
    return {"root": root, "location": location, "provider": provider}


def _job(env: dict[str, Any], relative: str, text: str) -> ImportJob:
    path = env["root"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix in {".htm", ".html"}:
        path.write_text(f"<html><head><title>Neuromancer</title></head><body>{text}</body></html>")
    else:
        path.write_text(text)
    return intake.create_source_import_job(path, env["location"])


def _run(job: ImportJob) -> ImportJob:
    intake.process_import_job(str(job.id))
    return ImportJob.objects.select_related("book").get(pk=job.pk)


@pytest.mark.django_db(transaction=True)
def test_later_format_attaches_to_already_linked_sibling(env: dict[str, Any]) -> None:
    txt = _job(env, "Gibson/Neuromancer.txt", "The sky above the port.")
    htm = _job(env, "Gibson/Neuromancer.htm", "The sky above the port, as HTML.")

    first = _run(txt)
    assert first.state == ImportJob.State.COMPLETE
    env["provider"].score = 0.1  # the second format alone would not be identified
    second = _run(htm)

    assert env["provider"].calls == 1  # no external lookup for the sibling
    assert second.state == ImportJob.State.COMPLETE
    assert second.book_id == first.book_id
    assert second.evidence[intake.SIBLING_EVIDENCE_KEY]["import_job"] == str(first.id)
    assert set(FileVariant.objects.filter(book=first.book).values_list("format", flat=True)) == {
        "txt",
        "html",
    }
    assert not ReviewTask.objects.filter(import_job=second).exists()


@pytest.mark.django_db(transaction=True)
def test_waiting_sibling_is_resolved_when_another_format_is_matched(env: dict[str, Any]) -> None:
    htm = _job(env, "Gibson/Neuromancer.htm", "As HTML.")
    txt = _job(env, "Gibson/Neuromancer.txt", "As text.")

    env["provider"].score = 0.5
    waiting = _run(htm)
    assert waiting.state == ImportJob.State.LOW_CONFIDENCE
    env["provider"].score = 0.95
    matched = _run(txt)

    waiting.refresh_from_db()
    assert waiting.state == ImportJob.State.COMPLETE
    assert waiting.book_id == matched.book_id
    task = ReviewTask.objects.get(import_job=waiting)
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.MERGED_BOOK
    assert task.decision_payload["rule"] == "group_sibling_formats"
    assert Book.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_review_decision_applies_to_waiting_siblings(env: dict[str, Any]) -> None:
    env["provider"].score = 0.5
    txt = _run(_job(env, "Gibson/Neuromancer.txt", "As text."))
    htm = _run(_job(env, "Gibson/Neuromancer.htm", "As HTML."))
    assert txt.state == htm.state == ImportJob.State.LOW_CONFIDENCE

    book = Book.objects.create(title="Neuromancer")
    intake.resolve_import_with_review_data(
        job=txt,
        review_task=ReviewTask.objects.get(import_job=txt),
        book=book,
        source_type="upload",
    )

    htm.refresh_from_db()
    assert htm.state == ImportJob.State.COMPLETE
    assert htm.book_id == book.id
    assert FileVariant.objects.filter(book=book).count() == 2
    assert ReviewTask.objects.filter(state=ReviewTask.State.OPEN).count() == 0


@pytest.mark.django_db(transaction=True)
def test_only_same_folder_and_same_name_are_siblings(env: dict[str, Any]) -> None:
    txt = _run(_job(env, "Gibson/Neuromancer.txt", "As text."))
    env["provider"].score = 0.1
    other_name = _run(_job(env, "Gibson/Count Zero.htm", "Other book."))
    other_folder = _run(_job(env, "Other/Neuromancer.htm", "Same name, other folder."))
    same_name_other_case = _run(_job(env, "Gibson/NEUROMANCER.htm", "Case differs."))

    assert other_name.state == ImportJob.State.LOW_CONFIDENCE
    assert other_folder.state == ImportJob.State.LOW_CONFIDENCE
    assert same_name_other_case.book_id == txt.book_id


@pytest.mark.django_db(transaction=True)
def test_grouping_can_be_disabled_per_source(env: dict[str, Any]) -> None:
    location: SourceLocation = env["location"]
    location.group_sibling_formats = False
    location.save(update_fields=["group_sibling_formats"])
    _run(_job(env, "Gibson/Neuromancer.txt", "As text."))
    env["provider"].score = 0.1
    htm = _run(_job(env, "Gibson/Neuromancer.htm", "As HTML."))

    assert htm.state == ImportJob.State.LOW_CONFIDENCE
    assert env["provider"].calls == 2


@pytest.mark.django_db(transaction=True)
def test_uploads_have_no_siblings(env: dict[str, Any], tmp_path: Path) -> None:
    upload = tmp_path / "Neuromancer.txt"
    upload.write_text("uploaded")
    job = ImportJob.objects.create(
        source_type=ImportJob.SourceType.UPLOAD,
        state=ImportJob.State.DISCOVERED,
        uploader=intake._make_system_user(),
        source_path=str(upload),
        original_filename=upload.name,
        normalized_filename=upload.name,
    )
    assert intake._sibling_jobs(job) == []
