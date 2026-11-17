"""ISBN-matched imports and authors (#42).

A high score (e.g. an exact ISBN match) is not evidence about authorship. When the
matched provider record names no authors, or names other authors than the file's own
metadata, the import goes to review instead of being accepted automatically, and the
file's authors are only offered to the reviewer as suggestions.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core import identification, intake
from ebook_server.core.models import (
    Author,
    Book,
    Identifier,
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

ISBN13 = "9780306406157"


# --- Open Library: edition without authors falls back to the work record ---------


class _FakeOpenLibrary(identification.OpenLibraryMetadataProvider):
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.requested: list[str] = []

    def _get_json(self, url: str) -> Any:
        self.requested.append(url)
        if url not in self.responses:
            raise OSError("unexpected url")
        return self.responses[url]


def test_edition_without_authors_uses_work_authors() -> None:
    provider = _FakeOpenLibrary(
        {
            f"https://openlibrary.org/isbn/{ISBN13}.json": {
                "title": "Phylogenesis",
                "works": [{"key": "/works/OL102541W"}],
            },
            "https://openlibrary.org/works/OL102541W.json": {
                "authors": [{"author": {"key": "/authors/OL29487A"}, "type": {"key": "x"}}],
            },
            "https://openlibrary.org/authors/OL29487A.json": {"name": "Alan Dean Foster"},
        }
    )

    payload = provider._query_isbn(ISBN13)

    assert payload is not None
    assert payload["author_name"] == ["Alan Dean Foster"]


def test_edition_authors_win_over_work_authors() -> None:
    provider = _FakeOpenLibrary(
        {
            f"https://openlibrary.org/isbn/{ISBN13}.json": {
                "title": "Phylogenesis",
                "authors": [{"key": "/authors/OL1A"}],
                "works": [{"key": "/works/OL102541W"}],
            },
            "https://openlibrary.org/authors/OL1A.json": {"name": "Edition Author"},
        }
    )

    payload = provider._query_isbn(ISBN13)

    assert payload is not None
    assert payload["author_name"] == ["Edition Author"]
    assert not any("/works/" in url for url in provider.requested)


def test_work_lookup_failure_leaves_candidate_without_authors() -> None:
    provider = _FakeOpenLibrary(
        {
            f"https://openlibrary.org/isbn/{ISBN13}.json": {
                "title": "Phylogenesis",
                "works": [{"key": "/works/OL102541W"}],
            },
        }
    )

    payload = provider._query_isbn(ISBN13)

    assert payload is not None
    assert "author_name" not in payload


def test_author_keys_accept_edition_and_work_shapes_and_reject_junk() -> None:
    keys = identification._open_library_author_keys(
        [
            {"key": "/authors/OL1A"},
            {"author": {"key": "/authors/OL2A"}},
            {"key": "/authors/OL1A"},
            {"key": "../../etc/passwd"},
            "not-a-dict",
        ]
    )
    assert keys == ["/authors/OL1A", "/authors/OL2A"]


# --- File author evidence and the conflict check ----------------------------------


def test_evidence_authors_drop_placeholder_creators() -> None:
    metadata = {
        "authors": [
            "Microsoft Office User",
            "Owner",
            "Unknown Author",
            "Administrator",
            "user",
            "  Alan   Dean Foster ",
            "alan dean foster",
        ]
    }
    assert identification.evidence_author_names(metadata) == ["Alan Dean Foster"]
    assert identification.evidence_author_names({}) == []


@pytest.mark.parametrize(
    ("candidate", "evidence"),
    [
        (["Iain M. Banks"], ["Banks, Iain"]),
        (["H. P. Lovecraft"], ["HP Lovecraft"]),
        (["J. R. R. Tolkien"], ["Tolkien"]),
        (["Someone Else", "Alan Dean Foster"], ["Alan Dean Foster"]),
        ([], ["Alan Dean Foster"]),
        (["Alan Dean Foster"], []),
    ],
)
def test_authors_do_not_conflict(candidate: list[str], evidence: list[str]) -> None:
    assert not identification.authors_conflict(candidate, evidence)


@pytest.mark.parametrize(
    ("candidate", "evidence"),
    [
        (["Brian Herbert"], ["Frank Herbert"]),
        (["Kevin J. Anderson"], ["Eric Flint"]),
        (["Alan Dean Foster"], ["jsmith"]),
    ],
)
def test_authors_conflict(candidate: list[str], evidence: list[str]) -> None:
    assert identification.authors_conflict(candidate, evidence)


# --- The auto-accept decision -----------------------------------------------------


def _candidate(*, authors: list[str], score: float = 1.0) -> identification.IdentificationCandidate:
    return identification.IdentificationCandidate(
        provider_id="stub",
        provider_ref="ref-1",
        title="Phylogenesis",
        authors=authors,
        isbn10=None,
        isbn13=ISBN13,
        language=None,
        description=None,
        publisher=None,
        edition=None,
        publication_year=None,
        score=score,
        raw={},
    )


# Normalised file signals without title evidence: only the author checks apply.
_NO_EVIDENCE = identification.IdentificationInput(
    title_hints=[],
    author_hints=[],
    isbns=[],
    folder_hints=[],
    language=None,
    publisher=None,
    description=None,
)


def test_decision_accepts_matching_authors() -> None:
    top = _candidate(authors=["Alan Dean Foster"])
    decision = intake.decide_auto_accept([top], ["Foster, Alan Dean"], file_evidence=_NO_EVIDENCE)
    assert decision.candidate is top
    assert decision.blocked_reason is None


def test_decision_below_threshold_is_plain_review() -> None:
    decision = intake.decide_auto_accept(
        [_candidate(authors=[], score=0.2)], ["A B"], file_evidence=_NO_EVIDENCE
    )
    assert decision.candidate is None
    assert decision.blocked_reason is None
    assert intake.decide_auto_accept([], [], file_evidence=_NO_EVIDENCE).candidate is None


@pytest.mark.parametrize(
    "authors", [[], ["Unknown"], ["Microsoft Office User"], ["Multiple Authors"]]
)
def test_decision_blocks_candidate_without_provider_authors(authors: list[str]) -> None:
    decision = intake.decide_auto_accept(
        [_candidate(authors=authors)], ["Alan Dean Foster"], file_evidence=_NO_EVIDENCE
    )
    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_NO_PROVIDER_AUTHORS
    assert decision.details["file_authors"] == ["Alan Dean Foster"]
    assert decision.details["score"] == 1.0


def test_decision_blocks_without_provider_authors_even_when_file_has_none() -> None:
    decision = intake.decide_auto_accept([_candidate(authors=[])], [], file_evidence=_NO_EVIDENCE)
    assert decision.blocked_reason == intake.BLOCK_NO_PROVIDER_AUTHORS


def test_decision_blocks_conflicting_authors() -> None:
    decision = intake.decide_auto_accept(
        [_candidate(authors=["Brian Herbert"])], ["Frank Herbert"], file_evidence=_NO_EVIDENCE
    )
    assert decision.candidate is None
    assert decision.blocked_reason == intake.BLOCK_AUTHOR_CONFLICT
    assert decision.details["candidate_authors"] == ["Brian Herbert"]


# --- End to end through upload intake and the review page -------------------------


def _build_epub(path: Path, *, creator: str, isbn: str = ISBN13) -> None:
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?>'
            '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:opf="http://www.idpf.org/2007/opf">'
            "<dc:title>Phylogenesis</dc:title>"
            f"<dc:creator>{creator}</dc:creator>"
            f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>'
            "</metadata></package>",
        )


def _stub_provider(monkeypatch: pytest.MonkeyPatch, *, authors: list[str]) -> None:
    class StubProvider:
        provider_id = "stub"

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
                    "key": "/books/OL24282198M",
                    "title": "Phylogenesis",
                    "author_name": authors,
                    "isbn_13": [ISBN13],
                }
            ]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.86)


def _upload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    client: Client,
    *,
    creator: str = "Alan Dean Foster",
    username: str = "isbn-author-user",
) -> ImportJob:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage_root = tmp_path / "storage"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root / "library-artifacts")),
    )
    profile = get_or_create_profile(username)
    client.force_login(profile.user)
    epub_path = tmp_path / "phylogenesis.epub"
    _build_epub(epub_path, creator=creator)
    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "Phylogenesis.epub",
                epub_path.read_bytes(),
                content_type="application/epub+zip",
            )
        },
    )
    assert response.status_code == 201
    return ImportJob.objects.get(pk=response.json()["job_id"])


@pytest.mark.django_db(transaction=True)
def test_isbn_match_without_provider_authors_goes_to_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, authors=[])
    job = _upload(monkeypatch, tmp_path, Client())

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    assert job.evidence[intake.AUTO_ACCEPT_BLOCKED_KEY] == intake.BLOCK_NO_PROVIDER_AUTHORS
    assert job.evidence[intake.SUGGESTED_AUTHORS_KEY] == ["Alan Dean Foster"]
    assert job.evidence[intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY]["score"] >= 0.86
    task = ReviewTask.objects.get(import_job=job)
    assert task.state == ReviewTask.State.OPEN
    assert task.embedded_metadata[intake.AUTO_ACCEPT_BLOCKED_KEY] == "no_provider_authors"
    # A blocked candidate is not presented as a MATCH.
    states = set(job.metadata_candidates.values_list("state", flat=True))
    assert states == {MetadataCandidate.CandidateState.REVIEW}
    # The file's author was not written anywhere as a canonical author.
    assert not Book.objects.exists()
    assert not Author.objects.filter(full_name="Alan Dean Foster").exists()


@pytest.mark.django_db(transaction=True)
def test_isbn_match_does_not_fill_existing_book_authors_from_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = Book.objects.create(title="Phylogenesis")
    Identifier.objects.create(
        book=existing,
        kind=Identifier.IdentifierType.ISBN13,
        value=ISBN13,
        normalized_value=ISBN13,
    )
    _stub_provider(monkeypatch, authors=[])
    job = _upload(monkeypatch, tmp_path, Client())

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.book_id is None
    existing.refresh_from_db()
    assert not existing.authors.exists()
    assert not existing.file_variants.exists()


@pytest.mark.django_db(transaction=True)
def test_isbn_match_with_conflicting_authors_goes_to_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, authors=["Brian Herbert"])
    job = _upload(monkeypatch, tmp_path, Client(), creator="Frank Herbert")

    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert job.evidence[intake.AUTO_ACCEPT_BLOCKED_KEY] == intake.BLOCK_AUTHOR_CONFLICT
    assert job.evidence[intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY]["candidate_authors"] == [
        "Brian Herbert"
    ]
    assert not Book.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_isbn_match_with_agreeing_authors_is_still_auto_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, authors=["Alan Dean Foster"])
    job = _upload(monkeypatch, tmp_path, Client(), creator="Foster, Alan Dean")

    assert job.state == ImportJob.State.COMPLETE
    assert intake.AUTO_ACCEPT_BLOCKED_KEY not in job.evidence
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Alan Dean Foster"]
    assert not ReviewTask.objects.filter(import_job=job).exists()


@pytest.mark.django_db(transaction=True)
def test_placeholder_creator_does_not_block_or_suggest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, authors=["Alan Dean Foster"])
    job = _upload(monkeypatch, tmp_path, Client(), creator="Microsoft Office User")

    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Alan Dean Foster"]


@pytest.mark.django_db(transaction=True)
def test_review_page_explains_block_and_accepts_with_suggested_authors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, authors=[])
    client = Client()
    job = _upload(monkeypatch, tmp_path, client)
    task = ReviewTask.objects.get(import_job=job)

    html = client.get(f"/reviews/{task.id}/?view=html").content.decode()
    assert 'data-block-reason="no_provider_authors"' in html
    assert "lists no authors" in html
    assert "Suggested authors from the file (unverified): Alan Dean Foster" in html
    assert 'name="authors" placeholder="Comma-separated" value="Alan Dean Foster"' in html

    detail = client.get(f"/reviews/{task.id}/").json()
    assert detail["auto_accept_block"]["reason"] == "no_provider_authors"
    assert detail["auto_accept_block"]["suggested_authors"] == ["Alan Dean Foster"]

    candidate = job.metadata_candidates.get()
    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": candidate.id, "authors": "Alan Dean Foster"},
    )
    assert response.status_code in {200, 302}

    job.refresh_from_db()
    task.refresh_from_db()
    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Alan Dean Foster"]
    assert task.decision_payload["reviewer_authors"] == ["Alan Dean Foster"]


@pytest.mark.django_db(transaction=True)
def test_reviewer_authors_never_override_provider_authors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_provider(monkeypatch, authors=["Brian Herbert"])
    client = Client()
    job = _upload(monkeypatch, tmp_path, client, creator="Frank Herbert")
    task = ReviewTask.objects.get(import_job=job)
    candidate = job.metadata_candidates.get()

    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": candidate.id, "authors": "Frank Herbert"},
    )
    assert response.status_code in {200, 302}

    job.refresh_from_db()
    assert job.book is not None
    assert [author.full_name for author in job.book.authors.all()] == ["Brian Herbert"]
