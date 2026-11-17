"""Routes for the review-page reader panel (issue #6)."""

from __future__ import annotations

from django.urls import path

from . import review_reader_views

_PREFIX = "reviews/<int:task_id>/reader/<slug:source_key>/"

urlpatterns = [
    path(f"{_PREFIX}file/", review_reader_views.review_reader_file, name="review-reader-file"),
    path(f"{_PREFIX}text/", review_reader_views.review_reader_text, name="review-reader-text"),
    path(f"{_PREFIX}parts/", review_reader_views.review_reader_parts, name="review-reader-parts"),
    path(
        f"{_PREFIX}document/",
        review_reader_views.review_reader_document,
        name="review-reader-document",
    ),
    path(
        f"{_PREFIX}pages/<int:page>/",
        review_reader_views.review_reader_page_image,
        name="review-reader-page-image",
    ),
]
