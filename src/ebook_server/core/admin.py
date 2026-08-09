"""Admin registrations for core models."""

from __future__ import annotations

from django.contrib import admin

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
class AppUserProfileAdmin(admin.ModelAdmin):
    list_display = ("ldap_uid", "role", "is_disabled", "created_at")
    list_filter = ("role", "is_disabled")


@admin.register(Book)
class BookAdmin(admin.ModelAdmin):
    list_display = ("title", "publisher", "publication_date")
    search_fields = ("title", "subtitle", "publisher")
    filter_horizontal = ("authors", "categories", "genres")


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin):
    list_display = ("full_name",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    list_display = ("name",)


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin):
    list_display = ("name",)


@admin.register(Identifier)
class IdentifierAdmin(admin.ModelAdmin):
    list_display = ("book", "kind", "value")


@admin.register(CoverImage)
class CoverImageAdmin(admin.ModelAdmin):
    list_display = ("book", "position", "storage_identifier")


@admin.register(FileVariant)
class FileVariantAdmin(admin.ModelAdmin):
    list_display = (
        "book",
        "format",
        "original_filename",
        "source_type",
        "processing_state",
    )


@admin.register(ScanJob)
class ScanJobAdmin(admin.ModelAdmin):
    list_display = ("id", "state", "scanner_id", "operator", "book", "created_at", "finished_at")
    list_filter = ("state",)


@admin.register(ScanPage)
class ScanPageAdmin(admin.ModelAdmin):
    list_display = ("id", "scan_job", "logical_sequence_number", "side", "updated_at")
    list_filter = ("side",)


@admin.register(ScanTailorStageState)
class ScanTailorStageStateAdmin(admin.ModelAdmin):
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
class ScanPageVersionAdmin(admin.ModelAdmin):
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
class StorageLocationAdmin(admin.ModelAdmin):
    list_display = ("identifier", "backend", "base_path")


@admin.register(SourceLocation)
class SourceLocationAdmin(admin.ModelAdmin):
    list_display = (
        "name",
        "root_path",
        "transfer_mode",
        "recursive",
        "default_source_type",
        "is_active",
    )


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin):
    list_display = ("job_id", "job_type", "state", "file_variant")


@admin.register(ImportJob)
class ImportJobAdmin(admin.ModelAdmin):
    list_display = ("id", "state", "source_type", "uploader", "original_filename", "created_at")
    list_filter = ("state", "source_type")


@admin.register(MetadataCandidate)
class MetadataCandidateAdmin(admin.ModelAdmin):
    list_display = ("import_job", "provider_id", "title", "score", "state", "created_at")
    list_filter = ("provider_id", "state")


@admin.register(ReviewTask)
class ReviewTaskAdmin(admin.ModelAdmin):
    list_display = (
        "import_job",
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
class ScanTodoAdmin(admin.ModelAdmin):
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
