"""Read-only artifact ownership inventory and conservative derivative eligibility."""

from __future__ import annotations

import os
import re
import stat
import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from django.utils import timezone

from ebook_server.storage import LocalFilesystemStorage, StorageEntry, check_storage_root

from . import book_opf
from .models import (
    BookOpfState,
    CoverImage,
    CoverThumbnailFile,
    FileVariant,
    ImportJob,
    ProcessingJob,
    ScanCover,
    ScanJob,
    ScanLog,
    ScanPageImage,
    ScanPageVersion,
    ScanTailorStageState,
    SourceLocation,
    StorageLocation,
)
from .storage_registry import StorageConfigurationError, filesystem_backend_for_location


@dataclass(frozen=True)
class ArtifactReference:
    kind: str
    record_id: str
    storage_identifier: str
    expected_sha256: str
    location: StorageLocation | None

    @property
    def backend(self) -> LocalFilesystemStorage:
        if self.location is None:
            raise StorageConfigurationError(f"{self.kind} has no storage location")
        return filesystem_backend_for_location(self.location)


def artifact_references() -> Iterable[ArtifactReference]:
    for model, kind, digest in (
        (FileVariant, "file_variant", "sha256"),
        (CoverImage, "cover_image", "sha256"),
        (ScanPageVersion, "scan_page_version", "checksum_sha256"),
        (ScanCover, "scan_cover", "checksum_sha256"),
        (ScanLog, "scan_log", "sha256"),
        (CoverThumbnailFile, "cover_thumbnail", "sha256"),
        (ImportJob, "archived_import", "sha256"),
    ):
        for row in model.objects.select_related("storage_location").iterator():
            if row.storage_identifier:
                yield ArtifactReference(
                    kind,
                    str(row.pk),
                    row.storage_identifier,
                    getattr(row, digest),
                    row.storage_location,
                )
    scan = StorageLocation.objects.filter(identifier="scan").first()
    archive = StorageLocation.objects.filter(identifier="default").first()
    for log in ScanLog.objects.exclude(staging_identifier="").iterator():
        yield ArtifactReference("staged_scan_log", str(log.pk), log.staging_identifier, "", scan)
    for job in ScanJob.objects.iterator():
        for field, location in (
            ("scan_jsonl_job_identifier", scan),
            ("scan_jsonl_book_identifier", archive),
        ):
            identifier = getattr(job, field)
            if identifier:
                yield ArtifactReference("rendered_scan_log", str(job.pk), identifier, "", location)
    for state in ScanTailorStageState.objects.select_related(
        "output_storage_location", "preview_storage_location"
    ).iterator():
        for kind, identifier, digest, location in (
            (
                "stage_output",
                state.output_storage_identifier,
                state.output_sha256,
                state.output_storage_location,
            ),
            ("stage_preview", state.preview_storage_identifier, "", state.preview_storage_location),
            (
                "stage_thumbnail",
                _existing_stage_thumbnail(state),
                "",
                state.output_storage_location,
            ),
            ("stage_input", state.input_storage_identifier, "", scan),
        ):
            if identifier:
                yield ArtifactReference(kind, str(state.pk), identifier, digest, location)
    for page_image in ScanPageImage.objects.select_related("storage_location").iterator():
        for kind, identifier in (
            ("page_work_image", page_image.work_storage_identifier),
            ("page_thumbnail", page_image.thumbnail_storage_identifier),
        ):
            if identifier:
                yield ArtifactReference(
                    kind, str(page_image.pk), identifier, "", page_image.storage_location
                )
    for opf_state in (
        BookOpfState.objects.filter(written_generation__gt=0).select_related("book").iterator()
    ):
        for target in book_opf.opf_targets(opf_state.book):
            yield ArtifactReference(
                "generated_book_opf", str(opf_state.pk), target.identifier, "", target.location
            )


def _existing_stage_thumbnail(state: ScanTailorStageState) -> str:
    """The stage's strip thumbnail (#195 step 3), if it was written.

    Stages published before thumbnails existed have none; it is regenerable, so its
    absence is not a missing artifact.
    """
    if not state.output_storage_identifier or state.output_storage_location is None:
        return ""
    identifier = f"{state.output_storage_identifier.rsplit('/', 1)[0]}/thumbnail.jpg"
    try:
        backend = filesystem_backend_for_location(state.output_storage_location)
        return identifier if backend.exists(identifier) else ""
    except (OSError, ValueError, StorageConfigurationError):
        return ""


def physical_key(location: StorageLocation, identifier: str) -> str:
    return os.path.normcase(
        str(filesystem_backend_for_location(location).resolve_location(identifier))
    )


