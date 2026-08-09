from __future__ import annotations

import pytest
from django.test import Client

from ebook_server.core.models import (
    AppUserProfile,
    Author,
    Book,
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    ScanJob,
    get_or_create_profile,
)


def _html_client(username: str = "web-member") -> tuple[Client, AppUserProfile]:
    profile = get_or_create_profile(username)
    client = Client(HTTP_ACCEPT="text/html")
    client.force_login(profile.user)
    return client, profile


@pytest.mark.django_db(transaction=True)
def test_library_pages_render_as_authenticated_html() -> None:
    client, _profile = _html_client()
    author = Author.objects.create(full_name="Ada Reader")
    book = Book.objects.create(
        title="A Rendered Library",
        subtitle="Usable without an API client",
        description="A server-rendered detail page.",
    )
    book.authors.add(author)

    home = client.get("/library/?view=html")
    browse = client.get("/library/books/?view=html&q=Rendered")
    detail = client.get(f"/library/books/{book.id}/?view=html")
    upload = client.get("/imports/upload/")
    root = client.get("/")

    assert home.status_code == 200
    assert b"Books for curious people" in home.content
    assert browse.status_code == 200
    assert b"A Rendered Library" in browse.content
    assert detail.status_code == 200
    assert b"Usable without an API client" in detail.content
    assert upload.status_code == 200
    assert b"Upload a digital book" in upload.content
    assert root.status_code == 302
    assert root["Location"] == "/library/"


@pytest.mark.django_db(transaction=True)
def test_review_queue_and_resolution_form_render_and_redirect() -> None:
    client, profile = _html_client("web-reviewer")
    import_job = ImportJob.objects.create(
        source_type=ImportJob.SourceType.UPLOAD,
        state=ImportJob.State.LOW_CONFIDENCE,
        uploader=profile.user,
        source_path="/staging/example.epub",
        original_filename="example.epub",
        normalized_filename="example.epub",
        detected_format="epub",
        mime_type="application/epub+zip",
    )
    candidate = MetadataCandidate.objects.create(
        import_job=import_job,
        provider_id="test-provider",
        provider_ref="candidate-1",
        title="Candidate Book",
        authors=["Casey Candidate"],
        score=72.5,
    )
    task = ReviewTask.objects.create(
        import_job=import_job,
        source_filename="example.epub",
        uploader=profile.user,
        inferred_title="Possible Book",
        inferred_authors=["Possible Author"],
    )

    queue = client.get("/reviews/?view=html")
    detail = client.get(f"/reviews/{task.id}/?view=html")
    resolved = client.post(
        f"/reviews/{task.id}/unidentified/",
        {"notes": "Insufficient evidence"},
    )

    assert queue.status_code == 200
    assert b"Possible Book" in queue.content
    assert detail.status_code == 200
    assert candidate.title.encode() in detail.content
    assert b"Manual metadata" in detail.content
    assert b"Search providers again" in detail.content
    assert resolved.status_code == 302
    assert resolved["Location"] == "/reviews/"


@pytest.mark.django_db(transaction=True)
def test_scan_job_list_create_and_workspace_render() -> None:
    client, profile = _html_client("web-scanner")
    book = Book.objects.create(title="Physical Book")

    created = client.post(
        "/scan/jobs/",
        {
            "provisional_title": "Bench scan",
            "scanner_id": "scanner-one",
            "book_id": str(book.id),
        },
    )

    assert created.status_code == 302
    job = ScanJob.objects.get(operator=profile.user)
    assert created["Location"] == f"/scan/jobs/{job.id}/workspace/"

    jobs = client.get("/scan/jobs/?view=html")
    workspace = client.get(f"/scan/jobs/{job.id}/workspace/?view=html")

    assert jobs.status_code == 200
    assert b"Bench scan" in jobs.content
    assert workspace.status_code == 200
    assert b"ScanTailor workspace" in workspace.content
    assert b"Upload the first page" in workspace.content
