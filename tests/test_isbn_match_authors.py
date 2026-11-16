"""Authors for ISBN matches: work-record fallback, file evidence, conflict guard."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core import intake
from ebook_server.core.identification import (
    IdentificationCandidate,
    OpenLibraryMetadataProvider,
    authors_conflict,
    evidence_author_names,
)
from ebook_server.core.models import ImportJob, ReviewTask, get_or_create_profile
from ebook_server.storage import LocalFilesystemStorage
from tests.test_identification_providers import _fake_urlopen
from tests.test_intake import _build_epub, _configure_storage

EDITION_WITHOUT_AUTHORS = {
    "key": "/books/OL24282198M",
    "title": "Phylogenesis",
    "works": [{"key": "/works/OL102541W"}],
    "isbn_10": ["034549427X"],
}
WORK = {
    "key": "/works/OL102541W",
    "title": "Phylogenesis",
    "authors": [{"type": {"key": "/type/author_role"}, "author": {"key": "/authors/OL29487A"}}],
}


def test_isbn_edition_without_authors_uses_work_authors() -> None:
    seen: list[str] = []
    routes: dict[str, object] = {
        "/isbn/": EDITION_WITHOUT_AUTHORS,
        "/works/OL102541W.json": WORK,
        "/authors/OL29487A.json": {"name": "Alan Dean Foster"},
    }
    with patch("ebook_server.core.identification.urlopen", _fake_urlopen(routes, seen)):
        hits = OpenLibraryMetadataProvider().search("Phylogenesis", isbn="034549427X")
    assert hits[0]["author_name"] == ["Alan Dean Foster"]
    assert any("/works/OL102541W.json" in url for url in seen)


def test_isbn_edition_with_authors_does_not_fetch_work() -> None:
    seen: list[str] = []
    edition = {**EDITION_WITHOUT_AUTHORS, "authors": [{"key": "/authors/OL1A"}]}
    routes: dict[str, object] = {
        "/isbn/": edition,
        "/authors/OL1A.json": {"name": "Edition Author"},
    }
    with patch("ebook_server.core.identification.urlopen", _fake_urlopen(routes, seen)):
        hits = OpenLibraryMetadataProvider().search("Phylogenesis", isbn="034549427X")
    assert hits[0]["author_name"] == ["Edition Author"]
    assert not any("/works/" in url for url in seen)


def test_unreachable_work_record_leaves_authors_empty() -> None:
    routes: dict[str, object] = {"/isbn/": EDITION_WITHOUT_AUTHORS}  # work URL answers 404
    with patch("ebook_server.core.identification.urlopen", _fake_urlopen(routes, [])):
        hits = OpenLibraryMetadataProvider().search("Phylogenesis", isbn="034549427X")
    assert "author_name" not in hits[0]


def test_evidence_author_names() -> None:
    assert evidence_author_names(
        {
            "authors": ["Foster, Alan Dean", "Unknown"],
            "path_layout": {"authors": ["Alan Dean Foster", "Second Person"]},
        }
    ) == ["Alan Dean Foster", "Second Person"]
    assert evidence_author_names({}) == []


@pytest.mark.parametrize(
    ("candidate", "evidence", "conflict"),
    [
        (["Alan Dean Foster"], ["Foster, Alan Dean"], False),
        (["A. D. Foster"], ["Alan Dean Foster"], False),
        (["Iain Banks"], ["Iain M. Banks"], False),
        (["Somebody Else"], ["Alan Dean Foster"], True),
        ([], ["Alan Dean Foster"], False),
        (["Alan Dean Foster"], [], False),
    ],
)
def test_authors_conflict(candidate: list[str], evidence: list[str], conflict: bool) -> None:
    assert authors_conflict(candidate, evidence) is conflict


def _candidate(authors: list[str]) -> IdentificationCandidate:
    return IdentificationCandidate(
        provider_id="openlibrary",
        provider_ref="/books/OL1M",
        title="Sample EPUB",
        authors=authors,
        isbn10="034549427X",
        isbn13=None,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=1.0,
        raw={},
    )


def _upload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, authors: list[str]) -> ImportJob:
    monkeypatch.setattr(
        "ebook_server.core.identification.resolve_candidates",
        lambda signals: [_candidate(authors)],
    )
    _configure_storage(tmp_path)
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root / "library-artifacts")),
    )
    client = Client()
    client.force_login(get_or_create_profile("isbn-uploader").user)
    epub_path = tmp_path / "book.epub"
    _build_epub(epub_path, "Sample EPUB")  # dc:creator "Jane Doe"
    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "sample.epub", epub_path.read_bytes(), content_type="application/epub+zip"
            )
        },
    )
    assert response.status_code == 201
    return ImportJob.objects.select_related("book").get(pk=response.json()["job_id"])


@pytest.mark.django_db(transaction=True)
def test_match_without_authors_takes_authors_from_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _upload(tmp_path, monkeypatch, authors=[])

    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert list(job.book.authors.values_list("full_name", flat=True)) == ["Jane Doe"]
    assert job.evidence[intake.BOOK_AUTHORS_SOURCE_KEY] == {
        "source": "file",
        "authors": ["Jane Doe"],
    }


@pytest.mark.django_db(transaction=True)
def test_match_with_conflicting_authors_goes_to_review(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _upload(tmp_path, monkeypatch, authors=["Somebody Else"])

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book is None
    blocked = job.evidence[intake.AUTO_ACCEPT_BLOCKED_KEY]
    assert blocked["candidate_authors"] == ["Somebody Else"]
    assert blocked["file_authors"] == ["Jane Doe"]
    task = ReviewTask.objects.get(import_job=job)
    assert task.embedded_metadata[intake.AUTO_ACCEPT_BLOCKED_KEY]["score"] == 1.0


@pytest.mark.django_db(transaction=True)
def test_match_with_matching_authors_is_still_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job = _upload(tmp_path, monkeypatch, authors=["Jane Doe"])

    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert list(job.book.authors.values_list("full_name", flat=True)) == ["Jane Doe"]
    assert intake.BOOK_AUTHORS_SOURCE_KEY not in job.evidence
    assert intake.AUTO_ACCEPT_BLOCKED_KEY not in job.evidence
