"""Explicit reversible quarantine; no purge, and no guessing original ownership."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

from django.db import transaction
from django.utils import timezone

from ebook_server.storage import check_storage_root

from .models import ScanTailorStageState, StorageLocation
from .storage_inventory import eligibility, stage_for
from .storage_registry import filesystem_backend_for_location

MANIFEST = re.compile(r"^\.quarantine/derived/[0-9a-f-]{36}/manifest\.json$")


class RetentionError(RuntimeError):
    """No destructive fallback is allowed when ownership or identity is uncertain."""


def _binding(location: StorageLocation, root: str) -> None:
    if location.identifier != "scan" or location.read_only:
        raise RetentionError("only writable scan storage supports derivative quarantine")
    if os.path.normcase(
        str(Path(filesystem_backend_for_location(location).root).resolve())
    ) != os.path.normcase(root):
        raise RetentionError("storage root changed since inventory")
    check_storage_root()  # read-only mount/sentinel guard; never creates the root


def quarantine_finding(
    finding: dict[str, Any], *, root: str, min_age_days: int, apply: bool
) -> dict[str, Any]:
    identifier = str(finding.get("identifier", ""))
    result: dict[str, Any] = {"identifier": identifier, "status": "skipped"}
    location = StorageLocation.objects.get(identifier=finding["location"])
    _binding(location, root)
    backend = filesystem_backend_for_location(location)
    state, _revision = stage_for(identifier)
    if state is None:
        return {**result, "reason": "unknown stage ownership"}
    try:
        before = backend.entry(identifier)
        actual_digest = backend.hash(identifier)
    except FileNotFoundError:
        return {**result, "reason": "already missing"}
    if list(before.signature()) != finding.get("signature"):
        return {**result, "reason": "file changed since inventory"}
    digest = finding.get("sha256", "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest) or actual_digest != digest:
        return {**result, "reason": "checksum changed or absent"}
    with transaction.atomic():
        location = StorageLocation.objects.select_for_update(no_key=True).get(pk=location.pk)
        _binding(location, root)
        if not ScanTailorStageState.objects.select_for_update().filter(pk=state.pk).first():
            return {**result, "reason": "stage ownership no longer exists"}
        # Fresh references, active work, source roots, age and exact file identity.
        try:
            fresh_entry = backend.entry(identifier)
        except FileNotFoundError:
            return {**result, "reason": "already missing"}
        reason = eligibility(location, fresh_entry, min_age_days=min_age_days)
        if reason:
            return {**result, "reason": reason}
        if fresh_entry.signature() != before.signature():
            return {**result, "reason": "file changed during verification"}
        if not apply:
            return {**result, "status": "would_quarantine"}
        prefix = f".quarantine/derived/{uuid.uuid4()}"
        manifest_id = f"{prefix}/manifest.json"
        quarantine_id = f"{prefix}/artifact"
        journal = {
            "schema_version": 1,
            "location": location.identifier,
            "root": root,
            "original": identifier,
            "quarantine": quarantine_id,
            "sha256": digest,
            "bytes": before.size_bytes,
            "min_age_days": min_age_days,
            "created_at": timezone.now().isoformat(),
            "status": "planned",
        }
        # Record intent durably first. A crash at either boundary is recoverable.
        staged = backend.stage_bytes(manifest_id, json.dumps(journal, sort_keys=True).encode())
        backend.commit_staged_exclusive(staged, manifest_id)
        try:
            backend.commit_staged_exclusive(identifier, quarantine_id)
            journal["status"] = "quarantined"
            backend.replace_bytes(manifest_id, json.dumps(journal, sort_keys=True).encode())
        except Exception as exc:
            raise RetentionError(
                f"quarantine interrupted; retain and inspect {manifest_id}"
            ) from exc
        return {**result, "status": "quarantined", "manifest": manifest_id}


def collect_plan(
    plan: dict[str, Any], *, apply: bool = False, limit: int = 100
) -> list[dict[str, Any]]:
    if not isinstance(plan, dict) or plan.get("schema_version") != 1 or plan.get("errors") != []:
        raise RetentionError("a complete error-free inventory is required")
    age = plan.get("min_age_days")
    if type(age) is not int or age < 1:
        raise RetentionError("retention must be at least one day")
    findings = plan.get("findings")
    if not isinstance(findings, list) or not isinstance(plan.get("roots"), dict):
        raise RetentionError("invalid inventory plan")
    candidates: list[tuple[dict[str, Any], str]] = []
    for finding in findings:
        if not isinstance(finding, dict):
            raise RetentionError("invalid inventory finding")
        if finding.get("eligible") is not True:
            continue
        signature = finding.get("signature")
        digest = finding.get("sha256")
        if (
            finding.get("location") != "scan"
            or not isinstance(finding.get("identifier"), str)
            or not isinstance(signature, list)
            or len(signature) != 6
            or not all(type(value) is int for value in signature)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise RetentionError("invalid quarantine candidate")
        root = plan["roots"].get(finding.get("location"))
        if not isinstance(root, str):
            raise RetentionError("inventory lacks its storage root binding")
        candidates.append((finding, root))
    results: list[dict[str, Any]] = []
    for finding, root in candidates[:limit]:
        results.append(quarantine_finding(finding, root=root, min_age_days=age, apply=apply))
    return results


def restore_manifest(
    location: StorageLocation, manifest_id: str, *, apply: bool = False
) -> dict[str, Any]:
    if not MANIFEST.fullmatch(manifest_id):
        raise RetentionError("restore requires a quarantine manifest identifier")
    backend = filesystem_backend_for_location(location)
    backend.entry(manifest_id)  # reject symlink components before reading the journal
    with backend.open(manifest_id) as stream:
        content = stream.read(65537)
    if len(content) > 65536:
        raise RetentionError("quarantine manifest is too large")
    journal = json.loads(content)
    expected_id = str(Path(manifest_id).parent.as_posix()) + "/artifact"
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != 1
        or journal.get("location") != location.identifier
        or journal.get("quarantine") != expected_id
        or not isinstance(journal.get("root"), str)
        or not isinstance(journal.get("original"), str)
        or type(journal.get("bytes")) is not int
        or journal["bytes"] < 0
        or not isinstance(journal.get("sha256"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", journal["sha256"])
    ):
        raise RetentionError("invalid quarantine journal binding")
    _binding(location, journal["root"])
    state, _revision = stage_for(journal["original"])
    if state is None:
        raise RetentionError(
            "stage ownership no longer verifiable; preserve quarantine for manual review"
        )
    try:
        entry = backend.entry(expected_id)
    except FileNotFoundError:
        if (
            backend.exists(journal["original"])
            and backend.hash(journal["original"]) == journal["sha256"]
        ):
            return {
                "status": "original_present",
                "identifier": journal["original"],
                "manifest": manifest_id,
            }
        raise RetentionError("quarantine artifact is missing; retain its journal") from None
    if (
        not entry.regular
        or entry.size_bytes != journal["bytes"]
        or backend.hash(expected_id) != journal["sha256"]
    ):
        raise RetentionError("quarantined bytes differ from the journal")
    with transaction.atomic():
        current_location = StorageLocation.objects.select_for_update(no_key=True).get(
            pk=location.pk
        )
        _binding(current_location, journal["root"])
        locked = ScanTailorStageState.objects.select_for_update().filter(pk=state.pk).first()
        if locked is None:
            raise RetentionError("stage ownership no longer exists")
        if backend.exists(journal["original"]):
            raise RetentionError("restore destination is occupied; neither copy was changed")
        reason = eligibility(
            current_location, replace(entry, identifier=journal["original"]), min_age_days=0
        )
        if reason:
            raise RetentionError(f"restore is protected: {reason}")
        if backend.entry(expected_id).signature() != entry.signature():
            raise RetentionError("quarantined file changed during restore verification")
        if apply:
            backend.commit_staged_exclusive(expected_id, journal["original"])
            journal["status"] = "restored"
            journal["restored_at"] = timezone.now().isoformat()
            backend.replace_bytes(manifest_id, json.dumps(journal, sort_keys=True).encode())

    return {
        "status": "restored" if apply else "would_restore",
        "identifier": journal["original"],
        "manifest": manifest_id,
    }
