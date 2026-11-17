"""Review-queue filtering/pagination and batch accept of unambiguous top candidates."""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

import pytest
from django.contrib.auth.models import User
from django.test import Client

from ebook_server.core import review_batch
from ebook_server.core.models import (
    AppUserProfile,
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    SourceLocation,
    StorageLocation,
    get_or_create_profile,
)


def _client(profile: AppUserProfile, *, html: bool = False, csrf: bool = False) -> Client:
    kwargs: dict[str, Any] = {"enforce_csrf_checks": csrf}
    if html:
        kwargs["HTTP_ACCEPT"] = "text/html"
    client = Client(**kwargs)
    client.force_login(profile.user)
    return client


def _make_task(
    owner: User,
    name: str,
    scores: list[float],
    *,
    source_location: SourceLocation | None = None,
    state: str = ReviewTask.State.OPEN,
) -> ReviewTask:
    """An archived low-confidence import with ``scores`` as its candidates."""
    storage_location, _ = StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    job = ImportJob.objects.create(
        source_type=(
            ImportJob.SourceType.NETWORK_SOURCE
            if source_location is not None
            else ImportJob.SourceType.UPLOAD
        ),
        state=ImportJob.State.LOW_CONFIDENCE,
        uploader=owner,
        source_location=source_location,
        source_path=f"/staging/{name}.epub",
        original_filename=f"{name}.epub",
        normalized_filename=f"{name}.epub",
        detected_format="epub",
        mime_type="application/epub+zip",
        sha256=hashlib.sha256(name.encode()).hexdigest(),
        storage_location=storage_location,
        storage_identifier=f"books/{name}.epub",
    )
    for index, score in enumerate(scores):
        MetadataCandidate.objects.create(
            import_job=job,
            provider_id="stub",
            provider_ref=f"{name}-{index}",
            title=f"{name.title()} Candidate {index}",
            authors=[f"Author {name}"],
            score=Decimal(str(score)),
        )
    return ReviewTask.objects.create(
        import_job=job,
        source_filename=f"{name}.epub",
        uploader=owner,
        inferred_title=f"{name} inferred",
        state=state,
    )


# --- eligibility rules -------------------------------------------------------------


def test_eligibility_requires_minimum_score_and_clear_lead() -> None:
    evaluate = review_batch.evaluate_top_candidates
    assert evaluate([], min_score=0.75, ambiguity_margin=0.05).reason == "no_candidates"
    assert evaluate([0.74], min_score=0.75, ambiguity_margin=0.05).reason == "below_min_score"
    assert evaluate([0.75], min_score=0.75, ambiguity_margin=0.05).eligible
    # Exact tie and near tie are ambiguous; the boundary (== margin) counts as near.
    assert evaluate([0.9, 0.9], min_score=0.75, ambiguity_margin=0.05).reason == "ambiguous"
    assert evaluate([0.9, 0.85], min_score=0.75, ambiguity_margin=0.05).reason == "ambiguous"
    assert evaluate([0.9, 0.84], min_score=0.75, ambiguity_margin=0.05).eligible
    # Order of input does not matter; Decimal scores (as stored) are accepted.
    result = evaluate([Decimal("0.40"), Decimal("0.95")], min_score=0.75, ambiguity_margin=0.05)
    assert result.eligible
    assert result.top_score == pytest.approx(0.95)
    assert result.runner_up_score == pytest.approx(0.40)
    # A zero margin still treats an exact tie as ambiguous.
    assert evaluate([0.8, 0.8], min_score=0.75, ambiguity_margin=0.0).reason == "ambiguous"


def test_request_can_only_make_batch_threshold_stricter(settings: Any) -> None:
    settings.BOOK_SCANNER_REVIEW_BATCH_MIN_SCORE = 0.8
    assert review_batch.effective_min_score(None) == pytest.approx(0.8)
    assert review_batch.effective_min_score(0.5) == pytest.approx(0.8)
    assert review_batch.effective_min_score(0.9) == pytest.approx(0.9)


