"""Repeat provider searches from the review page: terms, provider outcomes, results."""

from __future__ import annotations

from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import pytest
from django.test import Client, override_settings

from ebook_server.core import identification
from ebook_server.core.identification import (
    OpenLibraryMetadataProvider,
    ProviderRequestError,
    resolve_candidates_with_report,
)
from ebook_server.core.models import MetadataCandidate, MetadataSearch, ProcessingJob, ReviewTask
from tests.test_identification_providers import _fake_urlopen, _signals
from tests.test_review import _start_low_confidence_import

HTML = {"Accept": "text/html"}


def _providers(monkeypatch: pytest.MonkeyPatch, *providers: Any) -> None:
    monkeypatch.setattr(identification, "_provider_instances", lambda: list(providers))


class _Returns:
    def __init__(self, provider_id: str, label: str, results: list[dict[str, Any]]) -> None:
        self.provider_id = provider_id
        self.label = label
        self.results = results
        self.seen: list[tuple[str | None, str | None]] = []

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        del isbn, folder_hint
        self.seen.append((title, author))
        return self.results


class _Fails:
    provider_id = "down"
    label = "Down Provider"

    def search(self, title: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
        del title, kwargs
        raise ProviderRequestError("Down Provider: unreachable (connection refused)")


@pytest.fixture
def review(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    client = Client()
    task, import_job = _start_low_confidence_import(
        monkeypatch=monkeypatch,
        client=client,
        tmp_path=tmp_path,
        username="reviewer-ui",
        candidate_title="Old Candidate",
    )
    return {"client": client, "task": task, "import_job": import_job}


@pytest.mark.django_db(transaction=True)
def test_search_uses_submitted_terms_without_saving_them(
    review: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: Client = review["client"]
    task: ReviewTask = review["task"]
    saved_title = task.inferred_title
    provider = _Returns("stub", "Stub Library", [{"title": "Neuromancer", "author_name": ["W G"]}])
    _providers(monkeypatch, provider)

    response = client.post(
        f"/reviews/{task.id}/search-again/",
        {"inferred_title": "Neuromancer", "inferred_authors": "William Gibson, Someone Else"},
        headers=HTML,
    )

    assert response.status_code == 302
    assert response["Location"] == f"/reviews/{task.id}/"
    task.refresh_from_db()
    assert task.inferred_title == saved_title  # searching does not save
    assert provider.seen[0] == ("Neuromancer", "William Gibson")
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    # ...but the form keeps showing what was searched for.
    assert 'name="inferred_title" value="Neuromancer"' in page
    assert 'value="William Gibson, Someone Else"' in page
    assert "Showing the terms of your last search" in page
    search = MetadataSearch.objects.filter(trigger=MetadataSearch.Trigger.REVIEW).get()
    assert search.query["title_hints"] == ["Neuromancer"]
    assert search.query["author"] == "William Gibson"
    assert search.requested_by is not None


@pytest.mark.django_db(transaction=True)
def test_saved_terms_win_over_older_search_and_drive_plain_searches(
    review: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: Client = review["client"]
    task: ReviewTask = review["task"]
    provider = _Returns("stub", "Stub Library", [])
    _providers(monkeypatch, provider)
    client.post(f"/reviews/{task.id}/search-again/", {"inferred_title": "Draft"}, headers=HTML)

    client.post(
        f"/reviews/{task.id}/edit-search-terms/",
        {"inferred_title": "Count Zero", "inferred_authors": "William Gibson"},
        headers=HTML,
    )
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'name="inferred_title" value="Count Zero"' in page
    assert "Showing the terms of your last search" not in page

    client.post(f"/reviews/{task.id}/search-again/")  # API call without terms
    assert provider.seen[-1] == ("Count Zero", "William Gibson")


@pytest.mark.django_db(transaction=True)
def test_save_terms_only_does_not_search(
    review: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: Client = review["client"]
    task: ReviewTask = review["task"]
    provider = _Returns("stub", "Stub Library", [])
    _providers(monkeypatch, provider)

    client.post(
        f"/reviews/{task.id}/edit-search-terms/",
        {"inferred_title": "Count Zero", "inferred_authors": "William Gibson"},
        headers=HTML,
    )

    task.refresh_from_db()
    assert task.inferred_title == "Count Zero"
    assert provider.seen == []


@pytest.mark.django_db(transaction=True)
def test_page_shows_each_provider_outcome_and_new_results(
    review: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: Client = review["client"]
    task: ReviewTask = review["task"]
    _providers(
        monkeypatch,
        _Returns("found", "Found Library", [{"title": "Fresh Result", "author_name": ["A"]}]),
        _Returns("empty", "Empty Library", []),
        _Fails(),
    )

    client.post(f"/reviews/{task.id}/search-again/", {"inferred_title": "Fresh"}, headers=HTML)
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()

    search = MetadataSearch.objects.filter(trigger=MetadataSearch.Trigger.REVIEW).get()
    outcomes = {p["provider_id"]: p for p in search.providers}
    assert outcomes["found"]["status"] == "results"
    assert outcomes["found"]["candidate_count"] == 1
    assert outcomes["empty"]["status"] == "no_results"
    assert outcomes["down"]["status"] == "error"
    assert "connection refused" in outcomes["down"]["error"]
    for text in (
        "Found Library",
        "Empty Library",
        "Down Provider",
        "No results",
        "connection refused",
    ):
        assert text in page
    assert "Fresh Result" in page
    assert page.index("Latest search") < page.index("Fresh Result")


@pytest.mark.django_db(transaction=True)
def test_failed_search_keeps_earlier_results_visible(
    review: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: Client = review["client"]
    task: ReviewTask = review["task"]
    import_job = review["import_job"]
    old = MetadataCandidate.objects.get(import_job=import_job)
    _providers(monkeypatch, _Fails())

    client.post(f"/reviews/{task.id}/search-again/", headers=HTML)
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()

    assert MetadataCandidate.objects.filter(pk=old.pk).exists()
    assert "Old Candidate" in page
    assert "Earlier searches" in page
    assert "This search returned no candidates" in page
    assert "Some providers failed" in page


@pytest.mark.django_db(transaction=True)
def test_queued_search_shows_banner_and_refreshes(review: dict[str, Any]) -> None:
    client: Client = review["client"]
    task: ReviewTask = review["task"]
    ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.REVIEW_IDENTIFICATION,
        details={"review_task_id": task.id, "import_job_id": str(task.import_job_id)},
    )

    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    payload = client.get(f"/reviews/{task.id}/").json()

    assert "Searching providers" in page
    assert 'http-equiv="refresh"' in page
    assert "Search running" in page
    assert payload["active_search"]["state"] == ProcessingJob.JobState.QUEUED


@pytest.mark.django_db(transaction=True)
def test_job_status_page_returns_to_review_and_json_lists_providers(
    review: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    client: Client = review["client"]
    task: ReviewTask = review["task"]
    _providers(monkeypatch, _Fails())
    response = client.post(f"/reviews/{task.id}/search-again/")
    job_id = response.json()["identification_job"]["job_id"]
    assert response.json()["identification_job"]["providers"][0]["status"] == "error"

    status = client.get(f"/review-identifications/{job_id}/", headers=HTML)
    assert status.status_code == 302
    assert status["Location"] == f"/reviews/{task.id}/"
    detail = client.get(f"/reviews/{task.id}/").json()
    assert detail["searches"][0]["providers"][0]["label"] == "Down Provider"
    assert detail["configured_providers"][0]["label"] == "Down Provider"


@pytest.mark.django_db(transaction=True)
def test_intake_search_is_recorded_and_linked_to_review(review: dict[str, Any]) -> None:
    search = MetadataSearch.objects.get(trigger=MetadataSearch.Trigger.INTAKE)
    assert search.review_task_id == review["task"].id
    assert search.state == MetadataSearch.State.COMPLETE
    assert search.candidates.get().title == "Old Candidate"


@override_settings(
    BOOK_SCANNER_METADATA_PROVIDERS=[
        "ebook_server.core.identification.OpenLibraryMetadataProvider",
        "ebook_server.core.nonexistent.Provider",
    ]
)
def test_unloadable_provider_is_reported() -> None:
    providers = identification.configured_providers()
    assert providers[0] == {
        "provider_id": "openlibrary",
        "label": "Open Library",
        "available": True,
    }
    assert providers[1]["available"] is False
    with patch(
        "ebook_server.core.identification.urlopen",
        _fake_urlopen({"search.json": {"docs": []}}, []),
    ):
        _candidates, outcomes = resolve_candidates_with_report(_signals(title_hints=["Dune"]))
    assert outcomes[0].status == "error"
    assert "could not be loaded" in outcomes[0].error
    assert outcomes[1].status == "no_results"


def _raising(exc: Exception) -> Any:
    def opener(request: Any, timeout: float = 0) -> Any:
        del request, timeout
        raise exc

    return opener


@pytest.mark.parametrize(
    ("exc", "message"),
    [
        (URLError(TimeoutError("timed out")), "timed out"),
        (URLError(ConnectionRefusedError("refused")), "unreachable"),
        (HTTPError("u", 503, "Service Unavailable", Message(), None), "HTTP 503"),
    ],
)
def test_open_library_outage_is_an_error_not_zero_results(exc: Exception, message: str) -> None:
    with (
        patch("ebook_server.core.identification.urlopen", _raising(exc)),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [OpenLibraryMetadataProvider()],
        ),
    ):
        candidates, outcomes = resolve_candidates_with_report(
            _signals(title_hints=["Dune"], author_hints=["Frank Herbert"])
        )
    assert candidates == []
    assert outcomes[0].status == "error"
    assert message in outcomes[0].error
    assert outcomes[0].requests[0]["status"] == "error"


def test_open_library_isbn_not_found_falls_back_to_title_search() -> None:
    seen: list[str] = []
    with (
        patch(
            "ebook_server.core.identification.urlopen",
            _fake_urlopen(
                {"search.json": {"docs": [{"key": "/works/OL1W", "title": "Dune"}]}}, seen
            ),
        ),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [OpenLibraryMetadataProvider()],
        ),
    ):
        candidates, outcomes = resolve_candidates_with_report(
            _signals(title_hints=["Dune"], isbns=["9780441172719"])
        )
    assert candidates[0].title == "Dune"
    assert outcomes[0].status == "results"
    statuses = [(r["request"].split(" ")[0], r["status"]) for r in outcomes[0].requests]
    assert statuses == [("ISBN", "no_results"), ("search", "results")]