def inode_key(location: StorageLocation, identifier: str) -> str | None:
    try:
        info = filesystem_backend_for_location(location).resolve_location(identifier).stat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("artifact reference is not a regular file")
    return f"@inode:{info.st_dev}:{info.st_ino}" if info.st_ino else None


def references_for(
    index: dict[str, list[ArtifactReference]], location: StorageLocation, identifier: str
) -> list[ArtifactReference]:
    owned = index.get(physical_key(location, identifier), [])
    identity = inode_key(location, identifier)
    aliases = index.get(identity, []) if identity else []
    return list(
        {
            (
                ref.kind,
                ref.record_id,
                ref.location.pk if ref.location else None,
                ref.storage_identifier,
            ): ref
            for ref in [*owned, *aliases]
        }.values()
    )


def reference_index() -> tuple[dict[str, list[ArtifactReference]], list[str]]:
    result: dict[str, list[ArtifactReference]] = defaultdict(list)
    errors = []
    for reference in artifact_references():
        try:
            if reference.location is None:
                raise StorageConfigurationError(f"{reference.kind} lacks a location")
            result[physical_key(reference.location, reference.storage_identifier)].append(reference)
            identity = inode_key(reference.location, reference.storage_identifier)
            if identity:
                result[identity].append(reference)
        except (OSError, ValueError, StorageConfigurationError) as exc:
            errors.append(f"{reference.kind}:{reference.record_id}: {exc}")
    return result, errors


STAGE_FILE = re.compile(
    r"^scan-jobs/(?P<job>[^/]+)/(?P<page>[0-9a-f-]{36})/(?P<stage>[a-z_]+)/"
    # work.png / preview.jpg: working-resolution artifacts of stages 1-4 (#195 step 2).
    r"v(?P<revision>[1-9][0-9]*)/(?P<name>output\.png|preview\.png|work\.png|preview\.jpg|thumbnail\.jpg)$"
)


def stage_for(identifier: str) -> tuple[ScanTailorStageState | None, int]:
    match = STAGE_FILE.fullmatch(identifier)
    if match is None:
        return None, 0
    try:
        if str(uuid.UUID(match["page"])) != match["page"]:
            return None, 0
    except ValueError:
        return None, 0
    state = ScanTailorStageState.objects.filter(
        scan_job_id=match["job"],
        scan_page_id=match["page"],
        stage=match["stage"],
        scan_page__scan_job_id=match["job"],
    ).first()
    return state, int(match["revision"])


def eligibility(
    location: StorageLocation,
    entry: StorageEntry,
    *,
    min_age_days: int,
    known_references: dict[str, list[ArtifactReference]] | None = None,
    index_errors: list[str] | None = None,
) -> str:
    """Empty reason means eligible; filename appearance alone never proves ownership."""
    if location.read_only or location.identifier != "scan" or not entry.regular:
        return "protected location or non-regular entry"
    path = Path(filesystem_backend_for_location(location).resolve_location(entry.identifier))
    protected = [
        Path(source.root_path).expanduser().resolve() for source in SourceLocation.objects.all()
    ]
    protected.extend(
        Path(filesystem_backend_for_location(other).root).resolve()
        for other in StorageLocation.objects.filter(read_only=True)
    )
    if any(path == root or root in path.parents for root in protected):
        return "registered source/read-only root"
    if timezone.now().timestamp() * 1e9 - entry.mtime_ns < min_age_days * 86400 * 1e9:
        return "retention age not reached"
    state, revision = stage_for(entry.identifier)
    if state is None or revision >= state.revision:
        return "not a proven superseded stage revision"
    if ScanJob.objects.filter(pk=state.scan_job_id, state=ScanJob.State.FINALIZING).exists():
        return "scan finalization active"
    if (
        ScanTailorStageState.objects.filter(
            scan_job_id=state.scan_job_id, status__in=["queued", "running"]
        ).exists()
        or ProcessingJob.objects.filter(
            details__scan_job_id=str(state.scan_job_id), state__in=["queued", "running"]
        ).exists()
    ):
        return "scan work active"
    references, errors = (
        reference_index() if known_references is None else (known_references, index_errors or [])
    )
    if errors:
        return "reference index incomplete"
    if references_for(references, location, entry.identifier):
        return "referenced artifact"
    return ""


