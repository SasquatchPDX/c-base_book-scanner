"""Admin registrations for core models."""

from __future__ import annotations

from typing import Any

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
    ScanLog,
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
    # Assigned by core.archive_layout; editing it by hand would orphan the book's folder.
    readonly_fields = ("archive_folder",)
    actions = ("plan_archive_reorganize",)

    @admin.action(description="Plan moving selected books into their archive folders (dry run)")
    def plan_archive_reorganize(self, request: HttpRequest, queryset: QuerySet[Book]) -> None:
        _queue_reorganize(self, request, book_ids=list(queryset.values_list("pk", flat=True)))


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


@admin.register(ScanLog)
class ScanLogAdmin(admin.ModelAdmin[ScanLog]):
    list_display = ("scan_job", "original_filename", "size_bytes", "storage_location", "created_at")
    readonly_fields = ("storage_location", "storage_identifier", "sha256", "size_bytes")


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
    actions = ("plan_sort_into_archive_tree",)

    @admin.action(
        description="Plan sorting books imported from these sources into the archive tree (dry run)"
    )
    def plan_sort_into_archive_tree(
        self,
        request: HttpRequest,
        queryset: QuerySet[SourceLocation],
    ) -> None:
        _queue_reorganize(
            self,
            request,
            source_location_ids=list(queryset.values_list("pk", flat=True)),
        )


REORGANIZE_REPORT_ROWS = 500


def _queue_reorganize(
    model_admin: Any,
    request: HttpRequest,
    *,
    book_ids: list[int] | None = None,
    source_location_ids: list[int] | None = None,
    apply_plan_job: ProcessingJob | None = None,
) -> None:
    """Record an archive-reorganize ProcessingJob; planning and moving run in a worker."""
    from django.urls import reverse
    from django.utils.html import format_html

    from .archive_reorganize import ReorganizeDispatchError, ReorganizeError, request_reorganize

    user_id = request.user.pk if request.user.is_authenticated else None
    try:
        job, dispatched = request_reorganize(
            book_ids=book_ids,
            source_location_ids=source_location_ids,
            apply_plan_job=apply_plan_job,
            requested_by_user_id=user_id,
        )
    except ReorganizeError as exc:
        model_admin.message_user(request, f"Not queued: {exc}", messages.ERROR)
        return
    except ReorganizeDispatchError as exc:
        model_admin.message_user(
            request, f"Recorded but could not be queued: {exc}", messages.ERROR
        )
        return
    url = reverse("admin:core_processingjob_change", args=[job.pk])
    what = "Applying the plan" if apply_plan_job is not None else "Dry-run plan"
    if dispatched:
        model_admin.message_user(
            request,
            format_html(
                '{} queued as job <a href="{}">{}</a>. Review it there, then apply a plan '
                'with the "Apply selected archive reorganize plans" action.',
                what,
                url,
                job.job_id,
            ),
            messages.SUCCESS,
        )
    else:
        model_admin.message_user(
            request,
            format_html(
                '{} recorded as job <a href="{}">{}</a>, but no worker queue is configured. '
                "Run <code>manage.py reorganize_archive --run-queued</code>.",
                what,
                url,
                job.job_id,
            ),
            messages.WARNING,
        )


@admin.register(ProcessingJob)
class ProcessingJobAdmin(admin.ModelAdmin[ProcessingJob]):
    list_display = ("job_id", "job_type", "state", "file_variant", "scheduled_at", "finished_at")
    list_filter = ("job_type", "state")
    readonly_fields = ("reorganize_report",)
    actions = ("apply_reorganize_plan",)

    @admin.display(description="Archive reorganize")
    def reorganize_report(self, obj: ProcessingJob) -> str:
        from django.utils.html import format_html, format_html_join
        from django.utils.safestring import mark_safe

        if obj.job_type != ProcessingJob.JobType.ARCHIVE_REORGANIZE:
            return "-"
        details = obj.details if isinstance(obj.details, dict) else {}
        is_plan = details.get("mode") == "plan"
        rows = list((details.get("plan") if is_plan else details.get("results")) or [])
        shown = rows[:REORGANIZE_REPORT_ROWS]
        if is_plan:
            head = mark_safe("<tr><th>File</th><th>From</th><th>To</th></tr>")
            body = format_html_join(
                "",
                "<tr><td>{} {}</td><td>{}</td><td>{}/{}</td></tr>",
                (
                    (r.get("kind"), r.get("id"), r.get("from"), r.get("to_folder"), r.get("name"))
                    for r in shown
                ),
            )
        else:
            head = mark_safe("<tr><th>File</th><th>Status</th><th>Detail</th></tr>")
            body = format_html_join(
                "",
                "<tr><td>{} {}</td><td>{}</td><td>{}</td></tr>",
                ((r.get("kind"), r.get("id"), r.get("status"), r.get("detail")) for r in shown),
            )
        more = len(rows) - len(shown)
        return format_html(
            "<p>{}</p><table>{}{}</table><p>{}</p>",
            details.get("summary", ""),
            head,
            body,
            f"... and {more} more" if more > 0 else "",
        )

    @admin.action(description="Apply selected archive reorganize plans (moves files)")
    def apply_reorganize_plan(
        self,
        request: HttpRequest,
        queryset: QuerySet[ProcessingJob],
    ) -> None:
        for plan_job in queryset:
            _queue_reorganize(self, request, apply_plan_job=plan_job)


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
