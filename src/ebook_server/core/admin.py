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

    @admin.action(description="Queue re-check of selected referenced (indexed-in-place) files")
    def recheck_referenced_files(
        self,
        request: HttpRequest,
        queryset: QuerySet[FileVariant],
    ) -> None:
        variant_ids = list(
            queryset.filter(storage_location__read_only=True).values_list("pk", flat=True),
        )
        if not variant_ids:
            self.message_user(
                request,
                "None of the selected file variants is referenced in place; nothing queued.",
                messages.WARNING,
            )
            return
        _queue_reference_check(self, request, variant_ids=variant_ids)


def _queue_reference_check(
    model_admin: admin.ModelAdmin[FileVariant] | admin.ModelAdmin[StorageLocation],
    request: HttpRequest,
    *,
    variant_ids: list[int] | None = None,
    storage_location_ids: list[int] | None = None,
) -> None:
    """Record a reference-check ProcessingJob and report where to follow it.

    The check stats and may hash every file in scope, so it runs in a worker, never in
    this admin request (AGENTS.md "Asynchronous Work").
    """
    from django.urls import reverse
    from django.utils.html import format_html

    from .referenced_files import ReferenceCheckDispatchError, request_reference_check

    user_id = request.user.pk if request.user.is_authenticated else None
    try:
        job, dispatched = request_reference_check(
            variant_ids=variant_ids,
            storage_location_ids=storage_location_ids,
            requested_by_user_id=user_id,
        )
    except ReferenceCheckDispatchError as exc:
        model_admin.message_user(
            request,
            f"Reference check recorded but could not be queued: {exc}",
            messages.ERROR,
        )
        return
    url = reverse("admin:core_processingjob_change", args=[job.pk])
    scope = (
        f"{len(variant_ids)} file variant(s)"
        if variant_ids is not None
        else f"{len(storage_location_ids or [])} storage location(s)"
    )
    if dispatched:
        model_admin.message_user(
            request,
            format_html(
                'Reference check of {} queued as job <a href="{}">{}</a>; '
                "its state and report appear there when a worker finishes it.",
                scope,
                url,
                job.job_id,
            ),
            messages.SUCCESS,
        )
    else:
        model_admin.message_user(
            request,
            format_html(
                'Reference check of {} recorded as job <a href="{}">{}</a>, but no worker '
                "queue is configured. Run <code>manage.py check_referenced_files "
                "--run-queued</code> to process it.",
                scope,
                url,
                job.job_id,
            ),
            messages.WARNING,
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
    list_display = ("identifier", "backend", "base_path", "read_only", "sentinel_file")
    list_filter = ("read_only", "backend")
    actions = ("recheck_location_references",)

    @admin.action(description="Queue re-check of all referenced files on selected locations")
    def recheck_location_references(
        self,
        request: HttpRequest,
        queryset: QuerySet[StorageLocation],
    ) -> None:
        location_ids = list(queryset.filter(read_only=True).values_list("pk", flat=True))
        if not location_ids:
            self.message_user(
                request,
                "None of the selected storage locations is read-only; nothing queued.",
                messages.WARNING,
            )
            return
        _queue_reference_check(self, request, storage_location_ids=location_ids)


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
        (
            "Formats of one book",
            {"fields": ("group_sibling_formats",)},
        ),
    )


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin[ProcessingJob]):
    list_display = ("job_id", "job_type", "state", "file_variant", "scheduled_at", "finished_at")
    list_filter = ("job_type", "state")


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
