from __future__ import annotations

from typing import Any

import pytest
from django.db import IntegrityError, transaction
from django.test import Client

from ebook_server.core import identification
from ebook_server.core.models import (
    Book,
    MetadataCandidate,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    get_or_create_profile,
)


class _ScanMetadataProvider:
    provider_id = "scan-stub"

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        del title, author, isbn, folder_hint
        return [
            {
                "key": "/works/OL-SCAN",
                "title": "The Physical Book",
                "author_name": ["Ada Scanner"],
                "first_publish_year": 2024,
            }
        ]


def _scan_client(username: str = "scan-metadata-owner") -> tuple[Client, ScanJob]:
    profile = get_or_create_profile(username)
    client = Client()
    client.force_login(profile.user)
    response = client.post(
        "/scan/jobs/",
        {
            "provisional_title": "The Physical Book",
            "provisional_authors": "Ada Scanner",
            "expected_page_count": "42",
            "scanner_id": "scanner-metadata-1",
        },
    )
    assert response.status_code == 201
    return client, ScanJob.objects.get(pk=response.json()["id"])


@pytest.mark.django_db(transaction=True)
def test_scan_identification_creates_shared_review_and_accept_links_book(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: [_ScanMetadataProvider()])
    client, scan_job = _scan_client()

    response = client.post(f"/scan/jobs/{scan_job.id}/identify/")

    assert response.status_code == 202
    payload = response.json()
    assert payload["state"] == ProcessingJob.JobState.SUCCESS
    assert payload["candidate_count"] == 1
    task = ReviewTask.objects.get(pk=payload["review_task_id"])
    candidate = MetadataCandidate.objects.get(scan_job=scan_job)
    assert task.scan_job_id == scan_job.id
    assert task.import_job_id is None
    assert task.inferred_authors == ["Ada Scanner"]

    queue_response = client.get("/reviews/")
    assert queue_response.status_code == 200
    assert queue_response.json()["items"][0]["source_type"] == "scan"

    resolution = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": candidate.id},
    )
    assert resolution.status_code == 200
    assert resolution.json()["file_variant_id"] is None

    scan_job.refresh_from_db()
    task.refresh_from_db()
    assert scan_job.book is not None
    assert scan_job.book.title == "The Physical Book"
    assert task.state == ReviewTask.State.RESOLVED
    assert task.resolved_book_id == scan_job.book_id


@pytest.mark.django_db(transaction=True)
def test_finishing_unlinked_scan_routes_to_identification_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: [_ScanMetadataProvider()])
    client, scan_job = _scan_client("scan-finish-identification")

    response = client.post(f"/scan/jobs/{scan_job.id}/finish/")

    assert response.status_code == 202
    payload = response.json()
    assert payload["requires_bibliographic_review"] is True
    assert payload["identification_job"]["state"] == ProcessingJob.JobState.SUCCESS
    assert ReviewTask.objects.filter(scan_job=scan_job, state=ReviewTask.State.OPEN).exists()
    scan_job.refresh_from_db()
    assert scan_job.book_id is None
    assert scan_job.state == ScanJob.State.OPEN


@pytest.mark.django_db(transaction=True)
def test_scan_review_is_limited_to_operator_or_admin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: [_ScanMetadataProvider()])
    owner_client, scan_job = _scan_client("scan-review-owner")
    response = owner_client.post(f"/scan/jobs/{scan_job.id}/identify/")
    task_id = response.json()["review_task_id"]

    other = get_or_create_profile("scan-review-other")
    other_client = Client()
    other_client.force_login(other.user)
    assert other_client.get(f"/reviews/{task_id}/").status_code == 403
    assert other_client.get("/reviews/").json()["count"] == 0

    admin = get_or_create_profile("scan-review-admin", role="admin")
    admin_client = Client()
    admin_client.force_login(admin.user)
    assert admin_client.get(f"/reviews/{task_id}/").status_code == 200


@pytest.mark.django_db(transaction=True)
def test_scan_review_manual_entry_links_scan_without_creating_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: [])
    client, scan_job = _scan_client("scan-manual-owner")
    response = client.post(f"/scan/jobs/{scan_job.id}/identify/")
    task_id = response.json()["review_task_id"]

    resolution = client.post(
        f"/reviews/{task_id}/manual-entry/",
        {"title": "Manually Identified Scan", "authors": "Grace Reviewer"},
    )

    assert resolution.status_code == 200
    assert resolution.json()["variant_id"] is None
    scan_job.refresh_from_db()
    assert scan_job.book is not None
    assert scan_job.book.title == "Manually Identified Scan"
    assert Book.objects.get(pk=scan_job.book_id).authors.get().full_name == "Grace Reviewer"


@pytest.mark.django_db(transaction=True)
def test_candidate_requires_exactly_one_workflow_source() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        MetadataCandidate.objects.create(
            provider_id="invalid",
            title="No source",
        )


@pytest.mark.django_db(transaction=True)
def test_search_again_refreshes_scan_candidates_and_hints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: [_ScanMetadataProvider()])
    client, scan_job = _scan_client("scan-search-again-owner")
    first = client.post(f"/scan/jobs/{scan_job.id}/identify/")
    task = ReviewTask.objects.get(pk=first.json()["review_task_id"])
    old_candidate_id = MetadataCandidate.objects.get(scan_job=scan_job).id

    edit = client.post(
        f"/reviews/{task.id}/edit-search-terms/",
        {"inferred_title": "Refined Scan", "inferred_authors": "Rita Refiner"},
    )
    assert edit.status_code == 200

    class RefinedProvider:
        provider_id = "refined-scan-stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, Any]]:
            del isbn, folder_hint
            assert title == "Refined Scan"
            assert author == "Rita Refiner"
            return [
                {
                    "key": "/works/OL-REFINED",
                    "title": "Refined Result",
                    "author_name": ["Rita Refiner"],
                }
            ]

    monkeypatch.setattr(identification, "_provider_instances", lambda: [RefinedProvider()])
    response = client.post(f"/reviews/{task.id}/search-again/")

    assert response.status_code == 202
    assert response.json()["identification_job"]["state"] == ProcessingJob.JobState.SUCCESS
    scan_job.refresh_from_db()
    task.refresh_from_db()
    candidate = MetadataCandidate.objects.get(scan_job=scan_job)
    assert candidate.id != old_candidate_id
    assert candidate.title == "Refined Result"
    assert scan_job.provisional_title == "Refined Scan"
    assert scan_job.provisional_authors == ["Rita Refiner"]
    assert task.state == ReviewTask.State.OPEN
    assert task.decision == ReviewTask.Decision.SEARCH_AGAIN