# --- queue filtering / sorting / pagination ------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_queue_filters_by_source_state_confidence_and_candidates() -> None:
    admin = get_or_create_profile("queue-admin", role="admin")
    owner = get_or_create_profile("queue-owner")
    nas = SourceLocation.objects.create(name="nas", root_path="/mnt/nas")
    high = _make_task(owner.user, "high", [0.92], source_location=nas)
    low = _make_task(owner.user, "low", [0.41])
    empty = _make_task(owner.user, "empty", [], state=ReviewTask.State.CLAIMED)
    _make_task(owner.user, "done", [0.99], state=ReviewTask.State.RESOLVED)
    client = _client(admin)

    def ids(query: str) -> list[str]:
        response = client.get(f"/reviews/?{query}")
        assert response.status_code == 200
        return [item["id"] for item in response.json()["items"]]

    assert set(ids("")) == {str(high.id), str(low.id), str(empty.id)}
    assert ids(f"source={nas.id}") == [str(high.id)]
    assert ids("source=upload&has_candidates=yes") == [str(low.id)]
    assert ids("state=claimed") == [str(empty.id)]
    assert ids("has_candidates=no") == [str(empty.id)]
    assert ids("min_score=0.5") == [str(high.id)]
    assert ids("max_score=0.5") == [str(low.id)]
    assert ids("min_score=0.4&max_score=0.95&sort=confidence") == [str(low.id), str(high.id)]
    # Tasks without candidates sort last in both confidence directions.
    assert ids("sort=-confidence") == [str(high.id), str(low.id), str(empty.id)]
    assert ids("sort=confidence") == [str(low.id), str(high.id), str(empty.id)]
    # Invalid filter values are ignored rather than erroring.
    assert len(ids("min_score=abc&state=bogus&source=../etc&sort=nope")) == 3
    item = client.get(f"/reviews/?source={nas.id}").json()["items"][0]
    assert item["source_location_id"] == nas.id
    assert item["top_score"] == pytest.approx(0.92)
    assert item["batch_accept_eligibility"] == "eligible"


@pytest.mark.django_db(transaction=True)
def test_queue_paginates_and_keeps_filters_in_page_links() -> None:
    owner = get_or_create_profile("paging-owner")
    for index in range(5):
        _make_task(owner.user, f"page{index}", [0.5])
    client = _client(owner)

    first = client.get("/reviews/?page_size=2&sort=created").json()
    last = client.get("/reviews/?page_size=2&sort=created&page=3").json()
    beyond = client.get("/reviews/?page_size=2&sort=created&page=99").json()
    assert first["count"] == 5
    assert first["num_pages"] == 3
    assert len(first["items"]) == 2
    assert len(last["items"]) == 1
    assert beyond["page"] == 3

    html = _client(owner, html=True).get(
        "/reviews/?view=html&page_size=2&has_candidates=yes&min_score=0.25"
    )
    assert html.status_code == 200
    content = html.content.decode()
    assert "Page 1 of 3" in content
    assert "has_candidates=yes" in content
    assert "min_score=0.25" in content


@pytest.mark.django_db(transaction=True)
def test_queue_page_size_setting_and_cap(settings: Any) -> None:
    settings.BOOK_SCANNER_REVIEW_QUEUE_PAGE_SIZE = 3
    owner = get_or_create_profile("page-size-owner")
    for index in range(4):
        _make_task(owner.user, f"size{index}", [0.5])
    client = _client(owner)
    assert client.get("/reviews/").json()["page_size"] == 3
    assert client.get("/reviews/?page_size=100000").json()["page_size"] == (
        review_batch.MAX_QUEUE_PAGE_SIZE
    )


