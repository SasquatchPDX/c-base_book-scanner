"""Contribution recognition (#27): anti-gaming rules, settlement, visibility, GDPR."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from django.conf import settings as django_settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client
from django.utils import timezone

from ebook_server.core import book_deletion, contributions
from ebook_server.core.models import (
    AppUserProfile,
    Book,
    ContributionEvent,
    ContributionKind,
    ContributionMonthlyTotal,
    ContributorPreference,
    FileVariant,
    ReviewDecisionCorrection,
    ReviewTask,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    StorageLocation,
    get_or_create_profile,
)

AFTER_HOLD = timedelta(days=15)


@pytest.fixture
def enabled(settings: Any) -> Iterator[Any]:
    settings.BOOK_SCANNER_CONTRIBUTIONS_ENABLED = True
    settings.BOOK_SCANNER_LEADERBOARD_ENABLED = False
    settings.BOOK_SCANNER_CONTRIBUTIONS_HOLD_DAYS = 14
    settings.BOOK_SCANNER_CONTRIBUTIONS_DAILY_SOFT_CAP = 50
    settings.BOOK_SCANNER_CONTRIBUTIONS_RETENTION_MONTHS = 24
    cache.clear()
    yield settings
    cache.clear()


def _member(uid: str) -> AppUserProfile:
    return get_or_create_profile(uid)


def _admin(uid: str = "admin") -> AppUserProfile:
    return get_or_create_profile(uid, role="admin")


def _client(profile: AppUserProfile) -> Client:
    client = Client()
    client.force_login(profile.user)
    return client


def _scan_review(operator: User) -> ReviewTask:
    job = ScanJob.objects.create(operator=operator, state=ScanJob.State.CAPTURING)
    return ReviewTask.objects.create(scan_job=job, source_filename=f"scan-{job.pk}")


def _merge(client: Client, task: ReviewTask, book: Book) -> None:
    response = client.post(
        f"/reviews/{task.pk}/merge/",
        data=json.dumps({"book_id": book.pk}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content


def _later() -> Any:
    return timezone.now() + AFTER_HOLD


# --- Defaults ----------------------------------------------------------------------------


def test_feature_and_leaderboard_are_off_by_default() -> None:
    from ebook_server.settings import Settings

    defaults = Settings()
    assert defaults.contributions_enabled is False
    assert defaults.leaderboard_enabled is False
    assert defaults.contributions_hold_days == 14
    assert defaults.contributions_retention_months == 24


@pytest.mark.django_db
def test_disabled_feature_records_nothing_and_hides_pages(settings: Any) -> None:
    settings.BOOK_SCANNER_CONTRIBUTIONS_ENABLED = False
    admin = _admin()
    operator = _member("op")
    task = _scan_review(operator.user)
    _merge(_client(admin), task, Book.objects.create(title="Dune"))

    assert ContributionEvent.objects.count() == 0
    assert contributions.settle().skipped
    client = _client(admin)
    for url in (
        "/me/contributions/",
        "/contributions/stats/",
        "/contributions/leaderboard/",
        "/contributions/admin/",
    ):
        assert client.get(url).status_code == 404, url


# --- Emitters and anti-gaming ------------------------------------------------------------


@pytest.mark.django_db
def test_review_resolution_records_one_pending_event_without_points(enabled: Any) -> None:
    admin = _admin()
    operator = _member("op")
    task = _scan_review(operator.user)
    _merge(_client(admin), task, Book.objects.create(title="Dune"))

    event = ContributionEvent.objects.get()
    assert event.user_id == admin.user_id
    assert event.kind == ContributionKind.REVIEW_RESOLVED
    assert event.state == ContributionEvent.State.PENDING
    assert event.points is None  # points are computed by settlement, never in the request
    assert event.own_upload is False

    task.refresh_from_db()
    assert contributions.record_review_resolution(task) == event  # retry is idempotent
    assert ContributionEvent.objects.count() == 1


@pytest.mark.django_db
def test_settlement_waits_for_hold_period_then_settles(enabled: Any) -> None:
    admin = _admin()
    task = _scan_review(_member("op").user)
    _merge(_client(admin), task, Book.objects.create(title="Dune"))

    report = contributions.settle()
    assert report.settled == 0 and report.still_pending == 1
    report = contributions.settle(now=_later())
    assert report.settled == 1
    event = ContributionEvent.objects.get()
    assert event.state == ContributionEvent.State.SETTLED
    assert event.points == Decimal("3.00")
    assert contributions.settle(now=_later()).settled == 0  # idempotent


@pytest.mark.django_db
def test_reviewing_own_upload_earns_half(enabled: Any) -> None:
    operator = _member("op")
    task = _scan_review(operator.user)
    _merge(_client(operator), task, Book.objects.create(title="Dune"))

    contributions.settle(now=_later())
    event = ContributionEvent.objects.get()
    assert event.own_upload is True
    assert event.points == Decimal("1.50")
    assert "own upload" in event.reason


@pytest.mark.django_db
def test_mark_unidentified_earns_unidentified_weight(enabled: Any) -> None:
    admin = _admin()
    task = _scan_review(_member("op").user)
    response = _client(admin).post(f"/reviews/{task.pk}/unidentified/")
    assert response.status_code == 200
    contributions.settle(now=_later())
    event = ContributionEvent.objects.get()
    assert event.kind == ContributionKind.REVIEW_UNIDENTIFIED
    assert event.points == Decimal("1.00")


@pytest.mark.django_db
def test_automatic_resolution_and_claims_earn_nothing(enabled: Any) -> None:
    operator = _member("op")
    task = _scan_review(operator.user)
    task.claimed_by = operator.user
    task.state = ReviewTask.State.CLAIMED
    task.save()
    assert contributions.record_review_resolution(task) is None  # a claim is not an outcome
    task.state = ReviewTask.State.RESOLVED
    task.decision = ReviewTask.Decision.MERGED_BOOK
    task.decided_at = timezone.now()
    task.reviewer = None  # resolved automatically (e.g. a sibling format link)
    task.save()
    assert contributions.record_review_resolution(task) is None
    assert ContributionEvent.objects.count() == 0


def _stub_identification(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from ebook_server.storage import LocalFilesystemStorage

    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(tmp_path / "storage" / "library-artifacts")),
    )

    class StubProvider:
        provider_id = "stub"

        def search(self, title: str | None = None, **_: Any) -> list[dict[str, Any]]:
            del title
            return [{"title": "Something Else Entirely", "author_name": []}]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )
    monkeypatch.setattr("ebook_server.core.intake.REVIEW_SCORE", 0.99)
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.995)


@pytest.mark.django_db(transaction=True)
def test_uploads_earn_nothing(
    enabled: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from django.core.files.uploadedfile import SimpleUploadedFile

    _stub_identification(monkeypatch, tmp_path)
    uploader = _member("uploader")
    response = _client(uploader).post(
        "/imports/upload/",
        {"file": SimpleUploadedFile("x.pdf", b"%PDF-1.4\n%abc\n", "application/pdf")},
    )
    assert response.status_code == 201
    assert ContributionEvent.objects.count() == 0
    contributions.settle(now=_later())
    assert ContributionEvent.objects.count() == 0


@pytest.mark.django_db
def test_daily_soft_cap_halves_points_after_cap(enabled: Any) -> None:
    enabled.BOOK_SCANNER_CONTRIBUTIONS_DAILY_SOFT_CAP = 5
    admin = _admin()
    client = _client(admin)
    for index in range(3):
        task = _scan_review(_member(f"op{index}").user)
        _merge(client, task, Book.objects.create(title=f"Book {index}"))

    contributions.settle(now=_later())
    points = list(
        ContributionEvent.objects.order_by("occurred_at").values_list("points", flat=True),
    )
    assert points == [Decimal("3.00"), Decimal("3.00"), Decimal("1.50")]


@pytest.mark.django_db
def test_weight_versions_do_not_change_recorded_events(enabled: Any) -> None:
    admin = _admin()
    client = _client(admin)
    first = _scan_review(_member("op1").user)
    _merge(client, first, Book.objects.create(title="Old"))
    version = contributions.save_weights(
        {ContributionKind.REVIEW_RESOLVED: contributions.WeightSpec(Decimal("10"))},
        actor=admin.user,
    )
    assert version == 1
    second = _scan_review(_member("op2").user)
    _merge(client, second, Book.objects.create(title="New"))

    contributions.settle(now=_later())
    by_task = dict(ContributionEvent.objects.values_list("review_task_id", "points"))
    assert by_task[first.pk] == Decimal("3.00")
    assert by_task[second.pk] == Decimal("10.00")


# --- Reversals: void before settlement, clawback after -----------------------------------


@pytest.mark.django_db
def test_reopened_review_voids_pending_event(enabled: Any) -> None:
    admin = _admin()
    client = _client(admin)
    task = _scan_review(_member("op").user)
    _merge(client, task, Book.objects.create(title="Dune"))

    response = client.post(
        f"/reviews/{task.pk}/edit-search-terms/",
        data=json.dumps({"inferred_title": "Dune Messiah"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    correction = ReviewDecisionCorrection.objects.get()
    assert correction.source == ReviewDecisionCorrection.Source.REOPENED

    report = contributions.settle()  # voids early, no need to wait for the hold
    assert report.voided == 1
    event = ContributionEvent.objects.get()
    assert event.state == ContributionEvent.State.VOIDED
    assert event.points is None


@pytest.mark.django_db
def test_redecided_by_someone_else_voids_first_and_credits_second(enabled: Any) -> None:
    operator = _member("op")
    admin = _admin()
    task = _scan_review(operator.user)
    _merge(_client(operator), task, Book.objects.create(title="Wrong"))
    task.refresh_from_db()
    _merge(_client(admin), task, Book.objects.create(title="Right"))

    contributions.settle(now=_later())
    states = {
        event.user_id: (event.state, event.points) for event in ContributionEvent.objects.all()
    }
    assert states[operator.user_id] == (ContributionEvent.State.VOIDED, None)
    assert states[admin.user_id] == (ContributionEvent.State.SETTLED, Decimal("3.00"))


@pytest.mark.django_db
def test_redeciding_own_review_repeatedly_counts_once(enabled: Any) -> None:
    operator = _member("op")
    client = _client(operator)
    task = _scan_review(operator.user)
    book = Book.objects.create(title="Dune")
    for _ in range(3):
        _merge(client, task, book)
        task.refresh_from_db()

    contributions.settle(now=_later())
    settled = ContributionEvent.objects.filter(state=ContributionEvent.State.SETTLED)
    assert settled.count() == 1
    assert ContributionEvent.objects.filter(state=ContributionEvent.State.VOIDED).count() == 2


@pytest.mark.django_db
def test_book_deleted_after_settlement_claws_back(enabled: Any) -> None:
    admin = _admin()
    task = _scan_review(_member("op").user)
    book = Book.objects.create(title="Dune")
    _merge(_client(admin), task, book)
    contributions.settle(now=_later())

    book_deletion.delete_book_safely(book)
    assert ReviewDecisionCorrection.objects.filter(
        source=ReviewDecisionCorrection.Source.BOOK_DELETED,
    ).exists()
    report = contributions.settle(now=_later())
    assert report.clawed_back == 1
    clawback = ContributionEvent.objects.get(kind=ContributionKind.CLAWBACK)
    assert clawback.points == Decimal("-3.00")
    total = sum(
        (points or Decimal("0"))
        for points in ContributionEvent.objects.values_list("points", flat=True)
    )
    assert total == 0  # clawback symmetry
    assert contributions.settle(now=_later()).clawed_back == 0  # never twice
    assert contributions.scorecard(admin.user)["all_time_points"] == Decimal("0.00")


@pytest.mark.django_db
def test_admin_flagging_decision_wrong_claws_back(enabled: Any) -> None:
    admin = _admin()
    task = _scan_review(_member("op").user)
    _merge(_client(admin), task, Book.objects.create(title="Dune"))
    contributions.settle(now=_later())

    response = _client(admin).post(
        "/contributions/admin/",
        data=json.dumps({"action": "flag_review", "review_task_id": task.pk, "note": "wrong"}),
        content_type="application/json",
    )
    assert response.status_code == 200, response.content
    contributions.settle(now=_later())
    clawback = ContributionEvent.objects.get(kind=ContributionKind.CLAWBACK)
    assert "Admin flagged" in clawback.reason


# --- Scans ---------------------------------------------------------------------------------


def _finished_scan(pages_by_user: dict[User, int], *, operator: User) -> ScanJob:
    location, _ = StorageLocation.objects.get_or_create(
        identifier="contrib-test",
        defaults={"base_path": "contrib"},
    )
    book = Book.objects.create(title="Scanned")
    job = ScanJob.objects.create(operator=operator, book=book, state=ScanJob.State.FINALIZING)
    sequence = 0
    for user, count in pages_by_user.items():
        for _ in range(count):
            sequence += 1
            page = ScanPage.objects.create(scan_job=job, logical_sequence_number=sequence)
            ScanPageVersion.objects.create(
                scan_page=page,
                storage_location=location,
                storage_identifier=f"p{sequence}",
                checksum_sha256="0" * 64,
                mime_type="image/png",
                size_bytes=1,
                uploader=user,
            )
    FileVariant.objects.create(
        book=book,
        format="pdf",
        mime_type="application/pdf",
        original_filename="cleaned.pdf",
        storage_location=location,
        storage_identifier="cleaned.pdf",
        size_bytes=1,
        sha256="1" * 64,
        source_type=FileVariant.SourceType.CLEANED,
        processing_state=FileVariant.ProcessingState.COMPLETE,
        converter_options={"scan_job_id": str(job.id)},
    )
    job.state = ScanJob.State.COMPLETE
    job.finished_at = timezone.now()
    job.save()
    return job


@pytest.mark.django_db
def test_scan_completion_splits_bonus_by_final_pages(enabled: Any) -> None:
    alice = _member("alice").user
    bob = _member("bob").user
    job = _finished_scan({alice: 3, bob: 1}, operator=alice)
    events = contributions.record_scan_completion(job)
    assert len(events) == 2
    assert len(contributions.record_scan_completion(job)) == 2  # idempotent
    assert ContributionEvent.objects.count() == 2

    contributions.settle(now=_later())
    points = dict(ContributionEvent.objects.values_list("user_id", "points"))
    # 5 + 0.05 * 4 pages = 5.20, split 3:1
    assert points[alice.pk] == Decimal("3.90")
    assert points[bob.pk] == Decimal("1.30")


@pytest.mark.django_db
def test_scan_points_are_capped_per_job(enabled: Any) -> None:
    alice = _member("alice").user
    contributions.save_weights(
        {
            ContributionKind.SCAN_COMPLETED: contributions.WeightSpec(
                Decimal("5"),
                per_unit_points=Decimal("10"),
                cap=Decimal("25"),
            ),
        },
        actor=None,
    )
    job = _finished_scan({alice: 4}, operator=alice)
    contributions.record_scan_completion(job)
    contributions.settle(now=_later())
    assert ContributionEvent.objects.get().points == Decimal("25.00")


@pytest.mark.django_db
def test_scan_variant_invalidated_before_settlement_voids(enabled: Any) -> None:
    alice = _member("alice").user
    job = _finished_scan({alice: 2}, operator=alice)
    contributions.record_scan_completion(job)
    FileVariant.objects.update(processing_state=FileVariant.ProcessingState.FAILED)
    contributions.settle(now=_later())
    event = ContributionEvent.objects.get()
    assert event.state == ContributionEvent.State.VOIDED
    assert "no library variant" in event.reason


# --- Visibility: scorecard, stats, leaderboard ---------------------------------------------


def _settled_points(user: User, points: str) -> None:
    now = timezone.now()
    ContributionEvent.objects.create(
        user=user,
        kind=ContributionKind.ADMIN_ADJUSTMENT,
        state=ContributionEvent.State.SETTLED,
        points=Decimal(points),
        outcome_key=f"test:{user.pk}:{points}",
        occurred_at=now,
        settle_after=now,
        settled_at=now,
    )


@pytest.mark.django_db
def test_scorecard_shows_only_own_data(enabled: Any) -> None:
    alice = _member("alice")
    bob = _member("bob")
    _settled_points(alice.user, "7")
    _settled_points(bob.user, "99")

    payload = _client(alice).get("/me/contributions/").json()
    assert payload["all_time_points"] == "7.00"
    assert all(row["points"] != "99.00" for row in payload["recent"])
    html = _client(alice).get("/me/contributions/?view=html").content.decode()
    assert "Only you can see this page" in html
    assert "99.00" not in html


@pytest.mark.django_db
def test_collective_stats_are_anonymous(enabled: Any) -> None:
    admin = _admin("secret-admin-name")
    task = _scan_review(_member("op").user)
    _merge(_client(admin), task, Book.objects.create(title="Dune"))
    viewer = _client(_member("viewer"))

    payload = viewer.get("/contributions/stats/").json()
    assert payload["reviews_this_month"] == 1
    html = viewer.get("/contributions/stats/?view=html").content.decode()
    assert "secret-admin-name" not in html
    assert "secret-admin-name" not in json.dumps(payload)


@pytest.mark.django_db
def test_leaderboard_needs_its_own_flag(enabled: Any) -> None:
    member = _member("alice")
    assert _client(member).get("/contributions/leaderboard/").status_code == 404
    enabled.BOOK_SCANNER_LEADERBOARD_ENABLED = True
    assert _client(member).get("/contributions/leaderboard/").status_code == 200
    assert Client().get("/contributions/leaderboard/").status_code == 302  # members only


@pytest.mark.django_db
def test_opted_out_members_never_appear_on_leaderboard(enabled: Any) -> None:
    enabled.BOOK_SCANNER_LEADERBOARD_ENABLED = True
    alice = _member("alice-uid")
    bob = _member("bob-uid")
    _settled_points(alice.user, "10")
    _settled_points(bob.user, "20")
    viewer = _client(_member("viewer"))

    joined = _client(alice).post(
        "/me/contributions/leaderboard/",
        data=json.dumps({"opt_in": True, "consent": True, "display_handle": "Reader A"}),
        content_type="application/json",
    )
    assert joined.status_code == 200, joined.content

    for period in ("season", "all"):
        payload = viewer.get(f"/contributions/leaderboard/?period={period}").json()
        assert [row["handle"] for row in payload["rows"]] == ["Reader A"]
        html = viewer.get(f"/contributions/leaderboard/?view=html&period={period}")
        text = html.content.decode()
        assert "Reader A" in text
        assert "bob-uid" not in text and "alice-uid" not in text
        assert "bob-uid" not in json.dumps(payload)

    left = _client(alice).post(
        "/me/contributions/leaderboard/",
        data=json.dumps({"opt_in": False}),
        content_type="application/json",
    )
    assert left.status_code == 200
    assert viewer.get("/contributions/leaderboard/").json()["rows"] == []
    assert "Reader A" not in viewer.get("/contributions/leaderboard/?view=html").content.decode()
    # Points stay on the private scorecard after leaving.
    assert _client(alice).get("/me/contributions/").json()["all_time_points"] == "10.00"


@pytest.mark.django_db
def test_admin_exclusion_hides_member(enabled: Any) -> None:
    enabled.BOOK_SCANNER_LEADERBOARD_ENABLED = True
    alice = _member("alice")
    _settled_points(alice.user, "10")
    contributions.set_leaderboard_opt_in(user=alice.user, opt_in=True, handle="Reader A")
    admin = _client(_admin())
    response = admin.post(
        "/contributions/admin/",
        data=json.dumps({"action": "exclude", "username": "alice", "reason": "abuse"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert admin.get("/contributions/leaderboard/").json()["rows"] == []
    assert _client(alice).get("/me/contributions/").json()["all_time_points"] == "10.00"


@pytest.mark.django_db
def test_handle_validation(enabled: Any) -> None:
    alice = _member("alice")
    bob = _member("bob")
    contributions.set_leaderboard_opt_in(user=alice.user, opt_in=True, handle="Reader")
    with pytest.raises(contributions.PreferenceError, match="taken"):
        contributions.set_leaderboard_opt_in(user=bob.user, opt_in=True, handle="reader")
    with pytest.raises(contributions.PreferenceError):
        contributions.set_leaderboard_opt_in(user=bob.user, opt_in=True, handle="<script>")
    with pytest.raises(contributions.PreferenceError):
        contributions.set_leaderboard_opt_in(user=bob.user, opt_in=True, handle="x")
    # Consent is required to join.
    response = _client(bob).post(
        "/me/contributions/leaderboard/",
        data=json.dumps({"opt_in": True, "display_handle": "Bob"}),
        content_type="application/json",
    )
    assert response.status_code == 400
    # After alice leaves, her handle is free again.
    contributions.set_leaderboard_opt_in(user=alice.user, opt_in=False)
    contributions.set_leaderboard_opt_in(user=bob.user, opt_in=True, handle="reader")


# --- Admin permissions ---------------------------------------------------------------------


@pytest.mark.django_db
def test_admin_page_is_admin_only(enabled: Any) -> None:
    member = _client(_member("alice"))
    assert member.get("/contributions/admin/").status_code == 403
    response = member.post(
        "/contributions/admin/",
        data=json.dumps({"action": "adjust", "username": "alice", "points": "100", "reason": "x"}),
        content_type="application/json",
    )
    assert response.status_code == 403
    assert member.get("/contributions/admin/users/alice/export/").status_code == 403
    assert ContributionEvent.objects.count() == 0


@pytest.mark.django_db
def test_admin_adjust_and_void_require_reasons(enabled: Any) -> None:
    alice = _member("alice")
    admin_profile = _admin()
    admin = _client(admin_profile)

    missing = admin.post(
        "/contributions/admin/",
        data=json.dumps({"action": "adjust", "username": "alice", "points": "5"}),
        content_type="application/json",
    )
    assert missing.status_code == 400
    ok = admin.post(
        "/contributions/admin/",
        data=json.dumps({"action": "adjust", "username": "alice", "points": "5", "reason": "x"}),
        content_type="application/json",
    )
    assert ok.status_code == 200
    adjustment = ContributionEvent.objects.get()
    assert adjustment.actor_id == admin_profile.user_id

    task = _scan_review(alice.user)
    _merge(_client(alice), task, Book.objects.create(title="Dune"))
    pending = ContributionEvent.objects.get(state=ContributionEvent.State.PENDING)
    voided = admin.post(
        "/contributions/admin/",
        data=json.dumps({"action": "void", "event_id": str(pending.pk), "reason": "spam"}),
        content_type="application/json",
    )
    assert voided.status_code == 200
    pending.refresh_from_db()
    assert pending.state == ContributionEvent.State.VOIDED
    assert contributions.scorecard(alice.user)["all_time_points"] == Decimal("5.00")


# --- GDPR ----------------------------------------------------------------------------------


@pytest.mark.django_db
def test_export_contains_only_own_data(enabled: Any) -> None:
    alice = _member("alice")
    _settled_points(alice.user, "4")
    _settled_points(_member("bob").user, "9")
    contributions.set_leaderboard_opt_in(user=alice.user, opt_in=True, handle="Reader A")

    response = _client(alice).get("/me/contributions/export/")
    assert response.status_code == 200
    assert response["Content-Disposition"].startswith("attachment")
    data = json.loads(response.content)
    assert [event["points"] for event in data["events"]] == ["4.00"]
    assert data["preference"]["display_handle"] == "Reader A"


@pytest.mark.django_db
def test_erase_removes_own_history_only(enabled: Any) -> None:
    alice = _member("alice")
    bob = _member("bob")
    _settled_points(alice.user, "4")
    _settled_points(bob.user, "9")
    ContributionMonthlyTotal.objects.create(
        user=alice.user,
        month=timezone.now().date().replace(day=1),
        kind=ContributionKind.REVIEW_RESOLVED,
        points=Decimal("2"),
        event_count=1,
    )
    contributions.set_leaderboard_opt_in(user=alice.user, opt_in=True, handle="Reader A")

    client = _client(alice)
    refused = client.post(
        "/me/contributions/erase/",
        data=json.dumps({"confirm": "no"}),
        content_type="application/json",
    )
    assert refused.status_code == 400
    response = client.post(
        "/me/contributions/erase/",
        data=json.dumps({"confirm": "erase"}),
        content_type="application/json",
    )
    assert response.status_code == 200
    assert not ContributionEvent.objects.filter(user=alice.user).exists()
    assert not ContributionMonthlyTotal.objects.filter(user=alice.user).exists()
    assert not ContributorPreference.objects.filter(user=alice.user).exists()
    assert ContributionEvent.objects.filter(user=bob.user).count() == 1
    assert contributions.export_user_data(alice.user)["events"] == []


@pytest.mark.django_db
def test_admin_can_export_and_erase_member(enabled: Any) -> None:
    alice = _member("alice")
    _settled_points(alice.user, "4")
    admin = _client(_admin())
    exported = admin.get("/contributions/admin/users/alice/export/")
    assert exported.status_code == 200
    assert len(json.loads(exported.content)["events"]) == 1
    erased = admin.post(
        "/contributions/admin/",
        data=json.dumps({"action": "erase", "username": "alice", "confirm": "erase"}),
        content_type="application/json",
    )
    assert erased.status_code == 200
    assert not ContributionEvent.objects.filter(user=alice.user).exists()


@pytest.mark.django_db
def test_retention_folds_old_events_into_monthly_totals(enabled: Any) -> None:
    alice = _member("alice")
    old = timezone.now() - timedelta(days=800)
    ContributionEvent.objects.create(
        user=alice.user,
        kind=ContributionKind.ADMIN_ADJUSTMENT,
        state=ContributionEvent.State.SETTLED,
        points=Decimal("3"),
        outcome_key="old",
        occurred_at=old,
        settle_after=old,
        settled_at=old,
    )
    _settled_points(alice.user, "2")
    before = contributions.scorecard(alice.user)["all_time_points"]

    report = contributions.settle()
    assert report.pruned == 1
    assert ContributionEvent.objects.filter(user=alice.user).count() == 1
    total = ContributionMonthlyTotal.objects.get(user=alice.user)
    assert total.points == Decimal("3.00") and total.event_count == 1
    assert contributions.scorecard(alice.user)["all_time_points"] == before == Decimal("5.00")


@pytest.mark.django_db
def test_settle_command_runs(enabled: Any) -> None:
    from io import StringIO

    out = StringIO()
    call_command("settle_contributions", stdout=out)
    assert "settled 0" in out.getvalue()
    assert django_settings.BOOK_SCANNER_CONTRIBUTIONS_ENABLED is True


@pytest.mark.django_db(transaction=True)
def test_scan_finalization_emits_pending_event_for_page_owner(
    enabled: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import hashlib
    import io

    from django.core.files.uploadedfile import SimpleUploadedFile
    from PIL import Image

    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path)
    monkeypatch.setattr("ebook_server.core.scan_pipeline.get_queue", lambda: None)
    operator = _member("scanner-op")
    client = _client(operator)
    book = Book.objects.create(title="Scanned for points")
    created = client.post(
        "/scan/jobs/",
        data={"provisional_title": "Scan", "scanner_id": "desk", "book_id": str(book.pk)},
    )
    assert created.status_code == 201
    job_id = created.json()["id"]
    stream = io.BytesIO()
    Image.new("RGB", (64, 96), (255, 255, 255)).save(stream, format="PNG")
    png = stream.getvalue()
    upload = client.post(
        f"/scan/jobs/{job_id}/pages/",
        data={
            "logical_sequence_number": "1",
            "side": "single",
            "checksum_sha256": hashlib.sha256(png).hexdigest(),
            "file": SimpleUploadedFile("page.png", png, content_type="image/png"),
        },
    )
    assert upload.status_code == 201, upload.content
    finish = client.post(f"/scan/jobs/{job_id}/finish/", data={"allow_raw_pages": "true"})
    assert finish.status_code in {200, 202}, finish.content

    assert ScanJob.objects.get(pk=job_id).state == ScanJob.State.COMPLETE
    event = ContributionEvent.objects.get()
    assert event.kind == ContributionKind.SCAN_COMPLETED
    assert event.user_id == operator.user_id
    assert event.state == ContributionEvent.State.PENDING
    contributions.settle(now=_later())
    event.refresh_from_db()
    assert event.points == Decimal("5.05")  # 5 + 0.05 * 1 final page
