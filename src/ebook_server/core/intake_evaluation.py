"""Labelled, offline intake experiments. Never used by production intake."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from contextlib import ExitStack
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any, Literal
from unittest.mock import patch
from urllib.parse import quote
from xml.etree import ElementTree as ET
from zipfile import ZIP_STORED, ZipFile, ZipInfo

import isbnlib
from django.db import transaction
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from . import identification, intake, local_identity
from .languages import normalize_language
from .models import Identifier, ImportJob, MetadataCandidate, ReviewTask, SourceLocation


class LabelModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkLabel(LabelModel):
    title: str = Field(min_length=1, max_length=500)
    authors: list[str] = Field(min_length=1, max_length=10)


class EditionLabel(LabelModel):
    work: str
    isbn13: str | None = None
    publisher: str = ""
    language: str = ""
    year: int | None = None

    @field_validator("isbn13")
    @classmethod
    def valid_isbn(cls, value: str | None) -> str | None:
        if value is not None and not isbnlib.is_isbn13(value):
            raise ValueError("edition ISBN must be checksummed ISBN-13")
        return value


class FileLabel(LabelModel):
    path: str
    work: str
    edition: str | None = None
    require_review: bool = False
    work_only_language: str = ""
    metadata: dict[str, str | list[str]] = Field(default_factory=dict)
    text: str = Field(default="", max_length=10000)
    sidecar: dict[str, str | list[str]] | None = None

    @field_validator("path")
    @classmethod
    def safe_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        windows = PureWindowsPath(value)
        if (
            path.is_absolute()
            or windows.is_absolute()
            or windows.drive
            or "\\" in value
            or any(part in {"", ".", ".."} for part in value.split("/"))
            or path.suffix not in {".epub", ".txt"}
        ):
            raise ValueError("fixture paths must be confined relative .epub/.txt paths")
        return value


class CaseLabel(LabelModel):
    id: str = Field(pattern=r"^[a-z0-9-]+$", max_length=80)
    groups: list[str] = Field(min_length=1, max_length=8)
    rationale: str = Field(min_length=1, max_length=2000)
    files: list[FileLabel] = Field(min_length=1, max_length=5)
    records: list[str] = Field(default_factory=list, max_length=10)
    record_overrides: dict[str, dict[str, Any]] = Field(default_factory=dict)
    provider_failure: Literal["none", "one", "all"] = "none"
    sibling_formats: bool = False


class Corpus(LabelModel):
    schema_version: Literal[1]
    description: str
    works: dict[str, WorkLabel]
    editions: dict[str, EditionLabel]
    cases: list[CaseLabel] = Field(min_length=1, max_length=200)

    @model_validator(mode="after")
    def consistent_labels(self) -> Corpus:
        if len({case.id for case in self.cases}) != len(self.cases):
            raise ValueError("case IDs must be unique")
        for edition in self.editions.values():
            if edition.work not in self.works:
                raise ValueError("edition references an unknown work")
        for case in self.cases:
            if len({file.path for file in case.files}) != len(case.files):
                raise ValueError("case file paths must be unique")
            if any(record not in self.editions for record in case.records):
                raise ValueError("provider record references an unknown edition")
            if any(record not in case.records for record in case.record_overrides):
                raise ValueError("override references a record not in this case")
            for file in case.files:
                if file.work not in self.works:
                    raise ValueError("file label references an unknown work")
                if file.edition is not None and (
                    file.edition not in self.editions
                    or self.editions[file.edition].work != file.work
                ):
                    raise ValueError("file edition must belong to its labelled work")
        return self


def read_corpus(path: Path) -> Corpus:
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("corpus must be at most 1 MiB")
    return Corpus.model_validate_json(path.read_bytes())


def _metadata_xml(metadata: dict[str, str | list[str]]) -> bytes:
    package = ET.Element("package", {"xmlns": "http://www.idpf.org/2007/opf", "version": "2.0"})
    node = ET.SubElement(package, "metadata")
    for field, raw in metadata.items():
        tag = {"authors": "creator", "isbn": "identifier", "year": "date"}.get(field, field)
        for value in raw if isinstance(raw, list) else [raw]:
            ET.SubElement(node, f"{{http://purl.org/dc/elements/1.1/}}{tag}").text = str(value)
    return ET.tostring(package, encoding="utf-8", xml_declaration=True)


def _write_file(root: Path, label: FileLabel, case: CaseLabel, corpus: Corpus) -> Path:
    path = root / label.path
    path.parent.mkdir(parents=True, exist_ok=True)
    truth = corpus.works[label.work]
    text = label.text or f"Title: {truth.title}\nAuthor: {', '.join(truth.authors)}\n\n"
    text += f"Synthetic labelled body: {case.id}/{label.path}.\n" * 8
    if path.suffix == ".txt":
        path.write_bytes(text.encode("utf-8"))
    else:
        opf_namespace = "http://www.idpf.org/2007/opf"
        package = ET.fromstring(_metadata_xml(label.metadata))
        package.set("unique-identifier", "corpus-id")
        metadata = package.find(f"{{{opf_namespace}}}metadata")
        if metadata is None:
            raise RuntimeError("generated EPUB lacks its metadata element")
        ET.SubElement(
            metadata, "{http://purl.org/dc/elements/1.1/}identifier", {"id": "corpus-id"}
        ).text = f"urn:offline-corpus:{case.id}:{quote(label.path, safe='')}"
        manifest = ET.SubElement(package, f"{{{opf_namespace}}}manifest")
        ET.SubElement(
            manifest,
            f"{{{opf_namespace}}}item",
            {"id": "body", "href": "body.xhtml", "media-type": "application/xhtml+xml"},
        )
        spine = ET.SubElement(package, f"{{{opf_namespace}}}spine")
        ET.SubElement(spine, f"{{{opf_namespace}}}itemref", {"idref": "body"})
        html = ET.Element("html", {"xmlns": "http://www.w3.org/1999/xhtml"})
        head = ET.SubElement(html, "head")
        ET.SubElement(head, "title").text = truth.title
        ET.SubElement(ET.SubElement(html, "body"), "p").text = text
        container = (
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>'
        )
        with ZipFile(path, "w") as archive:
            for name, content in (
                ("mimetype", b"application/epub+zip"),
                ("META-INF/container.xml", container.encode()),
                ("OEBPS/content.opf", ET.tostring(package, encoding="utf-8", xml_declaration=True)),
                ("OEBPS/body.xhtml", ET.tostring(html, encoding="utf-8", xml_declaration=True)),
            ):
                entry = ZipInfo(name, (2000, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_STORED
                archive.writestr(entry, content)
    if label.sidecar is not None:
        (path.parent / "metadata.opf").write_bytes(_metadata_xml(label.sidecar))
    return path


class FixtureProvider:
    """Frozen provider boundary; extraction, normalization and decisions stay real."""

    def __init__(self, records: list[dict[str, Any]], *, failed: bool = False) -> None:
        self.provider_id = "offline-failed" if failed else "offline-records"
        self.records = records
        self.failed = failed
        self.calls = 0

    def search(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls += 1
        if self.failed:
            raise OSError("labelled provider outage")
        # Return snapshots, not mutable objects the scoring path could retain.
        return json.loads(json.dumps(self.records))


def _records(case: CaseLabel, corpus: Corpus) -> list[dict[str, Any]]:
    result = []
    for key in case.records:
        edition = corpus.editions[key]
        work = corpus.works[edition.work]
        record: dict[str, Any] = {
            "key": key,
            "title": work.title,
            "author_name": work.authors,
            "isbn_13": [edition.isbn13] if edition.isbn13 else [],
            "publisher": edition.publisher,
            "language": edition.language,
            "first_publish_year": edition.year,
        }
        record.update(case.record_overrides.get(key, {}))
        result.append(record)
    return result


def _key(value: str) -> str:
    return re.sub(r"\W+", " ", value.casefold()).strip()


def _observe(job: ImportJob, file: FileLabel, case: CaseLabel, corpus: Corpus) -> dict[str, Any]:
    job.refresh_from_db()
    automatic = job.state == ImportJob.State.COMPLETE and job.book_id is not None
    failed = job.state == ImportJob.State.FAILED
    book = job.book
    authors = sorted(book.authors.values_list("full_name", flat=True)) if book else []
    truth = corpus.works[file.work]
    work_correct = (
        book is not None
        and _key(book.title) == _key(truth.title)
        and (sorted(map(_key, authors)) == sorted(map(_key, truth.authors)))
    )
    identifiers = (
        sorted(Identifier.objects.filter(book=book).values_list("value", flat=True)) if book else []
    )
    if book is None:
        edition_correct = False
    elif file.edition is None:
        edition_correct = (
            not identifiers
            and not book.publisher
            and book.published_year is None
            and book.language == file.work_only_language
        )
    else:
        edition = corpus.editions[file.edition]
        edition_correct = (
            (not edition.isbn13 or edition.isbn13 in identifiers)
            and _key(book.publisher) == _key(edition.publisher)
            and (not edition.language or book.language == normalize_language(edition.language))
            and (edition.year is None or book.published_year == edition.year)
        )
    searches = list(job.metadata_searches.order_by("pk"))
    provider_failures = sum(
        1
        for search in searches
        for provider in search.providers
        if provider.get("status") == "error"
    )
    evidence = job.evidence if isinstance(job.evidence, dict) else {}
    review = ReviewTask.objects.filter(import_job=job, state__in=["open", "claimed"]).exists()
    return {
        "case": case.id,
        "file": file.path,
        "groups": case.groups,
        "truth": {
            "work": file.work,
            "edition": file.edition,
            "require_review": file.require_review,
            "work_only_language": file.work_only_language if file.edition is None else None,
        },
        "outcome": "auto"
        if automatic
        else "failed"
        if failed
        else "review"
        if review
        else job.state,
        "selected": {
            "title": book.title,
            "authors": authors,
            "identifiers": identifiers,
            "publisher": book.publisher,
            "language": book.language,
            "year": book.published_year,
        }
        if book
        else None,
        "correct_work": work_correct if automatic else None,
        "correct_edition": edition_correct if automatic else None,
        "correct_auto": automatic and work_correct and edition_correct and not file.require_review,
        "wrong_auto": automatic
        and (not work_correct or not edition_correct or file.require_review),
        "review_policy_violation": automatic and file.require_review,
        "provider_failures": provider_failures,
        "provider_search_runs": len(searches),
        "local_suggestion": local_identity.LOCAL_IDENTITY_KEY in evidence,
        "block_reason": evidence.get(intake.AUTO_ACCEPT_BLOCKED_KEY),
        "error": job.last_error if failed else "",
        "candidates": [
            {"ref": c.provider_ref, "score": round(float(c.score), 6)}
            for c in MetadataCandidate.objects.filter(import_job=job).order_by(
                "-score", "provider_ref"
            )
        ],
    }


def _tally(rows: list[dict[str, Any]]) -> dict[str, Any]:
    review = sum(row["outcome"] == "review" for row in rows)
    return {
        "rows": len(rows),
        "auto": sum(row["outcome"] == "auto" for row in rows),
        "correct_auto": sum(row["correct_auto"] for row in rows),
        "wrong_auto": sum(row["wrong_auto"] for row in rows),
        "wrong_work_auto": sum(row["correct_work"] is False for row in rows),
        "wrong_edition_auto": sum(row["correct_edition"] is False for row in rows),
        "review_policy_violations": sum(row["review_policy_violation"] for row in rows),
        "review": review,
        "review_rate": round(review / len(rows), 6) if rows else 0,
        "failed": sum(row["outcome"] == "failed" for row in rows),
        "provider_failure_rows": sum(row["provider_failures"] > 0 for row in rows),
        "provider_failures": sum(row["provider_failures"] for row in rows),
        "local_suggestions": sum(row["local_suggestion"] for row in rows),
    }


def run_corpus(corpus: Corpus, root: Path, threshold: float) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []
    unexpected_network: list[bool] = []

    def deny_network(*args: Any, **kwargs: Any) -> None:
        unexpected_network.append(True)
        raise OSError("unexpected network access during offline evaluation")

    with ExitStack() as stack:
        stack.enter_context(patch.object(identification, "AUTO_ACCEPT_SCORE", threshold))
        stack.enter_context(patch.object(intake, "AUTO_ACCEPT_SCORE", threshold))
        stack.enter_context(patch("socket.socket.connect", side_effect=deny_network))
        stack.enter_context(patch("socket.socket.connect_ex", side_effect=deny_network))
        stack.enter_context(patch("socket.socket.sendto", side_effect=deny_network))
        stack.enter_context(patch("socket.create_connection", side_effect=deny_network))
        stack.enter_context(patch("socket.getaddrinfo", side_effect=deny_network))
        for case in corpus.cases:
            case_root = root / case.id
            case_root.mkdir(parents=True)
            paths = [_write_file(case_root, file, case, corpus) for file in case.files]
            original_hashes = [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
            providers = [FixtureProvider(_records(case, corpus))]
            if case.provider_failure != "none":
                providers.insert(0, FixtureProvider([], failed=True))
            if case.provider_failure == "all":
                providers = providers[:1]
            with (
                transaction.atomic(),
                patch.object(identification, "_provider_instances", return_value=providers),
            ):
                source = SourceLocation.objects.create(
                    name=case.id,
                    root_path=str(case_root),
                    transfer_mode=SourceLocation.TransferMode.COPY,
                    group_sibling_formats=case.sibling_formats,
                )
                jobs = [intake.create_source_import_job(path, source) for path in paths]
                for job in jobs:
                    intake.process_import_job(str(job.pk))
                rows.extend(
                    _observe(job, file, case, corpus)
                    for job, file in zip(jobs, case.files, strict=True)
                )
                book_ids = {job.book_id for job in jobs if job.book_id is not None}
                # Refreshes in _observe above also expose sibling propagation.
                cases.append(
                    {
                        "case": case.id,
                        "canonical_books": len(book_ids),
                        "linked_formats": sorted(
                            job.file_variant.format for job in jobs if job.file_variant
                        ),
                        "provider_calls": sum(provider.calls for provider in providers),
                    }
                )
                if any(row["outcome"] not in {"auto", "review", "failed"} for row in rows):
                    raise RuntimeError("intake did not reach an evaluable terminal state")
                transaction.set_rollback(True)
            if original_hashes != [hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]:
                raise RuntimeError("evaluation modified a COPY source")
    if unexpected_network:
        raise RuntimeError("offline evaluation attempted network access")
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        for group in row["groups"]:
            groups[group].append(row)
    return {
        "threshold": threshold,
        "totals": _tally(rows),
        "groups": {group: _tally(group_rows) for group, group_rows in sorted(groups.items())},
        "rows": rows,
        "cases": cases,
    }