@pytest.mark.django_db(transaction=True)
def test_normal_user_queue_only_shows_own_tasks() -> None:
    owner = get_or_create_profile("own-queue-owner")
    other = get_or_create_profile("own-queue-other")
    mine = _make_task(owner.user, "mine", [0.9])
    _make_task(other.user, "theirs", [0.9])
    items = _client(owner).get("/reviews/").json()["items"]
    assert [item["id"] for item in items] == [str(mine.id)]


# --- batch accept --------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_batch_accept_resolves_only_unambiguous_high_confidence_tasks(settings: Any) -> None:
    settings.BOOK_SCANNER_REVIEW_BATCH_MIN_SCORE = 0.75
    settings.BOOK_SCANNER_REVIEW_BATCH_AMBIGUITY_MARGIN = 0.05
    owner = get_or_create_profile("batch-owner")
    clear = _make_task(owner.user, "clear", [0.9, 0.6])
    tied = _make_task(owner.user, "tied", [0.9, 0.88])
    weak = _make_task(owner.user, "weak", [0.6])
    none = _make_task(owner.user, "none", [])
    closed = _make_task(owner.user, "closed", [0.95], state=ReviewTask.State.RESOLVED)

    response = _client(owner).post(
        "/reviews/batch-accept/",
        data={"task_ids": [clear.id, tied.id, weak.id, none.id, closed.id, 999999]},
        content_type="application/json",
    )

    assert response.status_code == 200
    payload = response.json()
    outcomes = {item["task_id"]: item["outcome"] for item in payload["results"]}
    assert outcomes == {
        str(clear.id): "accepted",
        str(tied.id): "ambiguous",
        str(weak.id): "below_min_score",
        str(none.id): "no_candidates",
        str(closed.id): "closed",
        "999999": "not_found",
    }
    assert payload["accepted"] == 1
    assert payload["skipped"] == 5

    clear.refresh_from_db()
    assert clear.state == ReviewTask.State.RESOLVED
    assert clear.decision == ReviewTask.Decision.ACCEPT_CANDIDATE
    assert clear.reviewer_id == owner.user_id
    assert clear.selected_candidate is not None
    assert clear.selected_candidate.score == Decimal("0.90")
    assert clear.decision_payload["batch_accept"] is True
    assert clear.decision_payload["runner_up_score"] == pytest.approx(0.6)
    assert clear.decision_payload["min_score"] == pytest.approx(0.75)
    assert clear.import_job is not None
    clear.import_job.refresh_from_db()
    assert clear.import_job.state == ImportJob.State.COMPLETE
    assert clear.import_job.book_id is not None
    assert clear.import_job.file_variant_id is not None

    for task in (tied, weak, none):
        task.refresh_from_db()
        assert task.state == ReviewTask.State.OPEN
        assert task.resolved_book_id is None


@pytest.mark.django_db(transaction=True)
def test_normal_user_cannot_batch_accept_other_users_tasks() -> None:
    owner = get_or_create_profile("batch-perm-owner")
    intruder = get_or_create_profile("batch-perm-intruder")
    admin = get_or_create_profile("batch-perm-admin", role="admin")
    theirs = _make_task(owner.user, "theirs", [0.95])
    mine = _make_task(intruder.user, "mine", [0.95])

    response = _client(intruder).post(
        "/reviews/batch-accept/",
        data={"task_ids": [theirs.id, mine.id]},
        content_type="application/json",
    )
    results = {item["task_id"]: item for item in response.json()["results"]}
    assert results[str(theirs.id)]["outcome"] == "forbidden"
    # Nothing about a forbidden task is disclosed.
    assert set(results[str(theirs.id)]) == {"task_id", "outcome"}
    assert results[str(mine.id)]["outcome"] == "accepted"
    theirs.refresh_from_db()
    assert theirs.state == ReviewTask.State.OPEN

    admin_response = _client(admin).post(
        "/reviews/batch-accept/",
        data={"task_ids": [theirs.id]},
        content_type="application/json",
    )
    assert admin_response.json()["results"][0]["outcome"] == "accepted"
    theirs.refresh_from_db()
    assert theirs.state == ReviewTask.State.RESOLVED
    assert theirs.reviewer_id == admin.user_id


