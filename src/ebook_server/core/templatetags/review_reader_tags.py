"""Template tag rendering the review-page reader panel (issue #6)."""

from __future__ import annotations

from typing import Any

from django import template
from django.urls import reverse

from ..models import ReviewTask
from ..review_reader import reader_sources

register = template.Library()

_UNSUPPORTED_HINTS = {
    "mobi": "Kindle/MOBI files cannot be rendered in the browser.",
    "djvu": "DjVu files cannot be rendered in the browser.",
    "cbr": "RAR comic archives are not unpacked for previews.",
    "tiff": "Browsers cannot display TIFF images.",
}


@register.inclusion_tag("core/review_reader_panel.html")
def review_reader_panel(task: ReviewTask) -> dict[str, Any]:
    entries = []
    for source in reader_sources(task):
        file_url = reverse(
            "review-reader-file",
            kwargs={"task_id": task.pk, "source_key": source.key},
        )
        book_url = (
            reverse("library-book-detail", kwargs={"book_id": source.book_id}) + "?view=html"
            if source.book_id is not None
            else ""
        )
        entries.append(
            {
                "source": source,
                "kind": source.kind,
                "base_url": file_url.removesuffix("file/"),
                "download_url": f"{file_url}?download=1",
                "book_url": book_url,
                "hint": _UNSUPPORTED_HINTS.get(source.file_format.lower(), ""),
            },
        )
    return {"entries": entries}
