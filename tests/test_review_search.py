"""Review-page provider search: posted terms, provider errors, history, liveness (#40)."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
from django.test import Client
from django.utils import timezone

from ebook_server.core import identification, intake, metadata_search, scan_identification
from ebook_server.core.identification import (
    IdentificationInput,
    OpenLibraryMetadataProvider,
    ProviderRequestError,
)
from ebook_server.core.models import (
    ImportJob,
    MetadataCandidate,
    MetadataSearch,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    get_or_create_profile,
)
from tests.test_review import _start_low_confidence_import

HTML = {"Accept": "text/html"}


class _RecordingProvider:
    """Returns fixed records and remembers the terms it was asked for."""

    def __init__(self, provider_id: str, records: list[dict[str, Any]]) -> None:
        self.provider_id = provider_id
        self.records = records
        self.calls: list[dict[str, Any]] = []

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        self.calls.append({"title": title, "author": author, "isbn": isbn})
        return list(self.records)


class _FailingProvider:
    def __init__(self, provider_id: str, reason: str) -> None:
        self.provider_id = provider_id
        self.reason = reason

    def search(self, *args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        del args, kwargs
        raise ProviderRequestError(self.reason)


def _use_providers(monkeypatch: pytest.MonkeyPatch, *providers: Any) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: list(providers))


def _import_review(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    username: str,
) -> tuple[Client, ReviewTask, ImportJob]:
    client = Client()
    task, import_job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=client,
        tmp_path=tmp_path,
        username=username,
        candidate_title="Initial Candidate",
    )
    return client, task, import_job


def _current(import_job: ImportJob) -> list[MetadataCandidate]:
    return list(import_job.metadata_candidates.filter(superseded_at__isnull=True))


# ------------------------------------------------------------------ provider errors


def test_provider_errors_are_reported_separately_from_no_results() -> None:
    signals = IdentificationInput(
        title_hints=["Dune"],
        author_hints=[],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )
    empty = _RecordingProvider("empty", [])
    healthy = _RecordingProvider("healthy", [{"title": "Dune", "key": "/works/OL1W"}])
    with patch.object(
        identification,
        "_provider_instances",
        lambda: [_FailingProvider("down", "timed out"), empty, healthy],
    ):
        candidates, outcomes = identification.resolve_candidates_with_report(signals)

    assert [candidate.provider_id for candidate in candidates] == ["healthy"]
    by_id = {outcome.provider_id: outcome for outcome in outcomes}
    assert by_id["down"].status == "error"
    assert by_id["down"].error == "timed out"
    assert by_id["empty"].status == "no_results"
    assert by_id["healthy"].status == "results"
    assert by_id["healthy"].result_count == 1


def test_open_library_failures_raise_instead_of_returning_no_results() -> None:
    def timeout(request: Any, timeout: float = 0) -> Any:
        del request, timeout
        raise URLError(TimeoutError("timed out"))

    with patch.object(identification, "urlopen", timeout):
        with pytest.raises(ProviderRequestError) as excinfo:
            OpenLibraryMetadataProvider().search("Dune")
    assert excinfo.value.reason == "timed out"

    def server_error(request: Any, timeout: float = 0) -> Any:
        del timeout
        raise HTTPError(request.full_url, 503, "unavailable", None, None)  # type: ignore[arg-type]

    with patch.object(identification, "urlopen", server_error):
        with pytest.raises(ProviderRequestError) as excinfo:
            OpenLibraryMetadataProvider().search("Dune")
    assert excinfo.value.reason == "HTTP 503"
    # The reason never carries the request URL or query.
    assert "openlibrary" not in excinfo.value.reason


def test_open_library_unknown_isbn_is_a_miss_and_failed_isbn_lookup_is_an_error() -> None:
    def not_found(request: Any, timeout: float = 0) -> Any:
        del timeout
        raise HTTPError(request.full_url, 404, "not found", None, None)  # type: ignore[arg-type]

    with patch.object(identification, "urlopen", not_found):
        assert OpenLibraryMetadataProvider().search(None, isbn="9780441172719") == []

    def unavailable(request: Any, timeout: float = 0) -> Any:
        del timeout
        raise HTTPError(request.full_url, 502, "bad gateway", None, None)  # type: ignore[arg-type]

    with patch.object(identification, "urlopen", unavailable):
        with pytest.raises(ProviderRequestError):
            OpenLibraryMetadataProvider().search(None, isbn="9780441172719")


@pytest.mark.django_db(transaction=True)
def test_review_page_shows_provider_outcomes_and_escapes_them(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "search-outcomes")
    _use_providers(
        monkeypatch,
        _FailingProvider("down", "<b>timed out</b>"),
        _RecordingProvider("healthy", [{"title": "Found Book", "key": "/works/OL2W"}]),
    )

    response = client.post(f"/reviews/{task.id}/search-again/")
    assert response.status_code == 202

    page = client.get(f"/reviews/{task.id}/", headers=HTML)
    html = page.content.decode()
    assert "Latest search" in html
    assert 'data-provider-status="error"' in html
    assert "Error: &lt;b&gt;timed out&lt;/b&gt;" in html
    assert "<b>timed out</b>" not in html
    payload = client.get(f"/reviews/{task.id}/").json()
    latest = payload["searches"][0]
    assert latest["trigger"] == MetadataSearch.Trigger.REVIEW
    assert {row["provider_id"]: row["status"] for row in latest["providers"]} == {
        "down": "error",
        "healthy": "results",
    }
    assert "down: error" in latest["summary"]


# ------------------------------------------------------------- posted search terms


@pytest.mark.django_db(transaction=True)
def test_search_uses_and_saves_the_posted_terms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "search-posted-terms")
    provider = _RecordingProvider("stub", [{"title": "Edited Title", "key": "/works/OL3W"}])
    _use_providers(monkeypatch, provider)

    response = client.post(
        f"/reviews/{task.id}/search-again/",
        {"inferred_title": "Edited Title", "inferred_authors": "Edna Editor"},
        headers=HTML,
    )

    assert response.status_code == 302
    assert response["Location"] == f"/reviews/{task.id}/"
    assert provider.calls[0]["title"] == "Edited Title"
    assert provider.calls[0]["author"] == "Edna Editor"
    task.refresh_from_db()
    assert task.inferred_title == "Edited Title"
    assert task.inferred_authors == ["Edna Editor"]
    search = MetadataSearch.objects.get(trigger=MetadataSearch.Trigger.REVIEW)
    assert search.query["title_hints"][0] == "Edited Title"
    assert search.query["author"] == "Edna Editor"


@pytest.mark.django_db(transaction=True)
def test_review_form_posts_terms_to_the_search_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "search-form-shape")
    html = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    form_start = html.index(f'action="/reviews/{task.id}/search-again/"')
    form = html[form_start : html.index("</form>", form_start)]
    assert 'name="inferred_title"' in form
    assert 'name="inferred_authors"' in form
    assert "csrfmiddlewaretoken" in form
    assert f'formaction="/reviews/{task.id}/edit-search-terms/"' in form


# ---------------------------------------------------------------- non-destructive


@pytest.mark.django_db(transaction=True)
def test_empty_or_failed_repeat_search_keeps_earlier_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, import_job = _import_review(monkeypatch, tmp_path, "search-keep-results")
    original = _current(import_job)
    assert [candidate.title for candidate in original] == ["Initial Candidate"]

    _use_providers(monkeypatch, _RecordingProvider("stub", []))
    assert client.post(f"/reviews/{task.id}/search-again/").status_code == 202
    _use_providers(monkeypatch, _FailingProvider("stub", "unreachable"))
    assert client.post(f"/reviews/{task.id}/search-again/").status_code == 202

    assert [candidate.id for candidate in _current(import_job)] == [original[0].id]
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert "Initial Candidate" in page
    assert "data-current-from-earlier-search" in page
    states = list(
        MetadataSearch.objects.filter(import_job=import_job)
        .order_by("finished_at")
        .values_list("trigger", "candidate_count")
    )
    assert states == [("intake", 1), ("review", 0), ("review", 0)]
    accept = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": str(original[0].id)},
    )
    assert accept.status_code == 200


@pytest.mark.django_db(transaction=True)
def test_new_results_supersede_older_candidates_but_keep_them_as_history(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, import_job = _import_review(monkeypatch, tmp_path, "search-supersede")
    old = _current(import_job)[0]
    # The same provider record again plus a new one: unique only among current rows.
    _use_providers(
        monkeypatch,
        _RecordingProvider(
            "stub",
            [{"title": "Newer Candidate", "key": "/works/NEW"}, {"title": "Initial Candidate"}],
        ),
    )
    assert client.post(f"/reviews/{task.id}/search-again/").status_code == 202

    old.refresh_from_db()
    assert old.superseded_at is not None
    current_titles = sorted(candidate.title for candidate in _current(import_job))
    assert current_titles == ["Initial Candidate", "Newer Candidate"]
    assert import_job.metadata_candidates.count() == 3

    payload = client.get(f"/reviews/{task.id}/").json()
    assert sorted(candidate["title"] for candidate in payload["candidates"]) == current_titles
    queue = client.get("/reviews/").json()["items"]
    assert [item["candidate_count"] for item in queue if item["id"] == str(task.id)] == [2]

    stale_accept = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": str(old.id)},
    )
    assert stale_accept.status_code == 400
    task.refresh_from_db()
    assert task.state != ReviewTask.State.RESOLVED

    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert "Earlier searches (1)" in page
    assert "superseded" in page


@pytest.mark.django_db(transaction=True)
def test_failure_after_provider_search_is_recorded_as_failed_run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, import_job = _import_review(monkeypatch, tmp_path, "search-save-fails")
    _use_providers(monkeypatch, _RecordingProvider("stub", [{"title": "Lost"}]))

    def broken_save(**kwargs: Any) -> None:
        raise RuntimeError("database went away")

    monkeypatch.setattr(metadata_search, "save_candidates", broken_save)
    response = client.post(f"/reviews/{task.id}/search-again/")

    assert response.status_code == 202
    job = ProcessingJob.objects.get(job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION)
    assert job.state == ProcessingJob.JobState.FAILED
    runs = MetadataSearch.objects.filter(import_job=import_job, trigger="review")
    assert [(run.state, run.error) for run in runs] == [
        (MetadataSearch.State.FAILED, "saving the results failed: RuntimeError")
    ]
    assert runs[0].providers[0]["status"] == "results"
    assert [candidate.title for candidate in _current(import_job)] == ["Initial Candidate"]


@pytest.mark.django_db(transaction=True)
def test_intake_retry_replaces_only_its_own_run_and_unkeyed_records_do_not_collide(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, _task, import_job = _import_review(monkeypatch, tmp_path, "search-intake-retry")
    signals = identification.IdentificationInput(
        title_hints=["Anything"],
        author_hints=[],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )
    unkeyed = [
        identification.IdentificationCandidate(
            provider_id="stub",
            provider_ref="",
            title=title,
            authors=[],
            isbn10=None,
            isbn13=None,
            language=None,
            description=None,
            publisher=None,
            edition=None,
            publication_year=None,
            score=0.2,
            raw={},
        )
        for title in ("First", "Second")
    ]
    intake._save_metadata_candidates(import_job, unkeyed, signals=signals, outcomes=[])
    intake._save_metadata_candidates(import_job, unkeyed, signals=signals, outcomes=[])

    assert MetadataSearch.objects.filter(import_job=import_job).count() == 1
    refs = sorted(import_job.metadata_candidates.values_list("provider_ref", flat=True))
    assert refs == ["result-1", "result-2"]


# ------------------------------------------------------------------ liveness


def _search_job(
    task: ReviewTask, state: str, *, age: timedelta, terms: Any = None
) -> ProcessingJob:
    moment = timezone.now() - age
    return ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION,
        state=state,
        scheduled_at=moment,
        started_at=moment if state == ProcessingJob.JobState.RUNNING else None,
        details={
            "review_task_id": task.id,
            "import_job_id": str(task.import_job_id),
            "terms": terms if terms is not None else metadata_search.task_terms(task),
        },
    )


@pytest.mark.django_db(transaction=True)
def test_live_search_job_locks_and_refreshes_the_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "search-live")
    _search_job(task, ProcessingJob.JobState.RUNNING, age=timedelta(seconds=5))

    html = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'http-equiv="refresh"' in html
    assert "Searching providers" in html
    assert '<button type="submit" disabled>Search providers again</button>' in html
    assert client.get(f"/reviews/{task.id}/").json()["search_in_progress"] is True


@pytest.mark.django_db(transaction=True)
def test_search_left_running_by_a_dead_worker_does_not_lock_the_page(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, import_job = _import_review(monkeypatch, tmp_path, "search-dead-worker")
    dead = _search_job(task, ProcessingJob.JobState.RUNNING, age=timedelta(hours=2))

    html = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'http-equiv="refresh"' not in html
    assert '<button type="submit">Search providers again</button>' in html
    assert "did not finish" in html
    assert client.get(f"/reviews/{task.id}/").json()["search_in_progress"] is False

    _use_providers(monkeypatch, _RecordingProvider("stub", [{"title": "Fresh Result"}]))
    response = client.post(f"/reviews/{task.id}/search-again/")
    assert response.status_code == 202
    dead.refresh_from_db()
    assert dead.state == ProcessingJob.JobState.FAILED
    assert response.json()["identification_job"]["job_id"] != str(dead.job_id)
    assert [candidate.title for candidate in _current(import_job)] == ["Fresh Result"]


@pytest.mark.django_db(transaction=True)
def test_running_search_window_is_shorter_than_the_generic_job_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, task, _import_job = _import_review(monkeypatch, tmp_path, "search-window")
    seconds = metadata_search.DEFAULT_SEARCH_STALE_SECONDS + 60
    running = _search_job(task, ProcessingJob.JobState.RUNNING, age=timedelta(seconds=seconds))
    queued = _search_job(task, ProcessingJob.JobState.QUEUED, age=timedelta(seconds=seconds))

    assert metadata_search.is_stale_search_job(running)
    # A queued job may legitimately wait behind long conversions.
    assert not metadata_search.is_stale_search_job(queued)


@pytest.mark.django_db(transaction=True)
def test_new_terms_update_a_queued_search_and_are_rejected_while_one_runs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "search-queued-terms")

    class DeferredQueue:
        def enqueue(self, *args: Any, **kwargs: Any) -> None:
            del args, kwargs

    monkeypatch.setattr("ebook_server.core.review_identification.get_queue", DeferredQueue)
    first = client.post(f"/reviews/{task.id}/search-again/", {"inferred_title": "First"})
    second = client.post(f"/reviews/{task.id}/search-again/", {"inferred_title": "Second"})
    assert first.status_code == second.status_code == 202
    job_id = first.json()["identification_job"]["job_id"]
    assert second.json()["identification_job"]["job_id"] == job_id
    job = ProcessingJob.objects.get(job_id=job_id)
    assert job.details["terms"]["title"] == "Second"

    job.state = ProcessingJob.JobState.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=["state", "started_at"])
    rejected = client.post(f"/reviews/{task.id}/search-again/", {"inferred_title": "Third"})
    assert rejected.status_code == 409
    assert "already running" in rejected.json()["error"]
    html_rejected = client.post(
        f"/reviews/{task.id}/search-again/",
        {"inferred_title": "Third"},
        headers=HTML,
        follow=True,
    )
    assert "already running" in html_rejected.content.decode()
    task.refresh_from_db()
    assert task.inferred_title == "Third"  # saved, to search once the running one ends
    assert task.state == ReviewTask.State.OPEN


@pytest.mark.django_db(transaction=True)
def test_search_actions_are_limited_to_owner_or_admin(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _client, task, _import_job = _import_review(monkeypatch, tmp_path, "search-perm-owner")
    other = Client()
    other.force_login(get_or_create_profile("search-perm-other").user)
    assert other.post(f"/reviews/{task.id}/search-again/").status_code == 403
    assert other.get(f"/reviews/{task.id}/").status_code == 403
    admin = Client()
    admin.force_login(get_or_create_profile("search-perm-admin", role="admin").user)
    _use_providers(monkeypatch, _RecordingProvider("stub", []))
    assert admin.post(f"/reviews/{task.id}/search-again/").status_code == 202


# ------------------------------------------------------------- scan saved terms


class _ScanProvider(_RecordingProvider):
    def __init__(self) -> None:
        super().__init__("scan-stub", [{"key": "/works/SCAN", "title": "Scan Result"}])


def _scan_review(monkeypatch: pytest.MonkeyPatch, username: str) -> tuple[Client, ScanJob]:
    profile = get_or_create_profile(username)
    client = Client()
    client.force_login(profile.user)
    response = client.post(
        "/scan/jobs/",
        {"provisional_title": "Provisional Title", "provisional_authors": "Pro Visional"},
    )
    assert response.status_code == 201
    scan_job = ScanJob.objects.get(pk=response.json()["id"])
    _use_providers(monkeypatch, _ScanProvider())
    assert client.post(f"/scan/jobs/{scan_job.id}/identify/").status_code == 202
    return client, scan_job


@pytest.mark.django_db(transaction=True)
def test_scan_review_keeps_saved_terms_after_searching(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, scan_job = _scan_review(monkeypatch, "scan-saved-terms")
    task = ReviewTask.objects.get(scan_job=scan_job)
    saved = client.post(
        f"/reviews/{task.id}/edit-search-terms/",
        {"inferred_title": "Saved Title", "inferred_authors": "Sam Saved"},
    )
    assert saved.status_code == 200

    provider = _ScanProvider()
    _use_providers(monkeypatch, provider)
    # The review form always posts the fields, exactly as shown.
    response = client.post(
        f"/reviews/{task.id}/search-again/",
        {"inferred_title": "Saved Title", "inferred_authors": "Sam Saved"},
    )

    assert response.status_code == 202
    assert provider.calls[0]["title"] == "Saved Title"
    task.refresh_from_db()
    assert task.inferred_title == "Saved Title"
    assert task.inferred_authors == ["Sam Saved"]
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'name="inferred_title" value="Saved Title"' in page


@pytest.mark.django_db(transaction=True)
def test_later_scan_identification_does_not_overwrite_saved_review_terms(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, scan_job = _scan_review(monkeypatch, "scan-refresh-terms")
    task = ReviewTask.objects.get(scan_job=scan_job)
    assert (
        client.post(
            f"/reviews/{task.id}/edit-search-terms/",
            {"inferred_title": "Saved Title", "inferred_authors": "Sam Saved"},
        ).status_code
        == 200
    )
    scan_job.refresh_from_db()
    assert scan_job.provisional_title == "Saved Title"

    # The scanner (or finishing the scan) re-identifies with other hints.
    ScanJob.objects.filter(pk=scan_job.pk).update(provisional_title="Scanner Hint")
    scan_job.refresh_from_db()
    scan_identification.request_scan_identification(scan_job=scan_job, requested_by_user_id=None)

    task.refresh_from_db()
    assert task.inferred_title == "Saved Title"
    assert task.inferred_authors == ["Sam Saved"]
    latest = MetadataSearch.objects.filter(scan_job=scan_job).order_by("-finished_at").first()
    assert latest is not None
    assert latest.query["title_hints"] == ["Scanner Hint"]