@pytest.mark.django_db(transaction=True)
def test_batch_accept_request_cannot_lower_threshold(settings: Any) -> None:
    settings.BOOK_SCANNER_REVIEW_BATCH_MIN_SCORE = 0.85
    owner = get_or_create_profile("batch-threshold-owner")
    task = _make_task(owner.user, "eighty", [0.8])
    client = _client(owner)

    lowered = client.post(
        "/reviews/batch-accept/",
        data={"task_ids": [task.id], "min_score": 0.5},
        content_type="application/json",
    ).json()
    assert lowered["min_score"] == pytest.approx(0.85)
    assert lowered["results"][0]["outcome"] == "below_min_score"

    settings.BOOK_SCANNER_REVIEW_BATCH_MIN_SCORE = 0.75
    stricter = client.post(
        "/reviews/batch-accept/",
        data={"task_ids": [task.id], "min_score": 0.9},
        content_type="application/json",
    ).json()
    assert stricter["results"][0]["outcome"] == "below_min_score"
    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN


@pytest.mark.django_db(transaction=True)
def test_batch_accept_validates_input_and_size(settings: Any) -> None:
    settings.BOOK_SCANNER_REVIEW_BATCH_MAX_TASKS = 2
    owner = get_or_create_profile("batch-input-owner")
    client = _client(owner)
    tasks = [_make_task(owner.user, f"size{index}", [0.95]) for index in range(3)]

    def post(task_ids: Any) -> int:
        return client.post(
            "/reviews/batch-accept/",
            data={"task_ids": task_ids},
            content_type="application/json",
        ).status_code

    assert post([]) == 400
    assert post(["abc"]) == 400
    assert post("1") == 400
    assert post([task.id for task in tasks]) == 400
    assert client.get("/reviews/batch-accept/").status_code == 405
    # Duplicate ids are processed once.
    response = client.post(
        "/reviews/batch-accept/",
        data={"task_ids": [tasks[0].id, tasks[0].id]},
        content_type="application/json",
    )
    assert [item["outcome"] for item in response.json()["results"]] == ["accepted"]


@pytest.mark.django_db(transaction=True)
def test_batch_accept_requires_login_csrf_and_enabled_profile() -> None:
    owner = get_or_create_profile("batch-csrf-owner")
    task = _make_task(owner.user, "csrf", [0.95])

    anonymous = Client().post("/reviews/batch-accept/", {"task_ids": [task.id]})
    assert anonymous.status_code == 302

    no_token = _client(owner, csrf=True).post("/reviews/batch-accept/", {"task_ids": [task.id]})
    assert no_token.status_code == 403

    owner.is_disabled = True
    owner.save(update_fields=["is_disabled"])
    disabled = _client(owner).post("/reviews/batch-accept/", {"task_ids": [task.id]})
    assert disabled.status_code == 403
    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN


@pytest.mark.django_db(transaction=True)
def test_batch_accept_html_form_round_trip() -> None:
    owner = get_or_create_profile("batch-html-owner")
    good = _make_task(owner.user, "good", [0.93])
    tied = _make_task(owner.user, "tie", [0.9, 0.9])
    client = _client(owner, html=True)

    queue = client.get("/reviews/?view=html")
    content = queue.content.decode()
    assert queue.status_code == 200
    assert 'name="task_ids"' in content
    assert 'action="/reviews/batch-accept/"' in content
    assert "csrfmiddlewaretoken" in content
    assert "Batch-eligible" in content

    result = client.post("/reviews/batch-accept/", {"task_ids": [str(good.id), str(tied.id)]})
    assert result.status_code == 200
    body = result.content.decode()
    assert "1 accepted" in body
    assert "too close to the top candidate" in body
    good.refresh_from_db()
    assert good.state == ReviewTask.State.RESOLVED