def inventory(
    *,
    locations: list[str] | None = None,
    min_age_days: int = 30,
    limit: int = 10000,
    verify_hashes: bool = False,
) -> dict[str, Any]:
    references, errors = reference_index()
    try:
        check_storage_root()
    except OSError as exc:
        errors.append(f"storage mount policy: {exc}")
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    selected = StorageLocation.objects.all().order_by("pk")
    if locations:
        selected = selected.filter(identifier__in=locations)
    roots = {}
    for location in selected:
        try:
            backend = filesystem_backend_for_location(location)
            roots[location.identifier] = str(Path(backend.root).resolve())
            for entry in backend.inventory(limit=limit):
                key = (
                    physical_key(location, entry.identifier)
                    if entry.regular
                    else os.path.normcase(str(Path(backend.root).resolve() / entry.identifier))
                )
                seen.add(key)
                owned = (
                    references_for(references, location, entry.identifier)
                    if entry.regular
                    else references.get(key, [])
                )
                match = STAGE_FILE.fullmatch(entry.identifier)
                kind = (
                    "stage_preview"
                    if match and match["name"] == "preview.png"
                    else "stage_output"
                    if match
                    else "untracked"
                )
                if owned:
                    kind = "+".join(sorted({reference.kind for reference in owned}))
                reason = (
                    eligibility(
                        location,
                        entry,
                        min_age_days=min_age_days,
                        known_references=references,
                        index_errors=errors,
                    )
                    if match and not owned
                    else "referenced artifact"
                    if owned
                    else "outside retirement scope"
                )
                eligible = not reason and not errors
                digest = backend.hash(entry.identifier) if eligible else ""
                if eligible and backend.entry(entry.identifier).signature() != entry.signature():
                    eligible, reason, digest = False, "changed during inventory", ""
                status = "referenced" if owned else "unreferenced"
                bad_references = []
                if verify_hashes and owned and entry.regular:
                    expected = {
                        reference.expected_sha256
                        for reference in owned
                        if reference.expected_sha256
                    }
                    actual_digest = backend.hash(entry.identifier) if expected else ""
                    if expected and any(
                        actual_digest != expected_digest for expected_digest in expected
                    ):
                        status = "hash_mismatch"
                        bad_references = [
                            {
                                "kind": ref.kind,
                                "record_id": ref.record_id,
                                "expected_sha256": ref.expected_sha256,
                            }
                            for ref in owned
                            if ref.expected_sha256 and ref.expected_sha256 != actual_digest
                        ]
                findings.append(
                    {
                        "location": location.identifier,
                        "identifier": entry.identifier,
                        "kind": kind,
                        "status": status,
                        "bytes": entry.size_bytes,
                        "age_days": round(
                            max(0, (timezone.now().timestamp() * 1e9 - entry.mtime_ns) / 86400e9), 3
                        ),
                        "eligible": eligible,
                        "reason": reason,
                        "signature": list(entry.signature()),
                        "sha256": digest,
                        "checksum_mismatches": bad_references,
                    }
                )
        except (OSError, ValueError, StorageConfigurationError) as exc:
            errors.append(f"{location.identifier}: {exc}")
    for key, owned in references.items():
        if key.startswith("@inode:"):
            continue
        if key in seen:
            continue
        for reference in owned:
            if reference.location is None:
                continue
            if locations and reference.location.identifier not in locations:
                continue
            try:
                if not reference.backend.exists(reference.storage_identifier):
                    findings.append(
                        {
                            "location": reference.location.identifier,
                            "identifier": reference.storage_identifier,
                            "kind": reference.kind,
                            "status": "missing",
                            "bytes": None,
                            "age_days": None,
                            "eligible": False,
                            "reason": "missing referenced artifact",
                        }
                    )
            except (OSError, ValueError, StorageConfigurationError) as exc:
                errors.append(f"{reference.kind}:{reference.record_id}: {exc}")
    summaries: dict[tuple[str, str, str], dict[str, Any]] = {}
    for finding in findings:
        if errors and finding["eligible"]:
            finding["eligible"] = False
            finding["reason"] = "inventory is incomplete"
        summary_key = (finding["location"], finding["kind"], finding["status"])
        tally = summaries.setdefault(
            summary_key,
            {
                "location": summary_key[0],
                "kind": summary_key[1],
                "status": summary_key[2],
                "files": 0,
                "bytes": 0,
                "unknown_bytes": 0,
            },
        )
        tally["files"] += 1
        tally["bytes"] += finding["bytes"] or 0
        tally["unknown_bytes"] += finding["bytes"] is None
    return {
        "schema_version": 1,
        "min_age_days": min_age_days,
        "roots": roots,
        "findings": findings,
        "summary": list(summaries.values()),
        "errors": errors,
    }
