"""Scanner ``metadata.opf`` (#130, libreflip contract D3, §4.5, §6.8).

* ``PUT jobs/{id}/metadata``: versions, checksum, size, malformed and hostile XML;
* raw scans show the OPF and create no Book;
* the three finish linking paths (ISBN match, new Book, identification) and the
  duplicate/ambiguous routes to review;
* OPF updates after linking, hand-edited fields and their review task;
* the background provider check (owner decision on D3 vs AGENTS.md).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from typing import Any

import pytest
from django.test import Client

from ebook_server.core import identification, scan_identification, scan_pipeline, scanner_opf
from ebook_server.core.models import (
    Author,
    Book,
    BookMetadataProvenance,
    FileVariant,
    Identifier,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    ScanJobOpf,
    get_or_create_profile,
)
from ebook_server.core.scanner_auth import generate_key, hash_key
from tests.test_raw_scan_lifecycle import (
    JOB_ID,
    _add_page,
    _auth,
    _capture_complete,
    _manifest,
    _register,
    _status,
)

ISBN = "9780306406157"
OTHER_ISBN = "9780140449136"


def _opf(
    title: str | None = "The Physical Book",
    authors: tuple[str, ...] = ("Ada Scanner",),
    isbn: str | None = ISBN,
    language: str | None = "en",
    extra: str = "",
) -> bytes:
    parts = []
    if title is not None:
        parts.append(f"<dc:title>{title}</dc:title>")
    for author in authors:
        parts.append(f'<dc:creator opf:role="aut">{author}</dc:creator>')
    if isbn is not None:
        parts.append(f'<dc:identifier opf:scheme="ISBN">{isbn}</dc:identifier>')
    if language is not None:
        parts.append(f"<dc:language>{language}</dc:language>")
    body = "".join(parts) + extra
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
        'xmlns:opf="http://www.idpf.org/2007/opf">'
        f"{body}</metadata></package>"
    ).encode()


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _put_opf(key: str, payload: bytes, *, sha: str | None = None, job_id: str = JOB_ID) -> Any:
    from django.core.files.uploadedfile import SimpleUploadedFile
    from django.test.client import BOUNDARY, MULTIPART_CONTENT, encode_multipart

    body = encode_multipart(
        BOUNDARY,
        {
            "metadata": json.dumps({"sha256": sha or _sha(payload)}),
            "opf": SimpleUploadedFile(
                "metadata.opf", payload, content_type="application/oebps-package+xml"
            ),
        },
    )
    return Client().put(
        f"/api/scanner/v1/jobs/{job_id}/metadata",
        data=body,
        content_type=MULTIPART_CONTENT,
        headers=_auth(key),
    )


class _StubProvider:
    provider_id = "opf-stub"

    def __init__(self, records: list[dict[str, Any]]) -> None:
        self.records = records

    def search(self, title: str | None = None, **_kwargs: Any) -> list[dict[str, Any]]:
        return list(self.records)


@pytest.fixture(autouse=True)
def jobs_run_inline(settings: Any) -> None:
    """Background jobs run in-process, also in the infrastructure lane (which sets a
    Redis queue URL); tests that need a held-back worker patch ``get_queue`` themselves."""
    settings.BOOK_SCANNER_QUEUE_URL = ""


@pytest.fixture(autouse=True)
def providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, Any]]]:
    """Provider records every lookup returns; empty by default (no network in tests)."""
    records: list[dict[str, Any]] = []
    monkeypatch.setattr(identification, "_provider_instances", lambda: [_StubProvider(records)])
    yield records


@pytest.fixture
def key(db: None, settings: Any) -> str:
    """The scanner API key, configured as its SHA-256 (one scanner, one key)."""
    value = generate_key()
    settings.BOOK_SCANNER_SCANNER_API_KEY_SHA256 = hash_key(value)
    return value


@pytest.fixture
def admin_client(db: None) -> Client:
    profile = get_or_create_profile("opf-admin")
    profile.role = "admin"
    profile.save()
    client = Client()
    client.force_login(profile.user)
    return client


def _finish(client: Client) -> Any:
    return client.post(f"/scan/jobs/{JOB_ID}/finish/", data={"allow_raw_pages": "true"})


def _revision(client: Client, task: ReviewTask) -> str:
    """The evidence revision the review page shows (sent back with the decision)."""
    revision = client.get(f"/reviews/{task.pk}/").json()["scanner_opf"]["revision"]
    assert isinstance(revision, str) and revision
    return revision


def _resolve(client: Client, task: ReviewTask, choice: str, revision: str | None = None) -> Any:
    return client.post(
        f"/reviews/{task.pk}/scanner-opf/",
        {
            "action": "resolve",
            "choice": choice,
            "revision": revision if revision is not None else _revision(client, task),
        },
    )


# --- PUT jobs/{id}/metadata (§6.8) -------------------------------------------------------


@pytest.mark.django_db
def test_upload_is_versioned_and_idempotent(key: str) -> None:
    _register(key)
    first = _opf()

    created = _put_opf(key, first)
    assert created.status_code == 201, created.content
    assert created.json()["metadata_opf"] == {"sha256": _sha(first), "version": 1}
    assert created.json()["metadata"]["title"] == "The Physical Book"
    assert created.json()["metadata"]["isbn"] == ISBN

    repeated = _put_opf(key, first)
    assert repeated.status_code == 200
    assert ScanJobOpf.objects.count() == 1

    second = _opf(title="The Physical Book, Revised")
    assert _put_opf(key, second).status_code == 201
    assert _status(key).json()["metadata_opf"] == {"sha256": _sha(second), "version": 2}
    versions = list(ScanJobOpf.objects.order_by("version"))
    assert [v.version for v in versions] == [1, 2]
    assert bytes(versions[0].content) == first  # earlier versions are kept
    assert versions[1].source == "scanner"
    # A raw scan creates no Book (D6).
    assert Book.objects.count() == 0
    assert ScanJob.objects.get(pk=JOB_ID).provisional_title == "The Physical Book, Revised"


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"not xml at all", "invalid_opf"),
        (b"<package xmlns='http://www.idpf.org/2007/opf'/>", "invalid_opf"),
        (b"<html><metadata/></html>", "invalid_opf"),
        (
            b'<?xml version="1.0"?><!DOCTYPE package [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
            b"<package><metadata><title>&xxe;</title></metadata></package>",
            "invalid_opf",
        ),
        (
            b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">'
            b'<!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">'
            b'<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>'
            b"<package><metadata><title>&lol3;</title></metadata></package>",
            "invalid_opf",
        ),
    ],
    ids=["not-xml", "no-metadata", "not-package", "xxe", "billion-laughs"],
)
def test_malformed_or_hostile_opf_is_rejected(key: str, payload: bytes, code: str) -> None:
    _register(key)
    response = _put_opf(key, payload)
    assert response.status_code == 422, response.content
    assert response.json()["error"]["code"] == code
    assert ScanJobOpf.objects.count() == 0
    assert b"passwd" not in response.content and b"lollol" not in response.content


@pytest.mark.django_db
def test_checksum_size_state_and_ownership(key: str, settings: Any) -> None:
    _register(key)
    mismatch = _put_opf(key, _opf(), sha="0" * 64)
    assert mismatch.status_code == 422
    assert mismatch.json()["error"]["code"] == "checksum_mismatch"

    settings.BOOK_SCANNER_SCANNER_MAX_OPF_BYTES = 100
    too_large = _put_opf(key, _opf())
    assert too_large.status_code == 413
    assert too_large.json()["error"]["code"] == "payload_too_large"
    settings.BOOK_SCANNER_SCANNER_MAX_OPF_BYTES = 1024 * 1024

    missing = Client().put(
        f"/api/scanner/v1/jobs/{JOB_ID}/metadata",
        data=b"{}",
        content_type="application/json",
        headers=_auth(key),
    )
    assert missing.status_code == 400

    web_job = ScanJob.objects.create(id="20261117T150000Z", source=ScanJob.Source.WEB)
    assert _put_opf(key, _opf(), job_id=web_job.pk).status_code == 404

    ScanJob.objects.filter(pk=JOB_ID).update(state=ScanJob.State.CANCELLED)
    cancelled = _put_opf(key, _opf())
    assert cancelled.status_code == 409
    assert cancelled.json()["error"]["code"] == "job_state"


@pytest.mark.django_db
def test_capture_complete_checks_the_opf_checksum(key: str) -> None:
    _register(key)
    payload = _opf()
    missing = _capture_complete(key, _manifest({}, metadata_opf={"sha256": _sha(payload)}))
    assert missing.status_code == 409
    assert missing.json()["error"]["details"]["missing"] == ["metadata_opf"]

    assert _put_opf(key, payload).status_code == 201
    wrong = _capture_complete(key, _manifest({}, metadata_opf={"sha256": "b" * 64}))
    assert wrong.json()["error"]["details"]["mismatched"] == ["metadata_opf"]
    assert (
        _capture_complete(key, _manifest({}, metadata_opf={"sha256": _sha(payload)})).status_code
        == 200
    )


@pytest.mark.django_db
def test_raw_scan_shows_opf_on_job_page_and_raw_scans(key: str, admin_client: Client) -> None:
    _register(key)
    assert _put_opf(key, _opf(language="ger")).status_code == 201
    assert _capture_complete(key, _manifest({})).status_code == 200

    page = admin_client.get(f"/scan/jobs/{JOB_ID}/workspace/?view=html").content.decode()
    assert "The Physical Book" in page
    assert "Ada Scanner" in page
    assert ISBN in page
    assert "data-opf-language>de<" in page

    item = admin_client.get("/scan/raw/").json()["items"][0]
    assert item["title"] == "The Physical Book"
    assert item["metadata"]["authors"] == ["Ada Scanner"]
    assert item["metadata"]["isbn"] == ISBN
    assert item["metadata"]["language"] == "de"
    assert ISBN in admin_client.get("/scan/raw/?view=html").content.decode()
    assert Book.objects.count() == 0


@pytest.mark.django_db
def test_app_generated_opf_is_stored_but_not_used(key: str) -> None:
    _register(key)
    _put_opf(key, _opf())
    generated = _opf(
        title="Our Own Output",
        extra='<meta name="ebook-server:generated" content="true"/>',
    )
    assert _put_opf(key, generated).status_code == 201
    job = ScanJob.objects.get(pk=JOB_ID)
    assert scanner_opf.current_opf(job).app_generated is True  # type: ignore[union-attr]
    assert scanner_opf.effective_opf(job).version == 1  # type: ignore[union-attr]
    assert scanner_opf.display_metadata(job)["title"] == "The Physical Book"  # type: ignore[index]


# --- finish: the three linking paths (§4.5) -----------------------------------------------


def _prepare_finish(key: str, member: Client, payload: bytes | None) -> None:
    _register(key)
    _add_page(member, JOB_ID, 1, b"one")
    if payload is not None:
        assert _put_opf(key, payload).status_code == 201


@pytest.mark.django_db(transaction=True)
def test_finish_links_the_one_book_with_the_isbn(
    key: str, admin_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    existing = Book.objects.create(title="The Physical Book")
    Identifier.objects.create(book=existing, kind="isbn13", value=ISBN, normalized_value=ISBN)
    _prepare_finish(key, admin_client, _opf())
    assert Book.objects.count() == 1  # still raw: nothing created

    response = _finish(admin_client)

    assert response.status_code == 202, response.content
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.book_id == existing.pk
    assert job.state == ScanJob.State.COMPLETE
    assert Book.objects.count() == 1
    assert FileVariant.objects.filter(book=existing).exists()  # a new variant ("Book != File")
    existing.refresh_from_db()
    assert existing.language == "en"  # an empty field is filled from the OPF
    assert job.notes["book_link"]["path"] == "isbn_match"


@pytest.mark.django_db(transaction=True)
def test_finish_creates_a_book_from_the_opf(
    key: str, admin_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    _prepare_finish(key, admin_client, _opf())

    response = _finish(admin_client)

    assert response.status_code == 202, response.content
    assert "requires_bibliographic_review" not in response.json()
    book = Book.objects.get()
    assert book.title == "The Physical Book"
    assert [a.full_name for a in book.authors.all()] == ["Ada Scanner"]
    assert book.language == "en"
    assert set(book.identifiers.values_list("value", flat=True)) == {ISBN, "0306406152"}
    sources = set(
        BookMetadataProvenance.objects.filter(book=book, action="set").values_list(
            "field", "source"
        )
    )
    assert sources == {
        ("title", "scanner_opf"),
        ("authors", "scanner_opf"),
        ("isbn", "scanner_opf"),
        ("language", "scanner_opf"),
    }
    # The external check ran in the background and found nothing: scanner data stays.
    check = ProcessingJob.objects.get(job_type=ProcessingJob.JobType.SCANNER_OPF_CHECK)
    assert check.state == ProcessingJob.JobState.SUCCESS
    assert check.details["result"]["status"] == scanner_opf.CHECK_NO_RESULTS
    assert BookMetadataProvenance.objects.filter(
        book=book, action="provider_check", value="no_results"
    ).exists()
    assert not ReviewTask.objects.exists()


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("payload", [None, _opf(title=None)], ids=["no-opf", "no-title"])
def test_finish_without_opf_title_runs_scan_identification(
    key: str,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | None,
) -> None:
    _prepare_finish(key, admin_client, payload)
    calls: list[str] = []

    def identify(*, scan_job: ScanJob, requested_by_user_id: int | None) -> Any:
        calls.append(scan_job.pk)
        raise scan_identification.ScanIdentificationError("identification queued (stub)")

    monkeypatch.setattr(scan_identification, "request_scan_identification", identify)
    response = _finish(admin_client)

    assert calls == [JOB_ID]
    assert response.status_code == 400
    assert Book.objects.count() == 0
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.book_id is None
    if payload is not None:
        assert job.notes["scanner_opf"]["reason"] == "no_title"
        assert job.provisional_authors == ["Ada Scanner"]


@pytest.mark.django_db(transaction=True)
def test_isbn_less_duplicate_routes_to_review_and_can_create_a_book(
    key: str, admin_client: Client
) -> None:
    existing = Book.objects.create(title="The Physical Book")
    existing.authors.add(Author.objects.create(full_name="Ada Scanner"))
    _prepare_finish(key, admin_client, _opf(isbn=None))

    response = _finish(admin_client)

    assert response.status_code == 202, response.content
    assert response.json()["requires_bibliographic_review"] is True
    assert Book.objects.count() == 1  # no silent duplicate
    job = ScanJob.objects.get(pk=JOB_ID)
    assert job.notes["scanner_opf"]["reason"] == "possible_duplicate"
    assert job.notes["scanner_opf"]["possible_duplicate_book_ids"] == [existing.pk]
    task = ReviewTask.objects.get(scan_job=job)
    assert task.embedded_metadata["scanner_opf"]["reason"] == "possible_duplicate"

    detail = admin_client.get(f"/reviews/{task.pk}/")
    assert detail.json()["scanner_opf"]["possible_duplicate_book_ids"] == [existing.pk]
    html = admin_client.get(f"/reviews/{task.pk}/?view=html").content.decode()
    assert "Create a new book from the scanner metadata" in html
    assert f'data-possible-duplicate="{existing.pk}"' in html

    created = admin_client.post(f"/reviews/{task.pk}/scanner-opf/", {"action": "create_book"})
    assert created.status_code == 200, created.content
    book = Book.objects.exclude(pk=existing.pk).get()
    assert ScanJob.objects.get(pk=JOB_ID).book_id == book.pk
    assert BookMetadataProvenance.objects.filter(book=book, source="scanner_opf").exists()
    assert ReviewTask.objects.get(pk=task.pk).state == ReviewTask.State.RESOLVED


@pytest.mark.django_db(transaction=True)
def test_isbn_on_several_books_routes_to_review(key: str, admin_client: Client) -> None:
    for title in ("Copy A", "Copy B"):
        book = Book.objects.create(title=title)
        Identifier.objects.create(book=book, kind="isbn13", value=ISBN, normalized_value=ISBN)
    _prepare_finish(key, admin_client, _opf())

    assert _finish(admin_client).json()["requires_bibliographic_review"] is True
    assert Book.objects.count() == 2
    assert ScanJob.objects.get(pk=JOB_ID).notes["scanner_opf"]["reason"] == "ambiguous_isbn"


# --- OPF after linking ---------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_opf_after_linking_updates_scanner_fields_but_not_hand_edits(
    key: str, admin_client: Client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    _prepare_finish(key, admin_client, _opf())
    assert _finish(admin_client).status_code == 202
    book = Book.objects.get()

    # A reviewer corrects the language by hand (the admin records this).
    before = scanner_opf.snapshot_book(book)
    book.language = "fr"
    book.save()
    scanner_opf.record_reviewer_edits(book, before, user=None)

    newer = _opf(title="The Physical Book, Second Edition", language="de")
    assert _put_opf(key, newer).status_code == 201

    book.refresh_from_db()
    assert book.title == "The Physical Book, Second Edition"  # scanner-owned: updated
    assert book.language == "fr"  # hand-edited: kept
    history = list(
        BookMetadataProvenance.objects.filter(book=book, field="title", action="set").order_by("id")
    )
    assert [row.value for row in history] == [
        "The Physical Book",
        "The Physical Book, Second Edition",
    ]
    assert history[-1].previous_value == "The Physical Book"
    task = ReviewTask.objects.get(scan_job_id=JOB_ID, state=ReviewTask.State.OPEN)
    review = task.decision_payload["scanner_opf"]
    assert review["kind"] == scanner_opf.KIND_FIELD_CONFLICT
    assert review["preselected"] == "current"
    assert [c["field"] for c in review["conflicts"]] == ["language"]
    html = admin_client.get(f"/reviews/{task.pk}/?view=html").content.decode()
    assert 'data-scanner-opf-review="field_conflict"' in html
    assert 'value="current" checked' in html

    assert (
        admin_client.post(
            f"/reviews/{task.pk}/scanner-opf/", {"action": "resolve", "choice": "scanner"}
        ).status_code
        == 400  # the evidence revision the reviewer saw is required
    )
    resolved = _resolve(admin_client, task, "scanner")
    assert resolved.status_code == 200, resolved.content
    book.refresh_from_db()
    assert book.language == "de"
    assert ReviewTask.objects.get(pk=task.pk).state == ReviewTask.State.RESOLVED


# --- external provider check --------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_provider_confirmation_keeps_scanner_metadata(
    key: str,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    providers: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    providers.append(
        {
            "key": "/works/OL1W",
            "title": "The Physical Book: A Novel",
            "author_name": ["Ada Scanner"],
            "isbn": [ISBN],
        }
    )
    _prepare_finish(key, admin_client, _opf())
    assert _finish(admin_client).status_code == 202

    opf = ScanJobOpf.objects.get()
    assert opf.provider_check["status"] == scanner_opf.CHECK_CONFIRMED
    assert opf.provider_check["basis"] == "isbn_record"
    assert Book.objects.get().title == "The Physical Book"
    assert not ReviewTask.objects.exists()


@pytest.mark.django_db(transaction=True)
def test_provider_contradiction_opens_review_with_scanner_preselected(
    key: str,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    providers: list[dict[str, Any]],
) -> None:
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    providers.append(
        {
            "key": "/works/OL2W",
            "title": "A Completely Different Book",
            "author_name": ["Someone Else"],
            "isbn": [ISBN],
        }
    )
    _prepare_finish(key, admin_client, _opf())
    assert _finish(admin_client).status_code == 202

    book = Book.objects.get()
    assert book.title == "The Physical Book"  # never overwritten automatically
    task = ReviewTask.objects.get(scan_job_id=JOB_ID)
    review = task.decision_payload["scanner_opf"]
    assert review["kind"] == scanner_opf.KIND_PROVIDER_CONFLICT
    assert review["preselected"] == "scanner"
    assert review["provider"]["title"] == "A Completely Different Book"
    assert ScanJobOpf.objects.get().provider_check["status"] == scanner_opf.CHECK_CONTRADICTED
    html = admin_client.get(f"/reviews/{task.pk}/?view=html").content.decode()
    assert 'value="scanner" checked' in html
    assert "A Completely Different Book" in html

    assert _resolve(admin_client, task, "bogus").status_code == 400
    resolved = _resolve(admin_client, task, "provider")
    assert resolved.status_code == 200, resolved.content
    book.refresh_from_db()
    assert book.title == "A Completely Different Book"
    assert [a.full_name for a in book.authors.all()] == ["Someone Else"]
    # A reviewer's choice is now protected from later scanner OPFs.
    assert scanner_opf.latest_set(book, "title").source == "reviewer"  # type: ignore[union-attr]
    assert _put_opf(key, _opf(title="The Physical Book (again)")).status_code == 201
    book.refresh_from_db()
    assert book.title == "A Completely Different Book"


def test_assess_ignores_weak_matches() -> None:
    weak = identification.IdentificationCandidate(
        provider_id="p",
        provider_ref="r",
        title="Unrelated",
        authors=["X"],
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
    result = scanner_opf.assess_provider_result(
        {"title": "The Physical Book", "authors": ["Ada Scanner"], "isbn": None}, [weak], []
    )
    assert result["status"] == scanner_opf.CHECK_INCONCLUSIVE


# --- out-of-order provider workers and stale reviews (PR #143 review) -----------------------

V1_TITLE = "The Physical Book"
V2_TITLE = "Another Physical Volume"


class _RecordingQueue:
    """Holds dispatched provider checks so a test decides when (and in what order) they run."""

    def __init__(self) -> None:
        self.job_ids: list[str] = []

    def enqueue(self, func: Any, job_id: str, **_kwargs: Any) -> None:
        self.job_ids.append(job_id)


def _provider_record(title: str, author: str) -> dict[str, Any]:
    return {"key": f"/works/{title}", "title": title, "author_name": [author], "isbn": [ISBN]}


def _check_job_for(version: int) -> ProcessingJob:
    return ProcessingJob.objects.get(
        job_type=ProcessingJob.JobType.SCANNER_OPF_CHECK, details__opf_version=version
    )


def _claim(task: ReviewTask) -> None:
    task.state = ReviewTask.State.CLAIMED
    task.claimed_by = get_or_create_profile("opf-admin").user
    task.save(update_fields=["state", "claimed_by", "updated_at"])


@pytest.fixture
def held_checks(monkeypatch: pytest.MonkeyPatch) -> _RecordingQueue:
    """Provider checks are queued, not run: the test runs them in the order it wants."""
    monkeypatch.setattr(scan_pipeline, "get_queue", lambda: None)
    recording = _RecordingQueue()
    monkeypatch.setattr(scanner_opf, "get_queue", lambda: recording)
    return recording


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("v1_lookup", ["before_v2", "during_v2"])
def test_delayed_v1_check_cannot_replace_claimed_v2_evidence(
    key: str,
    admin_client: Client,
    monkeypatch: pytest.MonkeyPatch,
    held_checks: _RecordingQueue,
    v1_lookup: str,
) -> None:
    """The reviewer's sequence: OPF v2 is current and its review claimed when v1's lookup
    completes. The v1 result is stale history; the claimed v2 evidence stays; resolving
    applies the v2 decision; the Book keeps v2's title."""
    v2_record = _provider_record("Provider Title For V2", "Provider Person")
    v1_record = _provider_record("Obsolete Candidate For V1", "Old Person")
    claimed: dict[str, Any] = {}

    def open_and_claim_v2_review() -> None:
        assert _put_opf(key, _opf(title=V2_TITLE)).status_code == 201
        scanner_opf.process_provider_check_job(str(_check_job_for(2).job_id))
        task = ReviewTask.objects.get(scan_job_id=JOB_ID)
        _claim(task)
        claimed["task"] = task
        claimed["payload"] = ReviewTask.objects.get(pk=task.pk).decision_payload
        claimed["revision"] = _revision(admin_client, task)

    v1_lookups: list[str] = []

    class _OrderedProvider:
        provider_id = "opf-stub"

        def search(self, title: str | None = None, **_kwargs: Any) -> list[dict[str, Any]]:
            if title == V2_TITLE:
                return [v2_record]
            v1_lookups.append(str(title))
            if v1_lookup == "during_v2":
                # The v1 worker is mid-lookup (no transaction, no locks) while v2 arrives,
                # is checked, contradicted and its review claimed.
                open_and_claim_v2_review()
            return [v1_record]

    monkeypatch.setattr(identification, "_provider_instances", lambda: [_OrderedProvider()])
    _prepare_finish(key, admin_client, _opf(title=V1_TITLE))
    assert _finish(admin_client).status_code == 202
    v1_job = _check_job_for(1)
    assert v1_job.state == ProcessingJob.JobState.QUEUED  # held back: the delayed worker
    if v1_lookup == "before_v2":
        open_and_claim_v2_review()

    scanner_opf.process_provider_check_job(str(v1_job.job_id))

    v1_job.refresh_from_db()
    assert v1_job.state == ProcessingJob.JobState.SUCCESS
    assert v1_job.details["published"] is False
    assert v1_job.details["stale"]["reason"] == "opf_superseded"
    assert v1_job.details["stale"]["effective_opf_version"] == 2
    if v1_lookup == "during_v2":
        assert v1_lookups == [V1_TITLE]  # the completion was fenced
        assert v1_job.details["result"]["record"]["title"] == "Obsolete Candidate For V1"
    else:
        assert v1_lookups == []  # fenced before calling the providers at all
    v1_opf = ScanJobOpf.objects.get(scan_job_id=JOB_ID, version=1)
    assert v1_opf.provider_check == {}
    assert not BookMetadataProvenance.objects.filter(action="provider_check", opf=v1_opf).exists()
    # The claimed v2 review is untouched and still the only one.
    task = ReviewTask.objects.get(scan_job_id=JOB_ID)
    assert task.pk == claimed["task"].pk
    assert task.state == ReviewTask.State.CLAIMED
    assert task.decision_payload == claimed["payload"]
    assert task.decision_payload["scanner_opf"]["provider"]["title"] == "Provider Title For V2"

    resolved = _resolve(admin_client, task, "scanner", claimed["revision"])
    assert resolved.status_code == 200, resolved.content
    book = Book.objects.get()
    assert book.title == V2_TITLE
    assert ReviewTask.objects.get(pk=task.pk).state == ReviewTask.State.RESOLVED
    effective = scanner_opf.effective_opf(ScanJob.objects.get(pk=JOB_ID))
    assert effective is not None and effective.version == 2


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("client_kind", ["api", "html"])
def test_resolving_a_review_of_a_superseded_opf_is_rejected(
    key: str,
    admin_client: Client,
    held_checks: _RecordingQueue,
    providers: list[dict[str, Any]],
    client_kind: str,
) -> None:
    """A claimed v1 review is not rewritten when v2 arrives, and deciding it afterwards
    does not write v1's provider record over v2: 409 / form error, review closed."""
    providers.append(_provider_record("Obsolete Candidate For V1", "Old Person"))
    _prepare_finish(key, admin_client, _opf(title=V1_TITLE))
    assert _finish(admin_client).status_code == 202
    scanner_opf.process_provider_check_job(str(_check_job_for(1).job_id))
    task = ReviewTask.objects.get(scan_job_id=JOB_ID)
    _claim(task)
    revision = _revision(admin_client, task)
    evidence = ReviewTask.objects.get(pk=task.pk).decision_payload["scanner_opf"]

    assert _put_opf(key, _opf(title=V2_TITLE)).status_code == 201
    book = Book.objects.get()
    assert book.title == V2_TITLE  # the scanner-owned title follows the newer OPF
    still = ReviewTask.objects.get(pk=task.pk)
    assert still.state == ReviewTask.State.CLAIMED  # claimed evidence is preserved
    assert still.decision_payload["scanner_opf"] == evidence
    html = admin_client.get(f"/reviews/{task.pk}/?view=html").content.decode()
    assert 'data-scanner-opf-obsolete="opf_superseded"' in html
    assert 'name="choice"' not in html

    data = {"action": "resolve", "choice": "provider", "revision": revision}
    url = f"/reviews/{task.pk}/scanner-opf/"
    if client_kind == "api":
        response = admin_client.post(url, data)
        assert response.status_code == 409, response.content
        body = response.json()
        assert "version 2" in body["error"]
        assert body["state"] == ReviewTask.State.CANCELLED
    else:
        response = admin_client.post(url, data, HTTP_ACCEPT="text/html", follow=True)
        assert response.status_code == 200
        page = response.content.decode()
        assert 'data-message-level="error"' in page
        assert "This review is obsolete" in page

    book.refresh_from_db()
    assert book.title == V2_TITLE
    assert [a.full_name for a in book.authors.all()] == ["Ada Scanner"]
    assert not BookMetadataProvenance.objects.filter(book=book, source="reviewer").exists()
    closed = ReviewTask.objects.get(pk=task.pk)
    assert closed.state == ReviewTask.State.CANCELLED
    review = closed.decision_payload["scanner_opf"]
    assert review["obsolete"]["reason"] == "opf_superseded"
    assert {k: v for k, v in review.items() if k != "obsolete"} == evidence


