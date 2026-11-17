"""The review queue shows formats of one book as one entry (#49)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from ebook_server.core.models import (
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    SourceLocation,
    get_or_create_profile,
)


def _client(*, html: bool) -> Client:
    profile = get_or_create_profile("queue-admin", role="admin")
    client = Client(HTTP_ACCEPT="text/html") if html else Client()
    client.force_login(profile.user)
    return client


def _task(
    location: SourceLocation | None,
    path: Path,
    *,
    fmt: str,
    scores: tuple[float, ...] = (),
) -> ReviewTask:
    job = ImportJob.objects.create(
        source_type=ImportJob.SourceType.NETWORK_SOURCE,
        state=ImportJob.State.LOW_CONFIDENCE,
        source_location=location,
        uploader=get_or_create_profile("queue-admin", role="admin").user,
        source_path=str(path),
        original_filename=path.name,
        normalized_filename=path.name,
        detected_format=fmt,
    )
    for index, score in enumerate(scores):
        MetadataCandidate.objects.create(
            import_job=job,
            provider_id="stub",
            provider_ref=f"ref-{index}",
            title="Overclocked",
            authors=["Cory Doctorow"],
            score=score,
        )
    return ReviewTask.objects.create(
        import_job=job,
        source_filename=path.name,
        source_path_hint=str(path),
        inferred_title="Overclocked",
        inferred_authors=["Cory Doctorow"],
    )


@pytest.fixture
def folder(tmp_path: Path) -> Path:
    return tmp_path / "share" / "Cory Doctorow" / "Overclocked (12)"


def _location(tmp_path: Path, *, grouped: bool) -> SourceLocation:
    return SourceLocation.objects.create(
        name=f"share-{grouped}",
        root_path=str(tmp_path / "share"),
        group_sibling_formats=grouped,
    )


@pytest.mark.django_db
def test_json_items_stay_per_task_but_are_annotated(tmp_path: Path, folder: Path) -> None:
    location = _location(tmp_path, grouped=True)
    txt = _task(location, folder / "Overclocked.txt", fmt="txt")
    epub = _task(location, folder / "Overclocked.epub", fmt="epub")
    mobi = _task(location, folder / "OVERCLOCKED.mobi", fmt="mobi", scores=(0.4,))
    alone = _task(location, folder / "Other Book.epub", fmt="epub")

    payload: dict[str, Any] = _client(html=False).get("/reviews/").json()

    assert payload["count"] == 4
    assert payload["group_count"] == 2
    by_id = {item["id"]: item for item in payload["items"]}
    group = by_id[str(txt.id)]["sibling_group"]
    # Best candidate score wins, then the richer format, then the oldest task.
    assert group["primary_task_id"] == str(mobi.id)
    assert set(group["task_ids"]) == {str(txt.id), str(epub.id), str(mobi.id)}
    assert group["size"] == 3
    assert by_id[str(epub.id)]["sibling_group"] == group
    assert by_id[str(alone.id)]["sibling_group"] is None


@pytest.mark.django_db
def test_without_scores_the_most_informative_format_is_primary(
    tmp_path: Path, folder: Path
) -> None:
    location = _location(tmp_path, grouped=True)
    _task(location, folder / "Overclocked.txt", fmt="txt")
    epub = _task(location, folder / "Overclocked.epub", fmt="epub")

    payload = _client(html=False).get("/reviews/").json()
    assert payload["items"][0]["sibling_group"]["primary_task_id"] == str(epub.id)


@pytest.mark.django_db
def test_html_queue_shows_one_entry_per_book(tmp_path: Path, folder: Path) -> None:
    location = _location(tmp_path, grouped=True)
    for fmt in ("txt", "fb2", "mobi"):
        _task(location, folder / f"Overclocked.{fmt}", fmt=fmt)
    epub = _task(location, folder / "Overclocked.epub", fmt="epub")

    page = _client(html=True).get("/reviews/?view=html").content.decode()

    assert page.count('class="review-row"') == 1
    assert f"/reviews/{epub.id}/" in page
    assert "+3 other formats of this book" in page
    for fmt in ("txt", "fb2", "mobi"):
        assert f"Overclocked.{fmt}</a>" in page
    assert "4 open" in page


@pytest.mark.django_db
def test_no_grouping_without_the_source_setting(tmp_path: Path, folder: Path) -> None:
    location = _location(tmp_path, grouped=False)
    _task(location, folder / "Overclocked.txt", fmt="txt")
    _task(location, folder / "Overclocked.epub", fmt="epub")
    _task(None, folder / "Overclocked.mobi", fmt="mobi")  # uploads never group

    payload = _client(html=False).get("/reviews/").json()
    assert payload["group_count"] == 3
    assert all(item["sibling_group"] is None for item in payload["items"])
    page = _client(html=True).get("/reviews/?view=html").content.decode()
    assert page.count('class="review-row"') == 3


@pytest.mark.django_db
def test_other_folders_are_separate_groups(tmp_path: Path, folder: Path) -> None:
    location = _location(tmp_path, grouped=True)
    _task(location, folder / "Overclocked.txt", fmt="txt")
    _task(location, folder.parent / "Overclocked.epub", fmt="epub")

    payload = _client(html=False).get("/reviews/").json()
    assert payload["group_count"] == 2
