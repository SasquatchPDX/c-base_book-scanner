"""Admin registrations for core models."""

from __future__ import annotations

from typing import Any

from django.contrib import admin, messages
from django.db.models import QuerySet
from django.http import HttpRequest

from . import genres
from .models import (
    AppUserProfile,
    Author,
    Book,
    BookGenre,
    Category,
    ContributionEvent,
    ContributionMonthlyTotal,
    ContributionWeight,
    ContributorPreference,
    CoverImage,
    FileVariant,
    Genre,
    GenreAlias,
    Identifier,
    ImportGroup,
    ImportJob,
    LLMSuggestion,
    MetadataCandidate,
    MetadataSearch,
    ProcessingJob,
    ReviewDecisionCorrection,
    ReviewTask,
    ScanEvent,
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


class BookGenreInline(admin.TabularInline):  # type: ignore[type-arg]
    """Genres with provenance; rows added here are recorded as admin-set."""

    model = BookGenre
    extra = 0
    fields = ("genre", "source", "source_tag", "created_at")
    readonly_fields = ("source_tag", "created_at")
    autocomplete_fields = ("genre",)


@admin.register(Book)
class BookAdmin(admin.ModelAdmin[Book]):
    list_display = ("title", "publisher", "publication_year", "publication_date")
    search_fields = ("title", "subtitle", "publisher")
    filter_horizontal = ("authors", "categories")
    inlines = (BookGenreInline,)
    # archive_folder is assigned by core.archive_layout; editing it by hand would orphan
    # the book's folder.
    readonly_fields = ("genres_curated_at", "genres_curated_by", "archive_folder")
    actions = ("plan_archive_reorganize",)

    @admin.action(description="Plan moving selected books into their archive folders (dry run)")
    def plan_archive_reorganize(self, request: HttpRequest, queryset: QuerySet[Book]) -> None:
        _queue_reorganize(self, request, book_ids=list(queryset.values_list("pk", flat=True)))

    def save_formset(self, request: HttpRequest, form: Any, formset: Any, change: bool) -> None:
        super().save_formset(request, form, formset, change)
        # Any admin edit of the genre links, removing the last one included, is a human
        # decision that automatic assignment and backfills must not override.
        if formset.model is BookGenre and formset.has_changed():
            genres.mark_curated(form.instance, user=request.user)


@admin.register(Author)
class AuthorAdmin(admin.ModelAdmin[Author]):
    list_display = ("full_name",)


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin[Category]):
    list_display = ("name",)


class GenreAliasInline(admin.TabularInline):  # type: ignore[type-arg]
    model = GenreAlias
    extra = 1
    fields = ("alias", "key")
    readonly_fields = ("key",)


@admin.register(Genre)
class GenreAdmin(admin.ModelAdmin[Genre]):
    list_display = ("name",)
    search_fields = ("name", "aliases__alias")
    inlines = (GenreAliasInline,)


@admin.register(GenreAlias)
class GenreAliasAdmin(admin.ModelAdmin[GenreAlias]):
    list_display = ("alias", "genre", "key")
    search_fields = ("alias", "genre__name")
    list_select_related = ("genre",)
    readonly_fields = ("key",)


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
    list_display = (
        "id",
        "state",
        "scanner_id",
        "source",
        "operator",
        "book",
        "scan_started_at",
        "created_at",
        "finished_at",
    )
    list_filter = ("state", "source")
    search_fields = ("id", "scanner_id")


@admin.register(ScanEvent)
class ScanEventAdmin(admin.ModelAdmin[ScanEvent]):
    """scan.jsonl lines are append-only evidence: viewable, never edited here (#129)."""

    list_display = ("id", "scan_job", "origin", "line_no", "type", "received_at")
    list_filter = ("origin", "type")
    search_fields = ("scan_job__id",)
    readonly_fields = ("scan_job", "origin", "line_no", "type", "raw", "received_at")

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        # Deleting a line would open a gap in the scanner's numbering.
        return False


@admin.register(ScanLog)
class ScanLogAdmin(admin.ModelAdmin[ScanLog]):
    list_display = (
        "scan_job",
        "original_filename",
        "state",
        "size_bytes",
        "storage_location",
        "created_at",
    )
    list_filter = ("state",)
    readonly_fields = (
        "state",
        "staging_identifier",
        "storage_location",
        "storage_identifier",
        "sha256",
        "size_bytes",
        "duplicate_of",
        "processing_job",
        "error",
    )
    actions = ("retry_failed_logs",)

    @admin.action(description="Retry selected failed logs (staged upload kept)")
    def retry_failed_logs(self, request: HttpRequest, queryset: QuerySet[ScanLog]) -> None:
        from .scan_logs import ScanLogDispatchError, retry_failed_scan_log

        requeued = 0
        try:
            for log in queryset.filter(state=ScanLog.State.FAILED):
                if retry_failed_scan_log(log):
                    requeued += 1
        except ScanLogDispatchError as exc:
            self.message_user(request, f"Queue unavailable: {exc}", messages.ERROR)
            return
        self.message_user(
            request,
            f"Queued {requeued} log(s) again; failed logs without a staged upload were skipped.",
            messages.SUCCESS if requeued else messages.WARNING,
        )


