"""DNB and Google Books providers, provider records, and file details on review pages."""

from __future__ import annotations

import json
from email.message import Message
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError

import pytest
from django.test import Client, override_settings

from ebook_server.core import identification, intake
from ebook_server.core.metadata_providers import (
    DnbMetadataProvider,
    GoogleBooksMetadataProvider,
    clean_dnb_title,
)
from ebook_server.core.models import (
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    SourceLocation,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

DNB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/"><version>1.1</version>
<numberOfRecords>1</numberOfRecords><records><record><recordSchema>oai_dc</recordSchema>
<recordData><dc xmlns="http://www.openarchives.org/OAI/2.0/oai_dc/"
 xmlns:dc="http://purl.org/dc/elements/1.1/"
 xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
  <dc:title>[Ende] ; Die unendliche Geschichte : Roman / Michael Ende ; mit Bildern</dc:title>
  <dc:creator>Ende, Michael [Verfasser]</dc:creator>
  <dc:creator>Quadflieg, Roswitha [Illustrator]</dc:creator>
  <dc:publisher>Stuttgart : Thienemann</dc:publisher>
  <dc:date>1979</dc:date>
  <dc:language>ger</dc:language>
  <dc:identifier xsi:type="tel:ISBN">3-522-12800-1</dc:identifier>
  <dc:identifier xsi:type="dnb:IDN">790499312</dc:identifier>
</dc></recordData><recordPosition>1</recordPosition></record></records>
</searchRetrieveResponse>"""

EMPTY_DNB = (
    '<searchRetrieveResponse xmlns="http://www.loc.gov/zing/srw/">'
    "<numberOfRecords>0</numberOfRecords></searchRetrieveResponse>"
)


class _Raw:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Raw:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


def _opener(routes: dict[str, bytes | Exception], seen: list[str]) -> Any:
    def opener(request: Any, timeout: float = 0) -> _Raw:
        del timeout
        seen.append(request.full_url)
        for fragment, body in routes.items():
            if fragment in request.full_url:
                if isinstance(body, Exception):
                    raise body
                return _Raw(body)
        raise HTTPError(request.full_url, 404, "not found", Message(), None)

    return opener


def _signals(**overrides: Any) -> identification.IdentificationInput:
    values: dict[str, Any] = {
        "title_hints": [],
        "author_hints": [],
        "isbns": [],
        "folder_hints": [],
        "language": None,
        "publisher": None,
        "description": None,
    }
    values.update(overrides)
    return identification.IdentificationInput(**values)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            "[Ende] ; Die unendliche Geschichte : Roman / Michael Ende ; mit Bildern",
            ("Die unendliche Geschichte", "Roman"),
        ),
        ("Snow Crash : Roman / Neal Stephenson", ("Snow Crash", "Roman")),
        ("Momo / Michael Ende", ("Momo", "")),
        ("Plain title", ("Plain title", "")),
    ],
)
def test_clean_dnb_title(raw: str, expected: tuple[str, str]) -> None:
    assert clean_dnb_title(raw) == expected


def test_dnb_provider_maps_records_and_scores_like_other_providers() -> None:
    seen: list[str] = []
    with (
        patch(
            "ebook_server.core.identification.urlopen",
            _opener({"services.dnb.de": DNB_XML.encode()}, seen),
        ),
        patch(
            "ebook_server.core.identification._provider_instances", lambda: [DnbMetadataProvider()]
        ),
    ):
        candidates, outcomes = identification.resolve_candidates_with_report(
            _signals(
                title_hints=["Die unendliche Geschichte"],
                author_hints=["Michael Ende"],
                language="de",
            )
        )

    assert "tit%3D%22Die%20unendliche%20Geschichte%22%20and%20per%3D%22Michael%20Ende%22" in seen[0]
    top = candidates[0]
    assert top.provider_id == "dnb"
    assert top.title == "Die unendliche Geschichte"
    assert top.authors == ["Michael Ende"]  # illustrator dropped, name reordered
    assert top.publisher == "Thienemann"
    assert top.language == "de"
    assert top.isbn10 == "3522128001"
    assert top.publication_year == 1979
    assert top.raw["_record_url"] == "https://d-nb.info/790499312"
    assert top.score == pytest.approx(0.90)
    assert outcomes[0].label == "Deutsche Nationalbibliothek"
    assert outcomes[0].status == "results"
    assert outcomes[0].requests[0]["request"].startswith("search ")


def test_dnb_isbn_lookup_and_errors() -> None:
    seen: list[str] = []
    routes: dict[str, bytes | Exception] = {"num%3D": DNB_XML.encode()}
    with patch("ebook_server.core.identification.urlopen", _opener(routes, seen)):
        hits = DnbMetadataProvider().search("anything", isbn="3522128001")
    assert len(hits) == 1 and "num%3D3522128001" in seen[0]

    routes = {"services.dnb.de": HTTPError("u", 503, "down", Message(), None)}
    with (
        patch("ebook_server.core.identification.urlopen", _opener(routes, [])),
        patch(
            "ebook_server.core.identification._provider_instances", lambda: [DnbMetadataProvider()]
        ),
    ):
        _c, outcomes = identification.resolve_candidates_with_report(_signals(title_hints=["X"]))
    assert outcomes[0].status == "error"
    assert "HTTP 503" in outcomes[0].error

    with (
        patch(
            "ebook_server.core.identification.urlopen",
            _opener({"services.dnb.de": EMPTY_DNB.encode()}, []),
        ),
        patch(
            "ebook_server.core.identification._provider_instances", lambda: [DnbMetadataProvider()]
        ),
    ):
        _c, outcomes = identification.resolve_candidates_with_report(_signals(title_hints=["X"]))
    assert outcomes[0].status == "no_results"


@override_settings(
    BOOK_SCANNER_METADATA_PROVIDERS=[
        "ebook_server.core.identification.OpenLibraryMetadataProvider",
        "ebook_server.core.metadata_providers.DnbMetadataProvider",
        "ebook_server.core.metadata_providers.GoogleBooksMetadataProvider",
    ],
    BOOK_SCANNER_GOOGLE_BOOKS_API_KEY="",
)
def test_google_books_without_key_is_listed_as_unavailable() -> None:
    providers = identification.configured_providers()
    labels = [(p["label"], p["available"]) for p in providers]
    assert ("Open Library", True) in labels
    assert ("Deutsche Nationalbibliothek", True) in labels
    google = next(p for p in providers if "GoogleBooks" in p["label"])
    assert google["available"] is False
    assert "API key" in google["error"]


@override_settings(BOOK_SCANNER_GOOGLE_BOOKS_API_KEY="test-key")
def test_google_books_maps_volumes() -> None:
    body = json.dumps(
        {
            "items": [
                {
                    "id": "abc123",
                    "volumeInfo": {
                        "title": "Snow Crash",
                        "authors": ["Neal Stephenson"],
                        "publisher": "Bantam",
                        "publishedDate": "1993-05-01",
                        "language": "en",
                        "industryIdentifiers": [
                            {"type": "ISBN_13", "identifier": "9780553380958"},
                            {"type": "OTHER", "identifier": "x"},
                        ],
                        "infoLink": "https://books.google.com/books?id=abc123",
                    },
                }
            ]
        }
    ).encode()
    seen: list[str] = []
    with (
        patch("ebook_server.core.identification.urlopen", _opener({"googleapis.com": body}, seen)),
        patch(
            "ebook_server.core.identification._provider_instances",
            lambda: [GoogleBooksMetadataProvider()],
        ),
    ):
        candidates, outcomes = identification.resolve_candidates_with_report(
            _signals(title_hints=["Snow Crash"], author_hints=["Neal Stephenson"], language="en")
        )
    assert "key=test-key" in seen[0] and "langRestrict=en" in seen[0]
    assert candidates[0].title == "Snow Crash"
    assert candidates[0].isbn13 == "9780553380958"
    assert candidates[0].publication_year == 1993
    assert candidates[0].score == pytest.approx(0.90)
    assert outcomes[0].label == "Google Books"


@pytest.mark.django_db
def test_candidate_record_url() -> None:
    profile = get_or_create_profile("u")
    job = ImportJob.objects.create(
        source_type="upload", uploader=profile.user, original_filename="x.epub"
    )
    dnb = MetadataCandidate(
        import_job=job, provider_id="dnb", raw_payload={"_record_url": "https://d-nb.info/1"}
    )
    ol = MetadataCandidate(import_job=job, provider_id="openlibrary", provider_ref="/works/OL1W")
    unsafe = MetadataCandidate(
        import_job=job, provider_id="x", raw_payload={"_record_url": "javascript:x"}
    )
    assert dnb.record_url == "https://d-nb.info/1"
    assert ol.record_url == "https://openlibrary.org/works/OL1W"
    assert unsafe.record_url == ""


@pytest.mark.django_db(transaction=True)
def test_review_page_shows_file_name_path_source_and_siblings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage_root = tmp_path / "artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root)),
    )
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    monkeypatch.setattr(
        "ebook_server.core.identification.resolve_candidates_with_report",
        lambda signals: ([], []),
    )
    root = tmp_path / "member-share"
    folder = root / "Gibson, William"
    folder.mkdir(parents=True)
    (folder / "Neuromancer.txt").write_text("The sky above the port.")
    (folder / "Neuromancer.htm").write_text("<html><body>The sky.</body></html>")
    location = SourceLocation.objects.create(
        name="alice-share",
        root_path=str(root),
        path_layouts=["{author}/**"],
    )
    jobs = [
        intake.create_source_import_job(folder / name, location)
        for name in ("Neuromancer.txt", "Neuromancer.htm")
    ]
    for job in jobs:
        intake.process_import_job(str(job.id))
    task = ReviewTask.objects.get(import_job=jobs[0])
    admin = get_or_create_profile("admin-reviewer", role="admin")
    client = Client()
    client.force_login(admin.user)

    page = client.get(f"/reviews/{task.id}/", headers={"Accept": "text/html"}).content.decode()
    payload = client.get(f"/reviews/{task.id}/").json()
    queue = client.get("/reviews/", headers={"Accept": "text/html"}).content.decode()

    assert "Neuromancer.txt" in page
    assert "Gibson, William" in page
    assert "alice-share" in page
    assert str(folder / "Neuromancer.txt") in page
    assert "Neuromancer.htm" in page  # other format in the same folder
    assert "author William Gibson" in page
    assert payload["source"]["relative_path"] == "Gibson, William/Neuromancer.txt"
    assert payload["source"]["folder"] == "Gibson, William"
    assert payload["source"]["source_exists"] is True
    assert payload["source"]["siblings"][0]["filename"] == "Neuromancer.htm"
    assert str(folder / "Neuromancer.txt") in queue
