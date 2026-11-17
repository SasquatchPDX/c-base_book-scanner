"""Review page ISBN lookup: typed ISBN is a query, verified only by provider records (#59)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from ebook_server.core import metadata_search
from ebook_server.core.models import (
    Identifier,
    MetadataSearch,
    ProcessingJob,
    ReviewTask,
    get_or_create_profile,
)
from tests.test_review_search import (
    HTML,
    _FailingProvider,
    _import_review,
    _RecordingProvider,
    _scan_review,
    _use_providers,
)

DUNE_13 = "9780441172719"
DUNE_10 = "0441172717"


def _edition(title: str, *, isbn13: str = DUNE_13, key: str = "/books/OL1M") -> dict[str, Any]:
    return {"key": key, "title": title, "author_name": ["Frank Herbert"], "isbn_13": [isbn13]}


def test_parse_isbn_accepts_formatted_values_and_rejects_bad_check_digits() -> None:
    assert metadata_search.parse_isbn("978-0-441-17271-9") == DUNE_13
    assert metadata_search.parse_isbn(" 978 0441172719 ") == DUNE_13
    assert metadata_search.parse_isbn("ISBN: 0-441-17271-7") == DUNE_10
    assert metadata_search.parse_isbn("978-0-441-17271-8") is None  # wrong check digit
    assert metadata_search.parse_isbn("9780441172719, 0441172717") is None  # two values
    assert metadata_search.parse_isbn("Dune") is None
    assert metadata_search.parse_isbn(None) is None


@pytest.mark.django_db(transaction=True)
def test_invalid_isbn_is_rejected_without_queueing_a_search(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "isbn-invalid")
    jobs_before = ProcessingJob.objects.count()

    response = client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": "978-0-441-17271-8"})
    assert response.status_code == 400
    html = client.post(
        f"/reviews/{task.id}/isbn-lookup/",
        {"isbn": "not an isbn"},
        headers=HTML,
        follow=True,
    )
    assert "ISBN lookup not started" in html.content.decode()
    assert ProcessingJob.objects.count() == jobs_before


@pytest.mark.django_db(transaction=True)
def test_isbn_lookup_queries_by_isbn_only_and_verifies_the_returned_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, import_job = _import_review(monkeypatch, tmp_path, "isbn-verified")
    title_before = task.inferred_title
    provider = _RecordingProvider("stub", [_edition("Dune")])
    _use_providers(monkeypatch, provider)

    response = client.post(
        f"/reviews/{task.id}/isbn-lookup/",
        {"isbn": "978-0-441-17271-9"},
        headers=HTML,
    )

    assert response.status_code == 302
    assert provider.calls == [{"title": None, "author": None, "isbn": DUNE_13}]
    search = MetadataSearch.objects.get(import_job=import_job, trigger="review")
    assert search.query["mode"] == "isbn"
    assert search.query["isbns"][0] == DUNE_13
    task.refresh_from_db()
    assert task.inferred_title == title_before  # an ISBN lookup does not change saved terms

    payload = client.get(f"/reviews/{task.id}/").json()
    lookup = payload["isbn_lookup"]
    assert lookup["unambiguous"] is True
    best_id = lookup["best_candidate_id"]
    assert lookup["verified_candidate_ids"] == [best_id]
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'data-isbn-lookup="match"' in page
    assert "Accept ISBN match" in page
    assert "ISBN verified" in page

    accept = client.post(f"/reviews/{task.id}/accept-candidate/", {"candidate_id": best_id})
    assert accept.status_code == 200
    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED
    assert task.resolved_book is not None
    assert Identifier.objects.filter(book=task.resolved_book, value=DUNE_13).exists()


@pytest.mark.django_db(transaction=True)
def test_isbn10_lookup_verifies_records_carrying_the_isbn13_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "isbn-ten")
    provider = _RecordingProvider("stub", [_edition("Dune")])
    _use_providers(monkeypatch, provider)

    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_10}).status_code == 202

    assert provider.calls[0]["isbn"] == DUNE_10
    assert client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]["unambiguous"] is True


@pytest.mark.django_db(transaction=True)
def test_record_without_the_typed_isbn_is_not_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "isbn-other-record")
    _use_providers(
        monkeypatch, _RecordingProvider("stub", [_edition("Other Book", isbn13="9780306406157")])
    )

    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202

    lookup = client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]
    assert lookup["verified_candidate_ids"] == []
    assert lookup["best_candidate_id"] is None
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'data-isbn-lookup="none"' in page
    assert "Accept ISBN match" not in page


@pytest.mark.django_db(transaction=True)
def test_conflicting_isbn_records_are_ambiguous_and_agreeing_ones_are_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "isbn-ambiguous")
    _use_providers(
        monkeypatch,
        _RecordingProvider("one", [_edition("Dune")]),
        _RecordingProvider("two", [_edition("Dune Messiah", key="/books/OL2M")]),
    )
    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202
    lookup = client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]
    assert lookup["unambiguous"] is False
    assert len(lookup["verified_candidate_ids"]) == 2
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'data-isbn-lookup="ambiguous"' in page
    assert "Accept ISBN match" not in page

    _use_providers(
        monkeypatch,
        _RecordingProvider("one", [_edition("Dune")]),
        _RecordingProvider("two", [_edition("DUNE", key="/books/OL3M")]),
    )
    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202
    lookup = client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]
    assert lookup["unambiguous"] is True
    assert len(lookup["verified_candidate_ids"]) == 2


@pytest.mark.django_db(transaction=True)
def test_failed_isbn_lookup_keeps_earlier_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, import_job = _import_review(monkeypatch, tmp_path, "isbn-failed")
    _use_providers(monkeypatch, _FailingProvider("stub", "timed out"))

    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202

    current = import_job.metadata_candidates.filter(superseded_at__isnull=True)
    assert [candidate.title for candidate in current] == ["Initial Candidate"]
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'data-isbn-lookup="none"' in page
    assert "Error: timed out" in page
    assert "1 provider failed" in page


@pytest.mark.django_db(transaction=True)
def test_isbn_lookup_works_for_scan_reviews(monkeypatch: pytest.MonkeyPatch) -> None:
    client, scan_job = _scan_review(monkeypatch, "isbn-scan")
    task = ReviewTask.objects.get(scan_job=scan_job)
    provider = _RecordingProvider("stub", [_edition("Dune")])
    _use_providers(monkeypatch, provider)

    response = client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13})

    assert response.status_code == 202
    assert provider.calls == [{"title": None, "author": None, "isbn": DUNE_13}]
    assert client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]["unambiguous"] is True
    task.refresh_from_db()
    assert task.inferred_title == "Provisional Title"


@pytest.mark.django_db(transaction=True)
def test_isbn_lookup_is_limited_to_owner_or_admin_and_has_csrf_form(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "isbn-perm-owner")
    other = Client()
    other.force_login(get_or_create_profile("isbn-perm-other").user)
    assert other.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 403

    html = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    form_start = html.index(f'action="/reviews/{task.id}/isbn-lookup/"')
    form = html[form_start : html.index("</form>", form_start)]
    assert 'name="isbn"' in form
    assert "csrfmiddlewaretoken" in form