@pytest.mark.django_db(transaction=True)
def test_new_opf_supersedes_unclaimed_reviews_of_older_versions(
    key: str, admin_client: Client, held_checks: _RecordingQueue, providers: list[dict[str, Any]]
) -> None:
    providers.append(_provider_record("Obsolete Candidate For V1", "Old Person"))
    _prepare_finish(key, admin_client, _opf(title=V1_TITLE))
    assert _finish(admin_client).status_code == 202
    scanner_opf.process_provider_check_job(str(_check_job_for(1).job_id))
    task = ReviewTask.objects.get(scan_job_id=JOB_ID, state=ReviewTask.State.OPEN)

    assert _put_opf(key, _opf(title=V2_TITLE)).status_code == 201

    task.refresh_from_db()
    assert task.state == ReviewTask.State.CANCELLED
    assert task.decision_payload["scanner_opf"]["obsolete"]["effective_opf_version"] == 2


@pytest.mark.django_db(transaction=True)
def test_resolution_is_rejected_when_the_book_or_evidence_changed(
    key: str, admin_client: Client, held_checks: _RecordingQueue, providers: list[dict[str, Any]]
) -> None:
    providers.append(_provider_record("A Completely Different Book", "Someone Else"))
    _prepare_finish(key, admin_client, _opf())
    assert _finish(admin_client).status_code == 202
    scanner_opf.process_provider_check_job(str(_check_job_for(1).job_id))
    task = ReviewTask.objects.get(scan_job_id=JOB_ID)
    revision = _revision(admin_client, task)

    # An outdated or forged revision is rejected without touching anything.
    assert _resolve(admin_client, task, "provider", "0" * 32).status_code == 409
    assert Book.objects.get().title == "The Physical Book"

    # Someone edits the title by hand after the review was opened.
    book = Book.objects.get()
    before = scanner_opf.snapshot_book(book)
    book.title = "Hand-Corrected Title"
    book.save()
    scanner_opf.record_reviewer_edits(book, before, user=None)

    rejected = _resolve(admin_client, task, "provider", revision)
    assert rejected.status_code == 409, rejected.content
    assert "metadata changed" in rejected.json()["error"]
    book.refresh_from_db()
    assert book.title == "Hand-Corrected Title"  # the decision on old evidence is not applied
    assert ReviewTask.objects.get(pk=task.pk).state == ReviewTask.State.OPEN  # to decide again
    new_revision = rejected.json()["revision"]
    assert new_revision != revision
    assert new_revision == _revision(admin_client, task)

    accepted = _resolve(admin_client, task, "provider", new_revision)
    assert accepted.status_code == 200, accepted.content
    book.refresh_from_db()
    assert book.title == "A Completely Different Book"
