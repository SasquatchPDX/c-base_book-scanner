from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core.models import Book, ImportJob, ReviewTask, get_or_create_profile
from ebook_server.storage import LocalFilesystemStorage


def _configure_storage(tmp_path: Path) -> None:
    from ebook_server.core.models import StorageLocation

    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={
            "backend": StorageLocation.BackendType.LOCAL,
            "base_path": "default",
        },
    )


def _force_stub_provider(
    monkeypatch: pytest.MonkeyPatch,
    candidate_title: str,
    *,
    authors: list[str] | None = None,
    isbn_13: str | None = None,
    review_score: float = 0.99,
    auto_accept_score: float = 0.9,
) -> None:
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
            del title, author, folder_hint, isbn
            payload: dict[str, Any] = {
                "title": candidate_title,
                "author_name": authors or [],
            }
            if isbn_13 is not None:
                payload["isbn_13"] = [isbn_13]
            return [payload]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )
    monkeypatch.setattr("ebook_server.core.intake.REVIEW_SCORE", review_score)
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", auto_accept_score)


def _start_low_confidence_import(
    monkeypatch: pytest.MonkeyPatch,
    client: Client,
    tmp_path: Path,
    *,
    username: str,
    candidate_title: str,
    isbn_13: str | None = None,
) -> tuple[ReviewTask, ImportJob]:
    profile = get_or_create_profile(username)
    client.force_login(profile.user)

    _configure_storage(tmp_path)
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root / "library-artifacts")),
    )
    _force_stub_provider(monkeypatch, candidate_title=candidate_title, isbn_13=isbn_13)

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "mismatch.pdf",
                b"%PDF-1.4\n%abc\n",
                content_type="application/pdf",
            ),
        },
    )
    assert response.status_code == 201

    job = ImportJob.objects.get(pk=response.json()["job_id"])
    task = ReviewTask.objects.get(import_job=job)
    return task, job


def _manual_entry_payload() -> dict[str, Any]:
    return {
        "title": "Manual Title",
        "authors": ["Jane Manual"],
        "publisher": "Manual Press",
        "language": "en",
        "description": "Manual metadata entry",
    }


@pytest.mark.django_db(transaction=True)
def test_low_confidence_import_creates_open_review_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client()
    task, job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=client,
        tmp_path=tmp_path,
        username="alice-review-queue",
        candidate_title="Not A Match",
    )

    job.refresh_from_db()
    task.refresh_from_db()

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert task.state == ReviewTask.State.OPEN
    assert task.import_job_id == job.id
    assert task.import_job.metadata_candidates.count() == 1

    response = client.get("/reviews/")
    assert response.status_code == 200
    payload = response.json()
    assert payload["count"] == 1
    assert payload["items"][0]["id"] == str(task.id)


@pytest.mark.django_db(transaction=True)
def test_permission_model_for_review_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_client = Client()
    other_client = Client()
    admin_client = Client()

    task, _job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=owner_client,
        tmp_path=tmp_path,
        username="uploader-owner",
        candidate_title="Not A Match",
    )

    owner_profile = task.import_job.uploader.library_profile
    other_profile = get_or_create_profile("other-member")
    admin_profile = get_or_create_profile("admin-member", role="admin")

    other_client.force_login(other_profile.user)
    admin_client.force_login(admin_profile.user)

    assert owner_profile.user != other_profile.user
    assert owner_profile.user != admin_profile.user

    response_other = other_client.post(
        f"/reviews/{task.id}/unidentified/",
        data=b'{"notes":"should fail"}',
        content_type="application/json",
    )
    assert response_other.status_code == 403

    response_admin = admin_client.post(
        f"/reviews/{task.id}/unidentified/",
        data=b'{"notes":"ok"}',
        content_type="application/json",
    )
    assert response_admin.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_accept_candidate_resolves_import_to_existing_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ebook_server.core.models import Identifier

    existing_book = Book.objects.create(title="Known Book")
    Identifier.objects.create(
        book=existing_book,
        kind=Identifier.IdentifierType.ISBN13,
        value="9780306406157",
        normalized_value="9780306406157",
    )

    client = Client()
    task, _job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=client,
        tmp_path=tmp_path,
        username="uploader-accept",
        candidate_title="Known Book",
        isbn_13="9780306406157",
    )

    candidate_id = task.import_job.metadata_candidates.first()
    assert candidate_id is not None
    selected_candidate_id = candidate_id.id

    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        data=f"{{\"candidate_id\": {selected_candidate_id}}}".encode(),
        content_type="application/json",
    )
    assert response.status_code == 200

    task.refresh_from_db()
    import_job = task.import_job
    assert import_job.state == ImportJob.State.COMPLETE
    assert import_job.book_id == existing_book.id
    assert task.decision == ReviewTask.Decision.ACCEPT_CANDIDATE


@pytest.mark.django_db(transaction=True)
def test_manual_entry_accept_without_isbn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    client = Client()
    task, _job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=client,
        tmp_path=tmp_path,
        username="uploader-manual",
        candidate_title="Not A Match",
    )

    response = client.post(
        f"/reviews/{task.id}/manual-entry/",
        data=json.dumps(_manual_entry_payload()),
        content_type="application/json",
    )
    assert response.status_code == 200

    import_job = task.import_job
    import_job.refresh_from_db()
    assert import_job.state == ImportJob.State.COMPLETE
    assert import_job.book is not None
    assert import_job.book.title == "Manual Title"


@pytest.mark.django_db(transaction=True)
def test_merge_resolves_task_into_existing_book(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing_book = Book.objects.create(title="Library Canonical")

    client = Client()
    task, _job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=client,
        tmp_path=tmp_path,
        username="uploader-merge",
        candidate_title="Yet Another Title",
    )

    response = client.post(
        f"/reviews/{task.id}/merge/",
        data=json.dumps({"book_id": existing_book.id}),
        content_type="application/json",
    )
    assert response.status_code == 200

    import_job = task.import_job
    import_job.refresh_from_db()
    assert import_job.state == ImportJob.State.COMPLETE
    assert import_job.book_id == existing_book.id


@pytest.mark.django_db(transaction=True)
def test_mark_unidentified_records_decision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = Client()
    task, _job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=client,
        tmp_path=tmp_path,
        username="uploader-unid",
        candidate_title="Not A Match",
    )

    response = client.post(
        f"/reviews/{task.id}/unidentified/",
        data=b'{"notes":"no metadata found"}',
        content_type="application/json",
    )
    assert response.status_code == 200

    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.MARK_UNIDENTIFIED
