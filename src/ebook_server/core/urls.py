"""Core URL routes."""

from __future__ import annotations

from django.urls import path

from . import views

urlpatterns = [
    path("healthz/", views.health),
    path("healthz/dependencies/", views.readiness),
    path("library/", views.library_home, name="library-home"),
    path("library/books/", views.library_books, name="library-books"),
    path("library/books/<int:book_id>/", views.library_book_detail, name="library-book-detail"),
    path(
        "library/books/<int:book_id>/download/",
        views.library_book_download,
        name="library-book-download",
    ),
    path("scan/jobs/", views.scan_jobs, name="scan-jobs"),
    path("scan/jobs/<uuid:job_id>/", views.scan_job_detail, name="scan-job-detail"),
    path(
        "scan/jobs/<uuid:job_id>/workspace/",
        views.scan_job_workspace,
        name="scan-job-workspace",
    ),
    path(
        "scan/jobs/<uuid:job_id>/pages/<uuid:page_id>/stages/",
        views.scan_page_stages,
        name="scan-job-page-stages",
    ),
    path(
        "scan/jobs/<uuid:job_id>/pages/<uuid:page_id>/stages/<str:stage>/",
        views.scan_page_stage,
        name="scan-job-page-stage",
    ),
    path(
        "scan/jobs/<uuid:job_id>/stages/<str:stage>/batch/",
        views.scan_job_stage_batch,
        name="scan-job-stage-batch",
    ),
    path(
        "scan/jobs/<uuid:job_id>/pages/<uuid:page_id>/stages/<str:stage>/preview/",
        views.scan_page_stage_preview,
        name="scan-job-page-stage-preview",
    ),
    path(
        "scan/jobs/<uuid:job_id>/pages/",
        views.upload_scan_page,
        name="scan-job-upload-page",
    ),
    path(
        "scan/jobs/<uuid:job_id>/pause/",
        views.scan_job_pause,
        name="scan-job-pause",
    ),
    path(
        "scan/jobs/<uuid:job_id>/resume/",
        views.scan_job_resume,
        name="scan-job-resume",
    ),
    path(
        "scan/jobs/<uuid:job_id>/finish/",
        views.scan_job_finish,
        name="scan-job-finish",
    ),
    path(
        "scan/jobs/<uuid:job_id>/cancel/",
        views.scan_job_cancel,
        name="scan-job-cancel",
    ),
    path(
        "scan/jobs/<uuid:job_id>/todos/",
        views.scan_job_todos,
        name="scan-job-todos",
    ),
    path(
        "scan/jobs/<uuid:job_id>/ocr/",
        views.scan_job_ocr_request,
        name="scan-job-ocr",
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
        views.library_book_conversion_request,
        name="library-book-convert",
    ),
    path(
        "library/books/<int:book_id>/ocr/",
        views.library_book_ocr_request,
        name="library-book-ocr",
    ),
    path(
        "conversions/<uuid:job_id>/",
        views.conversion_job_status,
        name="conversion-job-status",
    ),
    path(
        "ocr/<uuid:job_id>/",
        views.ocr_job_status,
        name="ocr-job-status",
    ),
    path(
        "stages/<uuid:job_id>/",
        views.stage_job_status,
        name="stage-job-status",
    ),
    path("admin-only/", views.admin_only, name="admin-only"),
    path("imports/upload/", views.upload_file, name="imports-upload"),
    path("imports/<uuid:job_id>/", views.import_status, name="imports-status"),
    path("reviews/", views.review_queue, name="review-queue"),
    path("reviews/<int:task_id>/", views.review_detail, name="review-detail"),
    path(
        "reviews/<int:task_id>/accept-candidate/",
        views.review_accept_candidate,
        name="review-accept-candidate",
    ),
    path(
        "reviews/<int:task_id>/manual-entry/",
        views.review_manual_entry,
        name="review-manual-entry",
    ),
    path(
        "reviews/<int:task_id>/merge/",
        views.review_merge_existing,
        name="review-merge-existing",
    ),
    path(
        "reviews/<int:task_id>/unidentified/",
        views.review_mark_unidentified,
        name="review-mark-unidentified",
    ),
    path(
        "reviews/<int:task_id>/search-again/",
        views.review_search_again,
        name="review-search-again",
    ),
    path(
        "reviews/<int:task_id>/edit-search-terms/",
        views.review_edit_search_terms,
        name="review-edit-search-terms",
    ),
]
