"""Formats of one book: same source folder, same file name, different extension (#39)."""

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
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

NEUROMANCER_ISBN = "9780441569595"
OTHER_ISBN = "9780575074347"


class _Provider:
    """Stub external lookup: one configurable candidate; counts lookups."""

    def __init__(self) -> None:
        self.score = 0.95
        self.title = "Neuromancer"
        self.isbn13: str | None = None
        self.calls = 0

    def __call__(self, signals: Any) -> list[IdentificationCandidate]:
        del signals
        self.calls += 1
        return [
            IdentificationCandidate(
                provider_id="stub",
                provider_ref=f"/works/{self.title}",
                title=self.title,
                authors=["William Gibson"],
                isbn10=None,
                isbn13=self.isbn13,
                language="eng",
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
    monkeypatch.setattr("ebook_server.core.identification.resolve_candidates", provider)
    root = tmp_path / "share"
    location = SourceLocation.objects.create(
        name="member",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        group_sibling_formats=True,
    )
    return {"root": root, "location": location, "provider": provider}


def _job(
    env: dict[str, Any],
    relative: str,
    text: str = "The sky above the port.",
    *,
    title: str = "Neuromancer",
    isbn: str | None = None,
) -> ImportJob:
    path = env["root"] / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix in {".htm", ".html"}:
        meta = f'<meta name="isbn" content="{isbn}">' if isbn else ""
        path.write_text(
            f"<html><head><title>{title}</title>{meta}</head><body>{text}</body></html>"
        )
    else:
        path.write_text(text)
    return intake.create_source_import_job(path, env["location"])


def _run(job: ImportJob) -> ImportJob:
    intake.process_import_job(str(job.id))
    return ImportJob.objects.select_related("book").get(pk=job.pk)


def _evidence(job: ImportJob) -> dict[str, Any]:
    value = job.evidence[intake.SIBLING_EVIDENCE_KEY]
    assert isinstance(value, dict)
    return value


@pytest.mark.django_db(transaction=True)
def test_grouping_is_opt_in() -> None:
    assert SourceLocation(name="x", root_path="/x").group_sibling_formats is False


@pytest.mark.django_db(transaction=True)
def test_later_format_joins_sibling_book_after_its_own_lookup(env: dict[str, Any]) -> None:
    first = _run(_job(env, "Gibson/Neuromancer.txt"))
    assert first.state == ImportJob.State.COMPLETE
    env["provider"].score = 0.1  # the second format alone would not be identified
    second = _run(_job(env, "Gibson/Neuromancer.htm"))

    assert env["provider"].calls == 2  # external verification still runs for every file
    assert second.state == ImportJob.State.COMPLETE
    assert second.book_id == first.book_id
    evidence = _evidence(second)
    assert evidence["import_job"] == str(first.id)
    assert evidence["accepted"] is True
    assert evidence["score"] >= intake.AUTO_ACCEPT_SCORE
    assert second.metadata_candidates.count() == 1  # provider candidates are kept
    assert set(FileVariant.objects.filter(book=first.book).values_list("format", flat=True)) == {
        "txt",
        "html",
    }
    assert not ReviewTask.objects.filter(import_job=second).exists()


@pytest.mark.django_db(transaction=True)
def test_conflicting_embedded_isbn_goes_to_review(env: dict[str, Any]) -> None:
    env["provider"].isbn13 = NEUROMANCER_ISBN
    first = _run(_job(env, "Gibson/Neuromancer.txt"))
    assert first.book is not None
    assert first.book.identifiers.filter(value=NEUROMANCER_ISBN).exists()

    env["provider"].score = 0.1
    second = _run(_job(env, "Gibson/Neuromancer.htm", isbn=OTHER_ISBN))

    assert second.state == ImportJob.State.LOW_CONFIDENCE
    assert second.book_id is None
    assert _evidence(second)["reason"] == "conflicting_isbn"
    task = ReviewTask.objects.get(import_job=second)
    assert task.state == ReviewTask.State.OPEN
    assert task.embedded_metadata[intake.SIBLING_EVIDENCE_KEY]["book_id"] == first.book_id


@pytest.mark.django_db(transaction=True)
def test_matching_embedded_isbn_joins(env: dict[str, Any]) -> None:
    env["provider"].isbn13 = NEUROMANCER_ISBN
    first = _run(_job(env, "Gibson/Neuromancer.txt"))
    env["provider"].score = 0.1
    second = _run(_job(env, "Gibson/Neuromancer.htm", isbn=NEUROMANCER_ISBN))
    assert second.book_id == first.book_id


@pytest.mark.django_db(transaction=True)
def test_confident_provider_match_for_another_book_goes_to_review(env: dict[str, Any]) -> None:
    first = _run(_job(env, "Gibson/Neuromancer.txt"))
    env["provider"].title = "Count Zero"
    second = _run(_job(env, "Gibson/Neuromancer.htm"))

    assert second.state == ImportJob.State.LOW_CONFIDENCE
    assert _evidence(second)["reason"] == "provider_match_differs"
    assert Book.objects.count() == 1
    assert first.book_id is not None


@pytest.mark.django_db(transaction=True)
def test_weak_resemblance_to_sibling_book_goes_to_review(env: dict[str, Any]) -> None:
    _run(_job(env, "Gibson/nm.txt"))  # provider stub names it "Neuromancer"
    env["provider"].score = 0.1
    second = _run(_job(env, "Gibson/nm.htm", title="nm"))

    assert second.state == ImportJob.State.LOW_CONFIDENCE
    assert _evidence(second)["reason"] == "below_threshold"


@pytest.mark.django_db(transaction=True)
def test_open_waiting_sibling_is_linked_when_another_format_is_matched(
    env: dict[str, Any],
) -> None:
    env["provider"].score = 0.5
    waiting = _run(_job(env, "Gibson/Neuromancer.htm"))
    assert waiting.state == ImportJob.State.LOW_CONFIDENCE
    env["provider"].score = 0.95
    matched = _run(_job(env, "Gibson/Neuromancer.txt"))

    waiting.refresh_from_db()
    assert waiting.state == ImportJob.State.COMPLETE
    assert waiting.book_id == matched.book_id
    task = ReviewTask.objects.get(import_job=waiting)
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.MERGED_BOOK
    assert task.reviewer is None
    assert task.decision_payload["rule"] == "group_sibling_formats"
    assert task.decision_payload[intake.SIBLING_REVIEW_PAYLOAD_KEY]["accepted"] is True
    assert Book.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_claimed_review_task_is_never_resolved_automatically(env: dict[str, Any]) -> None:
    env["provider"].score = 0.5
    waiting = _run(_job(env, "Gibson/Neuromancer.htm"))
    task = ReviewTask.objects.get(import_job=waiting)
    task.state = ReviewTask.State.CLAIMED
    task.claimed_by = get_or_create_profile("reviewer").user
    task.save(update_fields=["state", "claimed_by", "updated_at"])

    env["provider"].score = 0.95
    matched = _run(_job(env, "Gibson/Neuromancer.txt"))

    waiting.refresh_from_db()
    task.refresh_from_db()
    assert waiting.state == ImportJob.State.LOW_CONFIDENCE
    assert task.state == ReviewTask.State.CLAIMED
    note = task.decision_payload[intake.SIBLING_REVIEW_PAYLOAD_KEY]
    assert note == {
        "import_job": str(matched.id),
        "filename": "Neuromancer.txt",
        "book_id": matched.book_id,
        "accepted": False,
        "reason": "claimed_by_reviewer",
    }


@pytest.mark.django_db(transaction=True)
def test_review_decision_is_offered_to_waiting_siblings(env: dict[str, Any]) -> None:
    env["provider"].score = 0.5
    txt = _run(_job(env, "Gibson/Neuromancer.txt"))
    htm = _run(_job(env, "Gibson/Neuromancer.htm"))
    assert txt.state == htm.state == ImportJob.State.LOW_CONFIDENCE
    assert intake.SIBLING_EVIDENCE_KEY not in htm.evidence  # nothing resolved yet

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
def test_sibling_linked_while_this_file_was_archived_for_review(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    txt = _job(env, "Gibson/Neuromancer.txt")
    htm = _job(env, "Gibson/Neuromancer.htm")
    original_archive = intake._archive_source
    state = {"nested": False}

    def archive_and_race(storage: Any, job: ImportJob) -> tuple[str, bool]:
        result = original_archive(storage, job)
        if job.pk == htm.pk and not state["nested"]:
            state["nested"] = True
            env["provider"].score = 0.95
            intake.process_import_job(str(txt.id))  # the sibling completes meanwhile
        return result

    monkeypatch.setattr(intake, "_archive_source", archive_and_race)
    env["provider"].score = 0.5
    waiting = _run(htm)

    assert waiting.state == ImportJob.State.COMPLETE
    assert waiting.book_id == ImportJob.objects.get(pk=txt.pk).book_id
    assert _evidence(waiting)["accepted"] is True


@pytest.mark.django_db(transaction=True)
def test_sibling_linked_while_this_file_was_archived_for_auto_accept(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    txt = _job(env, "Gibson/Neuromancer.txt")
    htm = _job(env, "Gibson/Neuromancer.htm")
    original_archive = intake._archive_source
    state = {"nested": False}

    def archive_and_race(storage: Any, job: ImportJob) -> tuple[str, bool]:
        result = original_archive(storage, job)
        if job.pk == htm.pk and not state["nested"]:
            state["nested"] = True
            intake.process_import_job(str(txt.id))
        return result

    monkeypatch.setattr(intake, "_archive_source", archive_and_race)
    linked = _run(htm)

    assert linked.state == ImportJob.State.COMPLETE
    assert Book.objects.count() == 1
    assert _evidence(linked)["import_job"] == str(txt.id)


@pytest.mark.django_db(transaction=True)
def test_only_same_folder_and_same_name_are_siblings(env: dict[str, Any]) -> None:
    txt = _run(_job(env, "Gibson/Neuromancer.txt"))
    env["provider"].score = 0.1
    other_name = _run(_job(env, "Gibson/Count Zero.htm", title="Count Zero"))
    other_folder = _run(_job(env, "Other/Neuromancer.htm"))
    same_name_other_case = _run(_job(env, "Gibson/NEUROMANCER.htm"))

    assert other_name.state == ImportJob.State.LOW_CONFIDENCE
    assert other_folder.state == ImportJob.State.LOW_CONFIDENCE
    assert intake.SIBLING_EVIDENCE_KEY not in other_folder.evidence
    assert same_name_other_case.book_id == txt.book_id


@pytest.mark.django_db(transaction=True)
def test_sources_without_grouping_identify_each_format_alone(env: dict[str, Any]) -> None:
    location: SourceLocation = env["location"]
    location.group_sibling_formats = False
    location.save(update_fields=["group_sibling_formats"])
    _run(_job(env, "Gibson/Neuromancer.txt"))
    env["provider"].score = 0.1
    htm = _run(_job(env, "Gibson/Neuromancer.htm"))

    assert htm.state == ImportJob.State.LOW_CONFIDENCE
    assert intake.SIBLING_EVIDENCE_KEY not in htm.evidence


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
