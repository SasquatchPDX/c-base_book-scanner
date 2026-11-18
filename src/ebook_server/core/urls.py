"""Core URL routes."""

from __future__ import annotations

from django.urls import path, register_converter
from django.views.generic import RedirectView

from . import (
    contributions_views,
    health_views,
    intake_views,
    library_views,
    member_views,
    review_views,
    views,
    worker_failure_views,
)
from .scan_job_ids import ScanJobIdConverter

# Scan job IDs are a canonical UUID (web jobs) or a scanner-assigned ID (#126).
register_converter(ScanJobIdConverter, "scan_job_id")

urlpatterns = [
    path("", RedirectView.as_view(pattern_name="library-home", permanent=False)),
    path("accounts/login/", member_views.library_login, name="library-login"),
    path("accounts/logout/", member_views.library_logout, name="library-logout"),
    path("healthz/", health_views.health),
    path("healthz/dependencies/", health_views.readiness),
    path("metrics/", health_views.prometheus_metrics, name="prometheus-metrics"),
    path("library/", library_views.library_home, name="library-home"),
    path("library/intake/", intake_views.intake_progress_view, name="intake-progress"),
    path("library/books/", library_views.library_books, name="library-books"),
    path("library/authors/", library_views.library_authors, name="library-authors"),
    path(
        "library/covers/<str:sha256>/<slug:size>.webp",
        library_views.library_cover_thumbnail,
        name="library-cover-thumbnail",
    ),
    path(
        "library/books/<int:book_id>/",
        library_views.library_book_detail,
        name="library-book-detail",
    ),
    path(
        "library/books/<int:book_id>/download/",
        library_views.library_book_download,
        name="library-book-download",
    ),
    path(
        "library/books/<int:book_id>/covers/<str:position>/",
        library_views.library_book_cover,
        name="library-book-cover",
    ),
    path(
        "library/books/<int:book_id>/delete/",
        library_views.library_book_delete,
        name="library-book-delete",
    ),
    # The scanner's QR code (#127, contract §9); portal_url is built from this route.
    path("j/<scan_job_id:job_id>", views.scan_job_portal_link, name="scan-job-portal-link"),
    path("j/<scan_job_id:job_id>/", views.scan_job_portal_link),
    path("scan/raw/", views.raw_scans, name="scan-raw-scans"),
    path("scan/jobs/", views.scan_jobs, name="scan-jobs"),
    path("scan/jobs/<scan_job_id:job_id>/", views.scan_job_detail, name="scan-job-detail"),
    path(
        "scan/jobs/<scan_job_id:job_id>/workspace/",
        views.scan_job_workspace,
        name="scan-job-workspace",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/editor/",
        views.scan_job_editor,
        name="scan-job-editor",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/workspace/state/",
        views.scan_job_workspace_state,
        name="scan-job-workspace-state",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/workspace/events/",
        views.scan_job_workspace_events,
        name="scan-job-workspace-events",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/pages/<uuid:page_id>/stages/",
        views.scan_page_stages,
        name="scan-job-page-stages",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/pages/<uuid:page_id>/stages/<str:stage>/",
        views.scan_page_stage,
        name="scan-job-page-stage",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/stages/<str:stage>/batch/",
        views.scan_job_stage_batch,
        name="scan-job-stage-batch",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/pages/<uuid:page_id>/stages/<str:stage>/preview/",
        views.scan_page_stage_preview,
        name="scan-job-page-stage-preview",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/pages/<uuid:page_id>/stages/<str:stage>/thumbnail/",
        views.scan_page_stage_thumbnail,
        name="scan-job-page-stage-thumbnail",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/pages/",
        views.upload_scan_page,
        name="scan-job-upload-page",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/logs/",
        views.upload_scan_log,
        name="scan-job-upload-log",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/pause/",
        views.scan_job_pause,
        name="scan-job-pause",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/resume/",
        views.scan_job_resume,
        name="scan-job-resume",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/finish/",
        views.scan_job_finish,
        name="scan-job-finish",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/cancel/",
        views.scan_job_cancel,
        name="scan-job-cancel",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/todos/",
        views.scan_job_todos,
        name="scan-job-todos",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/ocr/",
        views.scan_job_ocr_request,
        name="scan-job-ocr",
    ),
    path(
        "scan/jobs/<scan_job_id:job_id>/identify/",
        views.scan_job_identification_request,
        name="scan-job-identify",
    ),
    path("scan/pages/<uuid:page_id>/", views.scan_page, name="scan-page"),
    path(
        "scan/pages/<uuid:page_id>/rescan/",
        views.scan_page_request_rescan,
        name="scan-page-rescan",
    ),
    path(
        "scan/pages/<uuid:page_id>/image/",
        views.scan_page_thumbnail,
        name="scan-page-thumbnail",
    ),
    path(
        "scan/pages/<uuid:page_id>/thumbnail/",
        views.scan_page_small_thumbnail,
        name="scan-page-small-thumbnail",
    ),
    path("scan/todos/", views.scan_todos, name="scan-todos"),
    path("scan/todos/<int:todo_id>/claim/", views.scan_todo_claim, name="scan-todo-claim"),
    path(
        "scan/todos/<int:todo_id>/complete/",
        views.scan_todo_complete,
        name="scan-todo-complete",
    ),
    path("scan/todos/<int:todo_id>/cancel/", views.scan_todo_cancel, name="scan-todo-cancel"),
    path(
        "library/books/<int:book_id>/convert/",
        library_views.library_book_conversion_request,
        name="library-book-convert",
    ),
    path(
        "library/books/<int:book_id>/ocr/",
        library_views.library_book_ocr_request,
        name="library-book-ocr",
    ),
    path(
        "library/books/<int:book_id>/combine-parts/",
        library_views.library_book_combine_parts,
        name="library-book-combine-parts",
    ),
    path(
        "combine-jobs/<uuid:job_id>/",
        library_views.combine_job_status,
        name="combine-job-status",
    ),
    path(
        "conversions/<uuid:job_id>/",
        library_views.conversion_job_status,
        name="conversion-job-status",
    ),
    path(
        "ocr/<uuid:job_id>/",
        library_views.ocr_job_status,
        name="ocr-job-status",
    ),
    path(
        "stages/<uuid:job_id>/",
        views.stage_job_status,
        name="stage-job-status",
    ),
    path(
        "scan-finalizations/<uuid:job_id>/",
        views.scan_finalization_job_status,
        name="scan-finalization-job-status",
    ),
    path(
        "scan-identifications/<uuid:job_id>/",
        views.scan_identification_job_status,
        name="scan-identification-job-status",
    ),
    path("admin-only/", member_views.admin_only, name="admin-only"),
    path("imports/upload/", intake_views.upload_file, name="imports-upload"),
    path("imports/<uuid:job_id>/", intake_views.import_status, name="imports-status"),
    path("reviews/", review_views.review_queue, name="review-queue"),
    path("reviews/batch-accept/", review_views.review_batch_accept, name="review-batch-accept"),
    path("reviews/<int:task_id>/", review_views.review_detail, name="review-detail"),
    path(
        "reviews/<int:task_id>/worker-recovery/",
        worker_failure_views.recover,
        name="review-worker-recovery",
    ),
    path(
        "reviews/<int:task_id>/accept-candidate/",
        review_views.review_accept_candidate,
        name="review-accept-candidate",
    ),
    path(
        "reviews/<int:task_id>/manual-entry/",
        review_views.review_manual_entry,
        name="review-manual-entry",
    ),
    path(
        "reviews/<int:task_id>/scanner-opf/",
        review_views.review_scanner_opf,
        name="review-scanner-opf",
    ),
    path(
        "reviews/<int:task_id>/merge/",
        review_views.review_merge_existing,
        name="review-merge-existing",
    ),
    path(
        "reviews/<int:task_id>/unidentified/",
        review_views.review_mark_unidentified,
        name="review-mark-unidentified",
    ),
    path(
        "reviews/<int:task_id>/search-again/",
        review_views.review_search_again,
        name="review-search-again",
    ),
    path(
        "reviews/<int:task_id>/isbn-lookup/",
        review_views.review_isbn_lookup,
        name="review-isbn-lookup",
    ),
    path(
        "reviews/<int:task_id>/edit-search-terms/",
        review_views.review_edit_search_terms,
        name="review-edit-search-terms",
    ),
    path(
        "reviews/<int:task_id>/ask-llm/",
        review_views.review_ask_llm,
        name="review-ask-llm",
    ),
    path(
        "llm-identifications/<uuid:job_id>/",
        review_views.llm_identification_job_status,
        name="llm-identification-job-status",
    ),
    path(
        "reviews/<int:task_id>/multipart/order/",
        review_views.review_multipart_order,
        name="review-multipart-order",
    ),
    path(
        "reviews/<int:task_id>/multipart/acknowledge-incomplete/",
        review_views.review_multipart_acknowledge_incomplete,
        name="review-multipart-acknowledge-incomplete",
    ),
    path(
        "reviews/<int:task_id>/multipart/reject/",
        review_views.review_multipart_reject,
        name="review-multipart-reject",
    ),
    path(
        "review-identifications/<uuid:job_id>/",
        review_views.review_identification_job_status,
        name="review-identification-job-status",
    ),
    path(
        "me/contributions/",
        contributions_views.my_contributions,
        name="my-contributions",
    ),
    path(
        "me/contributions/leaderboard/",
        contributions_views.my_leaderboard_preference,
        name="my-contributions-leaderboard",
    ),
    path(
        "me/contributions/export/",
        contributions_views.my_contributions_export,
        name="my-contributions-export",
    ),
    path(
        "me/contributions/erase/",
        contributions_views.my_contributions_erase,
        name="my-contributions-erase",
    ),
    path(
        "contributions/stats/",
        contributions_views.collective_stats,
        name="contributions-stats",
    ),
    path(
        "contributions/leaderboard/",
        contributions_views.leaderboard,
        name="contributions-leaderboard",
    ),
    path(
        "contributions/admin/",
        contributions_views.contributions_admin,
        name="contributions-admin",
    ),
    path(
        "contributions/admin/users/<str:username>/export/",
        contributions_views.contributions_admin_export,
        name="contributions-admin-export",
    ),
]
