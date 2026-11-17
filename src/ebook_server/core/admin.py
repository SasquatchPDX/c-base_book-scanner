"""Admin registrations for core models."""

from __future__ import annotations

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from .models import (
    AppUserProfile,
    Author,
    Book,
    Category,
    CoverImage,
    FileVariant,
    Genre,
    Identifier,
    ImportJob,
    MetadataCandidate,
    ProcessingJob,
    ReviewTask,
    ScanJob,
    ScanPage,
    ScanPageVersion,
    ScanTailorStageState,
    ScanTodo,
    SourceLocation,
    StorageLocation,
)


@admin.register(AppUserProfile)
class AppUserProfileAdmin(admin.ModelAdmin[AppUserProfile]):
    list_display = ("ldap_uid", "role", "is_disabled", "created_at")
    list_filter = ("role", "is_disabled")


@admin.register(Book)
class BookAdmin(admin.ModelAdmin[Book]):
    list_display = ("title", "publisher", "publication_year", "publication_date")
    search_fields = ("title", "subtitle", "publisher")
    filter_horizontal = ("authors", "categories", "genres")


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin[Author]):
    list_display = ("full_name",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin[Category]):
    list_display = ("name",)


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin[Genre]):
    list_display = ("name",)


@admin.register(Identifier)
class IdentifierAdmin(admin.ModelAdmin[Identifier]):
    list_display = ("book", "kind", "value")


@admin.register(CoverImage)
class CoverImageAdmin(admin.ModelAdmin[CoverImage]):
    list_display = (
        "book",
        "position",
        "source_variant",
        "extractor_tool",
        "storage_identifier",
    )


@admin.register(FileVariant)
class FileVariantAdmin(admin.ModelAdmin[FileVariant]):
    list_display = (
        "book",
        "format",
        "original_filename",
        "source_type",
        "processing_state",
        "storage_location",
        "reference_state",
    )
    list_filter = ("reference_state", "storage_location", "source_type", "format")
    actions = ("recheck_referenced_files",)

    @admin.action(description="Re-check referenced (indexed-in-place) files now")
    def recheck_referenced_files(
        self,
        request: HttpRequest,
        queryset: QuerySet[FileVariant],
    ) -> None:
        del queryset  # the check is cheap per file and covers every referenced variant
        from .referenced_files import check_referenced_files

        report = check_referenced_files()
        self.message_user(
            request,
            f"checked={report.checked} ok={report.ok} missing={len(report.missing)} "
            f"changed={len(report.changed)} restored={len(report.restored)} "
            f"errors={len(report.errors)}",
            messages.WARNING
            if report.missing or report.changed or report.errors
            else messages.SUCCESS,
        )


@admin.register(ScanJob)
class ScanJobAdmin(admin.ModelAdmin[ScanJob]):
    list_display = ("id", "state", "scanner_id", "operator", "book", "created_at", "finished_at")
    list_filter = ("state",)


@admin.register(ScanPage)
class ScanPageAdmin(admin.ModelAdmin[ScanPage]):
    list_display = ("id", "scan_job", "logical_sequence_number", "side", "updated_at")
    list_filter = ("side",)


@admin.register(ScanTailorStageState)
class ScanTailorStageStateAdmin(admin.ModelAdmin[ScanTailorStageState]):
    list_display = (
        "id",
        "scan_job",
        "scan_page",
        "stage",
        "revision",
        "status",
        "worker_job_id",
        "updated_at",
    )
    list_filter = ("stage", "status")


@admin.register(ScanPageVersion)
class ScanPageVersionAdmin(admin.ModelAdmin[ScanPageVersion]):
    list_display = (
        "scan_page",
        "version",
        "checksum_sha256",
        "is_active",
        "mime_type",
        "created_at",
    )
    list_filter = ("is_active",)


@admin.register(StorageLocation)
class StorageLocationAdmin(admin.ModelAdmin[StorageLocation]):
    list_display = ("identifier", "backend", "base_path", "read_only")
    list_filter = ("read_only", "backend")


@admin.register(SourceLocation)
class SourceLocationAdmin(admin.ModelAdmin[SourceLocation]):
    list_display = (
        "name",
        "root_path",
        "transfer_mode",
        "index_storage_location",
        "recursive",
        "default_source_type",
        "is_active",
    )
    list_filter = ("transfer_mode", "is_active")
    fieldsets = (
        (
            None,
            {
                "fields": (
                    "name",
                    "root_path",
                    "transfer_mode",
                    "index_storage_location",
                    "recursive",
                    "default_source_type",
                    "is_active",
                    "default_metadata",
                ),
            },
        ),
        (
            "Folder layout (identification hints)",
            {
                "description": (
                    "Describe how this collection is organised so folder and file names "
                    "become author/title hints for the external lookup. Check the result "
                    "with <code>manage.py ingest_sources --source-id N --preview</code>."
                ),
                "fields": ("path_layouts", "author_name_order", "layout_ignore_values"),
            },
        ),
    )


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin[ProcessingJob]):
    list_display = ("job_id", "job_type", "state", "file_variant")


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin[ImportJob]):
    list_display = ("id", "state", "source_type", "uploader", "original_filename", "created_at")
    list_filter = ("state", "source_type")


@admin.register(MetadataCandidate)
class MetadataCandidateAdmin(admin.ModelAdmin[MetadataCandidate]):
    list_display = (
        "import_job",
        "scan_job",
        "provider_id",
        "title",
        "score",
        "state",
        "created_at",
    )
    list_filter = ("provider_id", "state")


@admin.register(ReviewTask)
class ReviewTaskAdmin(admin.ModelAdmin[ReviewTask]):
    list_display = (
        "import_job",
        "scan_job",
        "task_type",
        "state",
        "decision",
        "reviewer",
        "created_by",
        "created_at",
    )
    list_filter = ("task_type", "state", "decision")
    search_fields = ("source_filename", "embedded_metadata")


@admin.register(ScanTodo)
class ScanTodoAdmin(admin.ModelAdmin[ScanTodo]):
    list_display = (
        "id",
        "scan_job",
        "scan_page",
        "todo_type",
        "state",
        "creator",
        "claimed_by",
        "created_at",
    )
    list_filter = ("todo_type", "state")
