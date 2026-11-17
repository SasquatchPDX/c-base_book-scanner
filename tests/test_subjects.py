"""Subject/genre tags are captured as evidence (issue #50)."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from ebook_server.core import identification, intake
from ebook_server.core.models import (
    ImportJob,
    MetadataCandidate,
    ReviewTask,
    StorageLocation,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

DC = "http://purl.org/dc/elements/1.1/"


def _build_epub_with_subjects(path: Path, subjects: list[str]) -> None:
    subject_xml = "".join(f"<dc:subject>{subject}</dc:subject>" for subject in subjects)
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            f'<metadata xmlns:dc="{DC}"><dc:title>Tagged Book</dc:title>'
            f"<dc:creator>Jane Doe</dc:creator>{subject_xml}</metadata></package>",
        )


def _signals() -> identification.IdentificationInput:
    return identification.IdentificationInput(
        title_hints=["Tagged Book"],
        author_hints=["Jane Doe"],
        isbns=[],
        folder_hints=[],
        language=None,
        publisher=None,
        description=None,
    )


def test_normalize_subjects_trims_dedupes_and_bounds() -> None:
    raw: list[Any] = [
        "  Science   Fiction ",
        "science fiction",
        "",
        "Thriller",
        42,
        "x" * (identification.MAX_SUBJECT_LENGTH + 1),
    ]
    assert identification.normalize_subjects(raw) == ["Science Fiction", "Thriller"]
    assert identification.normalize_subjects("Single") == ["Single"]
    assert identification.normalize_subjects(None) == []
    many = [f"Tag {index}" for index in range(identification.MAX_SUBJECTS + 10)]
    assert len(identification.normalize_subjects(many)) == identification.MAX_SUBJECTS


def test_epub_dc_subjects_are_extracted_as_evidence(tmp_path: Path) -> None:
    epub_path = tmp_path / "tagged.epub"
    _build_epub_with_subjects(epub_path, ["Science Fiction", "Thriller", "science fiction"])

    metadata = intake.extract_metadata(str(epub_path), "epub", original_filename=epub_path.name)

    assert metadata["subjects"] == ["Science Fiction", "Thriller"]


def test_epub_without_subjects_reports_empty_list(tmp_path: Path) -> None:
    epub_path = tmp_path / "untagged.epub"
    _build_epub_with_subjects(epub_path, [])

    metadata = intake.extract_metadata(str(epub_path), "epub", original_filename=epub_path.name)

    assert metadata["subjects"] == []


def test_comicinfo_genre_is_extracted_as_evidence(tmp_path: Path) -> None:
    cbz_path = tmp_path / "comic.cbz"
    with ZipFile(cbz_path, "w") as archive:
        archive.writestr("001.jpg", b"page")
        archive.writestr(
            "ComicInfo.xml",
            "<ComicInfo><Title>Comic</Title><Genre>Superhero, Science Fiction,</Genre></ComicInfo>",
        )

    metadata = intake.extract_metadata(str(cbz_path), "cbz", original_filename=cbz_path.name)

    assert metadata["subjects"] == ["Superhero", "Science Fiction"]


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"subject": ["Fiction", "Space opera"]}, ["Fiction", "Space opera"]),
        ({"subjects": ["History"]}, ["History"]),
        ({"categories": ["Fiction / Science Fiction"]}, ["Fiction / Science Fiction"]),
        ({}, []),
    ],
)
def test_provider_subject_shapes_are_normalized_onto_candidates(
    payload: dict[str, Any],
    expected: list[str],
) -> None:
    candidate = identification._normalize_candidate(
        "stub",
        {"title": "Tagged Book", "author_name": ["Jane Doe"], **payload},
        _signals(),
    )

    assert candidate.subjects == expected


@pytest.mark.django_db(transaction=True)
def test_subjects_flow_to_job_evidence_candidates_and_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
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
                    "title": "Tagged Book",
                    "author_name": ["Someone Else"],
                    "subject": ["Space opera", "Fiction"],
                }
            ]

    monkeypatch.setattr(identification, "_provider_instances", lambda: [StubProvider()])
    monkeypatch.setattr(intake, "AUTO_ACCEPT_SCORE", 0.99)
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    monkeypatch.setattr(intake, "_default_storage", lambda: storage)
    profile = get_or_create_profile("subjects-user")
    client = Client()
    client.force_login(profile.user)
    epub_path = tmp_path / "tagged.epub"
    _build_epub_with_subjects(epub_path, ["Science Fiction"])

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                epub_path.name,
                epub_path.read_bytes(),
                content_type="application/epub+zip",
            )
        },
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    assert job.evidence["subjects"] == ["Science Fiction"]
    candidate = MetadataCandidate.objects.get(import_job=job)
    assert candidate.subjects == ["Space opera", "Fiction"]
    task = ReviewTask.objects.get(import_job=job)
    assert task.embedded_metadata["subjects"] == ["Science Fiction"]

    detail_json = client.get(f"/reviews/{task.id}/", HTTP_ACCEPT="application/json")
    assert detail_json.json()["candidates"][0]["subjects"] == ["Space opera", "Fiction"]
    detail_html = client.get(f"/reviews/{task.id}/", HTTP_ACCEPT="text/html")
    assert "Subjects: Space opera, Fiction" in detail_html.content.decode()
