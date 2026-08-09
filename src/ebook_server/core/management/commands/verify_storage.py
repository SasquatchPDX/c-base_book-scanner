"""Verify that database artifact records match persistent storage."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from django.core.management.base import BaseCommand, CommandError, CommandParser

from ebook_server.storage import StorageBackend

from ...models import CoverImage, FileVariant, ScanPageVersion, ScanTailorStageState
from ...storage_registry import StorageConfigurationError, backend_for_location


@dataclass(frozen=True)
class ArtifactReference:
    kind: str
    record_id: str
    storage_identifier: str
    expected_sha256: str
    backend: StorageBackend


def artifact_references() -> Iterable[ArtifactReference]:
    for variant in FileVariant.objects.select_related("storage_location").iterator():
        yield ArtifactReference(
            "file_variant",
            str(variant.pk),
            variant.storage_identifier,
            variant.sha256,
            backend_for_location(variant.storage_location),
        )
    for cover in CoverImage.objects.select_related("storage_location").iterator():
        yield ArtifactReference(
            "cover_image",
            str(cover.pk),
            cover.storage_identifier,
            cover.sha256,
            backend_for_location(cover.storage_location),
        )
    for version in ScanPageVersion.objects.select_related("storage_location").iterator():
        yield ArtifactReference(
            "scan_page_version",
            str(version.pk),
            version.storage_identifier,
            version.checksum_sha256,
            backend_for_location(version.storage_location),
        )
    states = ScanTailorStageState.objects.select_related(
        "output_storage_location",
        "preview_storage_location",
    ).iterator()
    for state in states:
        if state.output_storage_location and state.output_storage_identifier:
            yield ArtifactReference(
                "stage_output",
                str(state.pk),
                state.output_storage_identifier,
                state.output_sha256,
                backend_for_location(state.output_storage_location),
            )
        if state.preview_storage_location and state.preview_storage_identifier:
            yield ArtifactReference(
                "stage_preview",
                str(state.pk),
                state.preview_storage_identifier,
                "",
                backend_for_location(state.preview_storage_location),
            )


class Command(BaseCommand):
    help = "Verify that all database artifact references exist and optionally match hashes."

    def add_arguments(self, parser: CommandParser) -> None:
        parser.add_argument(
            "--verify-hashes",
            action="store_true",
            help="Recompute SHA-256 for records that carry an expected digest.",
        )

    def handle(self, *args: object, **options: object) -> None:
        verify_hashes = bool(options["verify_hashes"])
        checked = 0
        missing: list[str] = []
        mismatched: list[str] = []
        configuration_errors: list[str] = []

        try:
            references = artifact_references()
            for reference in references:
                checked += 1
                label = f"{reference.kind}:{reference.record_id}:{reference.storage_identifier}"
                try:
                    if not reference.backend.exists(reference.storage_identifier):
                        missing.append(label)
                        continue
                    if (
                        verify_hashes
                        and reference.expected_sha256
                        and reference.backend.hash(reference.storage_identifier)
                        != reference.expected_sha256
                    ):
                        mismatched.append(label)
                except (OSError, ValueError) as exc:
                    configuration_errors.append(f"{label}: {exc}")
        except StorageConfigurationError as exc:
            configuration_errors.append(str(exc))

        self.stdout.write(
            f"checked={checked} missing={len(missing)} "
            f"mismatched={len(mismatched)} errors={len(configuration_errors)}",
        )
        for issue in missing:
            self.stderr.write(f"MISSING {issue}")
        for issue in mismatched:
            self.stderr.write(f"HASH_MISMATCH {issue}")
        for issue in configuration_errors:
            self.stderr.write(f"ERROR {issue}")

        if missing or mismatched or configuration_errors:
            raise CommandError("storage verification failed")