@admin.register(ScanPage)
class ScanPageAdmin(admin.ModelAdmin[ScanPage]):
    list_display = (
        "id",
        "scan_job",
        "logical_sequence_number",
        "side",
        "recognized_page_number",
        "status",
        "updated_at",
    )
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
        "is_paused",
        "ingest_batch_size",
        "ingest_max_per_hour",
    )
    list_filter = ("transfer_mode", "is_active", "is_paused")
    actions = ("pause_imports", "resume_imports", "plan_sort_into_archive_tree")
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
        (
            "Ingestion throttling",
            {"fields": ("is_paused", "ingest_batch_size", "ingest_max_per_hour")},
        ),
    )

    @admin.action(description="Pause imports from selected sources")
    def pause_imports(self, request: Any, queryset: QuerySet[SourceLocation]) -> None:
        updated = queryset.update(is_paused=True)
        self.message_user(request, f"Paused {updated} source(s).")

    @admin.action(description="Resume imports from selected sources")
    def resume_imports(self, request: Any, queryset: QuerySet[SourceLocation]) -> None:
        updated = queryset.update(is_paused=False)
        self.message_user(
            request,
            f"Resumed {updated} source(s). Run ingest_sources to re-queue waiting imports.",
        )

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


@admin.register(ImportGroup)
class ImportGroupAdmin(admin.ModelAdmin[ImportGroup]):
    list_display = ("id", "kind", "state", "folder", "book", "created_at")
    list_filter = ("kind", "state")
    search_fields = ("folder",)


@admin.register(MetadataCandidate)
class MetadataCandidateAdmin(admin.ModelAdmin[MetadataCandidate]):
    list_display = (
        "import_job",
        "scan_job",
        "provider_id",
        "title",
        "score",
        "state",
        "superseded_at",
        "created_at",
    )
    list_filter = ("provider_id", "state")


@admin.register(MetadataSearch)
class MetadataSearchAdmin(admin.ModelAdmin[MetadataSearch]):
    list_display = (
        "import_job",
        "scan_job",
        "trigger",
        "state",
        "candidate_count",
        "requested_by",
        "finished_at",
    )
    list_filter = ("trigger", "state")


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


class _ReadOnlyLedgerAdmin(admin.ModelAdmin[Any]):
    """The contribution ledger is append-only; changes go through /contributions/admin/."""

    def has_add_permission(self, request: HttpRequest) -> bool:
        return False

    def has_change_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False

    def has_delete_permission(self, request: HttpRequest, obj: Any = None) -> bool:
        return False


@admin.register(ContributionEvent)
class ContributionEventAdmin(_ReadOnlyLedgerAdmin):
    list_display = ("user", "kind", "state", "points", "occurred_at", "settle_after")
    list_filter = ("kind", "state")
    search_fields = ("user__username", "outcome_key", "reason")


@admin.register(ContributionWeight)
class ContributionWeightAdmin(_ReadOnlyLedgerAdmin):
    list_display = ("version", "kind", "base_points", "per_unit_points", "cap")


@admin.register(ReviewDecisionCorrection)
class ReviewDecisionCorrectionAdmin(_ReadOnlyLedgerAdmin):
    list_display = ("review_task", "source", "found_by", "created_at")
    list_filter = ("source",)


@admin.register(ContributorPreference)
class ContributorPreferenceAdmin(_ReadOnlyLedgerAdmin):
    list_display = ("user", "leaderboard_opt_in", "display_handle", "excluded_from_leaderboard")


@admin.register(ContributionMonthlyTotal)
class ContributionMonthlyTotalAdmin(_ReadOnlyLedgerAdmin):
    list_display = ("user", "month", "kind", "points", "event_count")


@admin.register(LLMSuggestion)
class LLMSuggestionAdmin(admin.ModelAdmin[LLMSuggestion]):
    """Read-only: suggestions are evidence written by the LLM job, never edited by hand."""

    list_display = (
        "created_at",
        "import_job",
        "trigger",
        "outcome",
        "model",
        "new_candidate_count",
    )
    list_filter = ("trigger", "outcome", "model")
    readonly_fields = tuple(field.name for field in LLMSuggestion._meta.fields)

    def has_add_permission(self, request: Any) -> bool:
        return False

    def has_change_permission(self, request: Any, obj: LLMSuggestion | None = None) -> bool:
        return False
