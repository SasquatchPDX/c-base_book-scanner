"""Review page ISBN lookup (#59).

The reviewer reads the ISBN from the actual ebook and types it. It is only a query:
a candidate counts as verified when the provider's own record lists that ISBN. One
distinct verified book is accepted automatically; several are shown for choice.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from django.test import Client

from ebook_server.core import metadata_search, review_identification
from ebook_server.core.models import (
    Identifier,
    ImportJob,
    MetadataCandidate,
    MetadataSearch,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    get_or_create_profile,
)
from tests.test_review_search import (
    HTML,
    _CallbackProvider,
    _FailingProvider,
    _import_review,
    _RecordingProvider,
    _scan_review,
    _use_providers,
)

DUNE_13 = "9780441172719"
DUNE_10 = "0441172717"


def _edition(
    title: str,
    *,
    isbn13: str = DUNE_13,
    key: str = "/books/OL1M",
    authors: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "key": key,
        "title": title,
        "author_name": ["Frank Herbert"] if authors is None else authors,
        "isbn_13": [isbn13],
        "publishers": ["Ace"],
    }


def _candidate(raw: dict[str, Any]) -> MetadataCandidate:
    return MetadataCandidate(title="x", authors=[], raw_payload=raw)


def test_only_identifiers_in_the_provider_record_count_as_evidence() -> None:
    # Open Library / Hardcover / DNB shapes, ISBN-10 vs ISBN-13 equivalence.
    assert metadata_search.record_carries_isbn(_candidate({"isbn_13": [DUNE_13]}), DUNE_10)
    assert metadata_search.record_carries_isbn(_candidate({"isbn_10": DUNE_10}), DUNE_13)
    assert metadata_search.record_carries_isbn(_candidate({"isbn": ["0-441-17271-7"]}), DUNE_13)
    # Google Books volumeInfo.industryIdentifiers.
    google = {"volumeInfo": {"industryIdentifiers": [{"type": "ISBN_10", "identifier": DUNE_10}]}}
    assert metadata_search.record_carries_isbn(_candidate(google), DUNE_13)
    # The lookup query echoed into the payload is ours, not the provider's.
    echoed = {"isbn_13": ["9780306406157"], "_queried_isbn": DUNE_13}
    assert not metadata_search.record_carries_isbn(_candidate(echoed), DUNE_13)
    assert not metadata_search.record_carries_isbn(_candidate({}), DUNE_13)


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
    assert lookup["auto_accepted"] is True

    # One verified book: accepted through the normal accept path, metadata used fully.
    task.refresh_from_db()
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision == ReviewTask.Decision.ACCEPT_CANDIDATE
    assert str(task.selected_candidate_id) == best_id
    assert task.reviewer is not None
    assert task.reviewer.get_username() == "isbn-verified"
    assert task.decision_payload["resolution_source"] == "reviewer_isbn"
    assert task.decision_payload["isbn"] == DUNE_13
    book = task.resolved_book
    assert book is not None
    assert book.title == "Dune"
    assert book.publisher.lower() == "ace"  # provider metadata, normalized
    assert [author.full_name for author in book.authors.all()] == ["Frank Herbert"]
    assert Identifier.objects.filter(book=book, value=DUNE_13).exists()
    import_job.refresh_from_db()
    assert import_job.state == ImportJob.State.COMPLETE
    assert import_job.book_id == book.id
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert "data-isbn-auto-accepted" in page
    assert "Accept ISBN match" not in page


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
    lookup = client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]
    assert lookup["unambiguous"] is True
    assert lookup["auto_accepted"] is True


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
    task.refresh_from_db()
    assert task.state != ReviewTask.State.RESOLVED


@pytest.mark.django_db(transaction=True)
def test_queried_isbn_echoed_into_a_different_record_is_not_verified(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDXDJN's probe: requested Dune, got "Different Book" carrying only another ISBN."""
    client, task, import_job = _import_review(monkeypatch, tmp_path, "isbn-echo")
    record = {
        "key": "/books/OL9M",
        "title": "Different Book",
        "author_name": ["Some Author"],
        "isbn_13": ["9780306406157"],
        "_queried_isbn": DUNE_13,  # what Open Library's ISBN lookup adds to the payload
    }
    _use_providers(monkeypatch, _RecordingProvider("openlibrary", [record]))

    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202

    candidate = import_job.metadata_candidates.get(superseded_at__isnull=True)
    assert candidate.title == "Different Book"
    assert candidate.isbn13 == "9780306406157"  # the echo is not copied onto it either
    lookup = client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]
    assert lookup["verified_candidate_ids"] == []
    assert lookup["auto_accepted"] is False
    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert 'data-isbn-lookup="none"' in page


@pytest.mark.django_db(transaction=True)
def test_verified_record_without_authors_is_offered_not_auto_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "isbn-no-authors")
    _use_providers(monkeypatch, _RecordingProvider("stub", [_edition("Dune", authors=[])]))

    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202

    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN
    page = client.get(f"/reviews/{task.id}/", headers=HTML).content.decode()
    assert "Accept ISBN match" in page
    assert 'name="authors"' in page


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
    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN  # several books: the reviewer chooses

    _use_providers(
        monkeypatch,
        _RecordingProvider("one", [_edition("Dune")]),
        _RecordingProvider("two", [_edition("DUNE", key="/books/OL3M")]),
    )
    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202
    lookup = client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]
    assert lookup["unambiguous"] is True
    assert len(lookup["verified_candidate_ids"]) == 2
    assert lookup["auto_accepted"] is True  # two providers, one book


@pytest.mark.django_db(transaction=True)
def test_isbn_match_is_not_auto_accepted_into_a_review_resolved_meanwhile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, _import_job = _import_review(monkeypatch, tmp_path, "isbn-resolved")

    def resolve_meanwhile() -> None:
        assert client.post(f"/reviews/{task.id}/unidentified/").status_code == 200

    _use_providers(monkeypatch, _CallbackProvider([_edition("Dune")], resolve_meanwhile))
    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202

    task.refresh_from_db()
    assert task.decision == ReviewTask.Decision.MARK_UNIDENTIFIED
    assert task.resolved_book is None


@pytest.mark.django_db(transaction=True)
def test_isbn_match_of_a_superseded_search_is_not_auto_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, task, import_job = _import_review(monkeypatch, tmp_path, "isbn-superseded")

    def superseded_meanwhile() -> None:
        # A newer search becomes current after this one has published.
        ImportJob.objects.filter(pk=import_job.pk).update(current_search_job_id=None)

    original_publish = review_identification._publish

    def publish_then_supersede(**kwargs: Any) -> str:
        reason = original_publish(**kwargs)
        superseded_meanwhile()
        return reason

    monkeypatch.setattr(review_identification, "_publish", publish_then_supersede)
    _use_providers(monkeypatch, _RecordingProvider("stub", [_edition("Dune")]))
    assert client.post(f"/reviews/{task.id}/isbn-lookup/", {"isbn": DUNE_13}).status_code == 202

    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN
    assert client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]["auto_accepted"] is False


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
    lookup = client.get(f"/reviews/{task.id}/").json()["isbn_lookup"]
    assert lookup["unambiguous"] is True
    assert lookup["auto_accepted"] is True
    task.refresh_from_db()
    assert task.inferred_title == "Provisional Title"
    assert task.state == ReviewTask.State.RESOLVED
    assert task.decision_payload["resolution_source"] == "reviewer_isbn"
    scan_job = ScanJob.objects.get(pk=scan_job.pk)
    assert scan_job.book is not None
    assert scan_job.book.title == "Dune"


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
