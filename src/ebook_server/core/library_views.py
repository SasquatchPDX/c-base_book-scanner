"""Library browse, downloads, and conversion and OCR requests."""

from __future__ import annotations

import re
import time
from typing import Any, Final
from urllib.parse import urlencode

import structlog
from django.conf import settings as django_settings
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Prefetch
from django.http import (
    FileResponse,
    HttpRequest,
    HttpResponse,
    HttpResponseBadRequest,
    HttpResponseForbidden,
    HttpResponseNotFound,
    JsonResponse,
)
from django.http.response import HttpResponseBase
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import (
    book_deletion,
    combine,
    conversion,
    library_visibility,
    ocr,
    search,
)
from . import thumbnails as thumbnails_module
from .models import (
    Author,
    Book,
    Category,
    CoverImage,
    CoverThumbnail,
    FileVariant,
    Genre,
    ProcessingJob,
)
from .referenced_files import ReferencedFileError, ensure_reference_intact
from .storage_registry import StorageConfigurationError, backend_for_location
from .view_helpers import (
    _active_library_profile,
    _as_positive_int,
    _coerce_optional_bool,
    _profile_for_request,
    _request_payload,
    _wants_html,
)

logger = structlog.get_logger(__name__)


@require_GET
@login_required
def library_home(request: HttpRequest) -> HttpResponseBase:
    profile = _profile_for_request(request)
    if profile is None:
        return JsonResponse({"detail": "profile_missing"}, status=404)
    if profile.is_disabled:
        return JsonResponse({"detail": "forbidden"}, status=403)
    if _wants_html(request):
        started = time.perf_counter()
        context = _library_landing_context()
        logger.info(
            "library_landing",
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
        )
        return render(request, "core/library_home.html", {"profile": profile, **context})
    return JsonResponse(
        {"status": "ok", "user": request.user.username, "role": profile.role},
    )


SHELF_SIZE: Final = 12
# Facets of the landing page sidebar, in display order, with their headings.
LANDING_FACETS: Final[tuple[tuple[str, str], ...]] = (
    ("format", "Format"),
    ("language", "Language"),
    ("genre", "Genre"),
    ("category", "Category"),
    ("author", "Author"),
    ("decade", "Decade"),
    ("has_cover", "Cover"),
)
LANDING_FACET_ITEMS: Final = 8
# Query parameter each facet's links set on `/library/books/`.
FACET_FILTER_PARAMS: Final[dict[str, str]] = {
    "format": "format",
    "language": "language",
    "genre": "genre_id",
    "category": "category_id",
    "author": "author_id",
    "decade": "decade",
    "has_cover": "has_cover",
}


def _facet_label(facet: str, item: search.FacetCount) -> str:
    if facet == "format":
        return item.value.upper()
    if facet == "language":
        return item.value.upper() if len(item.value) <= 3 else item.value
    if facet == "decade":
        return f"{item.value}s"
    if facet == "has_cover":
        return "With cover" if item.value == "true" else "Without cover"
    return item.value


def _facet_links(facets: dict[str, list[search.FacetCount]]) -> list[dict[str, Any]]:
    """Sidebar groups for the template; facets without values are hidden (#50)."""
    groups = []
    for facet, heading in LANDING_FACETS:
        items = facets.get(facet, [])[:LANDING_FACET_ITEMS]
        if not items:
            continue
        param = FACET_FILTER_PARAMS[facet]
        groups.append(
            {
                "name": facet,
                "heading": heading,
                "items": [
                    {
                        "label": _facet_label(facet, item),
                        "count": item.count,
                        "query": urlencode({"view": "html", param: item.filter_key}),
                    }
                    for item in items
                ],
            }
        )
    return groups


def _language_shelf_ids(
    languages: list[search.FacetCount],
) -> list[tuple[str, str, list[int]]]:
    """ "New in <language>" shelves, cached with the global facets.

    The most common language is skipped: its shelf would mostly repeat "Recently
    added". The next two get a shelf when the library has more than one language.
    """
    cached = cache.get(search.LANGUAGE_SHELVES_CACHE_KEY)
    if cached is not None:
        return list(cached)
    shelves = [
        (item.value, item.filter_key, search.newest_book_ids(limit=SHELF_SIZE, language=item.value))
        for item in languages[1:3]
    ]
    cache.set(search.LANGUAGE_SHELVES_CACHE_KEY, shelves, search.GLOBAL_CACHE_SECONDS)
    return shelves


def _shelf_books(book_ids: list[int]) -> dict[int, Book]:
    """Shelf cards need title, authors and the cover thumbnail: two queries in all."""
    if not book_ids:
        return {}
    # Raw-scan books (#127) never appear on a shelf, whatever the id source.
    return thumbnails_module.with_thumbnail_refs(
        library_visibility.visible_books().prefetch_related("authors")
    ).in_bulk(book_ids)


def _library_landing_context() -> dict[str, Any]:
    """Search-first landing page (#28 Q1): search box, global facets, shelves, indexes.

    Global facet counts, the total and the language shelves come from a
    five-minute cache; "Recently added" and "Recently identified" are live.
    """
    facets = search.global_facets([facet for facet, _heading in LANDING_FACETS])
    recent_ids = search.newest_book_ids(limit=SHELF_SIZE)
    identified_ids = search.recently_identified_book_ids(limit=SHELF_SIZE)
    language_shelves = _language_shelf_ids(facets.get("language", []))
    books = _shelf_books(
        list(
            dict.fromkeys(
                [
                    *recent_ids,
                    *identified_ids,
                    *(i for _l, _k, ids in language_shelves for i in ids),
                ]
            )
        )
    )

    def cards(ids: list[int]) -> list[dict[str, Any]]:
        return [_shelf_card(books[book_id]) for book_id in ids if book_id in books]

    shelves: list[dict[str, Any]] = [
        {
            "id": "recently-added",
            "title": "Recently added",
            "books": cards(recent_ids),
            "see_all": urlencode({"view": "html", "sort": "-created_at"}),
        },
        {
            "id": "recently-identified",
            "title": "Recently identified",
            "books": cards(identified_ids),
            "see_all": None,
        },
    ]
    for value, key, ids in language_shelves:
        shelves.append(
            {
                "id": f"new-in-{key}",
                "title": f"New in {value.upper() if len(value) <= 3 else value}",
                "books": cards(ids),
                "see_all": urlencode({"view": "html", "language": key, "sort": "-created_at"}),
            }
        )
    return {
        "book_count": (total := search.total_books()),
        "book_count_display": f"{total:,}",
        "facet_groups": _facet_links(facets),
        "shelves": [shelf for shelf in shelves if shelf["books"]],
        "letters": AUTHOR_INDEX_LETTERS,
    }


def _shelf_card(book: Book) -> dict[str, Any]:
    return {
        "id": book.id,
        "title": book.title,
        "authors": [author.full_name for author in book.authors.all()],
        "year": book.published_year,
        "cover_image": _cover_image_payload(book),
    }


def _cover_image_payload(book: Book) -> dict[str, Any] | None:
    """What an `<img>` for the front cover needs: thumbnail srcset or the full cover.

    Thumbnails come from the `with_thumbnail_refs` annotation of the list query,
    so rendering never queries per book. Without a current thumbnail (not yet
    generated, or the cover could not be decoded) the full cover is used.
    """
    if not book.front_cover_id:
        return None
    refs = thumbnails_module.annotated_thumbnails(book) or {}
    medium = refs.get(CoverThumbnail.Size.MEDIUM)
    small = refs.get(CoverThumbnail.Size.SMALL)
    primary = medium or small
    if primary is None:
        return {
            "src": f"/library/books/{book.id}/covers/front/",
            "srcset": "",
            "width": 240,
            "height": 320,
            "thumbnail": False,
        }
    return {
        "src": primary.url,
        "small_src": (small or primary).url,
        "srcset": ", ".join(f"{ref.url} {ref.width}w" for ref in (small, medium) if ref),
        "width": primary.width,
        "height": primary.height,
        "thumbnail": True,
    }


AUTHOR_INDEX_LETTERS: Final = tuple("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
AUTHOR_INDEX_PAGE_SIZE: Final = 60


@require_GET
@login_required
def library_authors(request: HttpRequest) -> HttpResponseBase:
    """Author A-Z index (#28 Q1): authors with books, keyset-paginated by name."""
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    letter = request.GET.get("letter", "").strip()[:1].upper()
    if letter and letter not in AUTHOR_INDEX_LETTERS:
        letter = ""
    page_size = min(_as_positive_int(request.GET.get("page_size"), AUTHOR_INDEX_PAGE_SIZE), 200)
    try:
        page = search.author_index_page(
            letter=letter,
            cursor=request.GET.get("cursor", "").strip(),
            page_size=page_size,
        )
    except search.InvalidCursorError:
        return JsonResponse({"detail": "invalid_cursor"}, status=400)

    def page_url(cursor: str | None) -> str | None:
        if not cursor:
            return None
        params = {"cursor": cursor}
        if _wants_html(request):
            params["view"] = "html"
        if "page_size" in request.GET:
            params["page_size"] = str(page_size)
        return f"?{urlencode(params)}"

    if _wants_html(request):
        return render(
            request,
            "core/library_authors.html",
            {
                "authors": page.authors,
                "letter": letter,
                "letters": AUTHOR_INDEX_LETTERS,
                "next_url": page_url(page.next_cursor),
                "previous_url": page_url(page.previous_cursor),
            },
        )
    return JsonResponse(
        {
            "items": [
                {"id": entry.id, "name": entry.name, "book_count": entry.book_count}
                for entry in page.authors
            ],
            "next_cursor": page.next_cursor,
            "previous_cursor": page.previous_cursor,
        }
    )


_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")


@require_GET
@login_required
def library_cover_thumbnail(request: HttpRequest, sha256: str, size: str) -> HttpResponseBase:
    """A content-addressed cover thumbnail, cacheable forever by the member's browser.

    The permission check always runs here. With
    `BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX` set, nginx sends the bytes
    (`X-Accel-Redirect` to an `internal` location) instead of a Python worker.
    """
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    if size not in thumbnails_module.THUMBNAIL_WIDTHS or not _SHA256_RE.fullmatch(sha256):
        return HttpResponseNotFound()
    thumbnail = (
        CoverThumbnail.objects.select_related("storage_location")
        .filter(sha256=sha256, size=size)
        .first()
    )
    if thumbnail is None:
        return HttpResponseNotFound("thumbnail not available")

    etag = f'"{thumbnail.sha256}"'
    headers = {
        "Cache-Control": thumbnails_module.IMMUTABLE_CACHE_CONTROL,
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if etag in request.headers.get("If-None-Match", ""):
        not_modified = HttpResponse(status=304)
        for name, value in headers.items():
            not_modified[name] = value
        return not_modified

    accel_prefix = str(
        getattr(django_settings, "BOOK_SCANNER_COVER_THUMBNAIL_ACCEL_PREFIX", "") or ""
    )
    response: HttpResponseBase
    if accel_prefix:
        response = HttpResponse(content_type=thumbnail.mime_type)
        response["X-Accel-Redirect"] = f"{accel_prefix.rstrip('/')}/{thumbnail.storage_identifier}"
    else:
        try:
            storage = backend_for_location(thumbnail.storage_location)
            stream = storage.open(thumbnail.storage_identifier)
        except StorageConfigurationError:
            return HttpResponse("thumbnail storage unavailable", status=503)
        except FileNotFoundError:
            return HttpResponseNotFound("thumbnail missing")
        except OSError:
            return HttpResponse("storage unavailable", status=503)
        response = FileResponse(stream, as_attachment=False, content_type=thumbnail.mime_type)
        response["Content-Length"] = str(thumbnail.size_bytes)
    for name, value in headers.items():
        response[name] = value
    return response


# Attribute that `_book_list_prefetches` fills with the book's COMPLETE variants.
_COMPLETE_VARIANTS_ATTR = "complete_file_variants"


def _book_list_prefetches() -> list[str | Prefetch[Any]]:
    """Prefetches that let `_serialize_book` render a list row without extra queries.

    The COMPLETE variants are prefetched once for the whole page into
    `_COMPLETE_VARIANTS_ATTR`, so `formats_available` is not a per-book query (#28).
    """
    return [
        "authors",
        "categories",
        "genres",
        Prefetch(
            "file_variants",
            queryset=library_visibility.without_pending_scan_variants(
                FileVariant.objects.filter(
                    processing_state=FileVariant.ProcessingState.COMPLETE,
                )
            ).only("id", "book_id", "format", "processing_state"),
            to_attr=_COMPLETE_VARIANTS_ATTR,
        ),
    ]


def _complete_variants(book: Book) -> list[FileVariant]:
    prefetched: list[FileVariant] | None = getattr(book, _COMPLETE_VARIANTS_ATTR, None)
    if prefetched is not None:
        return prefetched
    # `.all()` reuses a plain `file_variants` prefetch when there is one; a
    # `.filter()` here would bypass it and cost one query per book (#28).
    variants = list(book.file_variants.all())
    hidden = _pending_scan_variant_ids(book, variants)
    return [
        variant
        for variant in variants
        if variant.processing_state == FileVariant.ProcessingState.COMPLETE
        and variant.id not in hidden
    ]


def _pending_scan_variant_ids(book: Book, variants: list[FileVariant]) -> set[int]:
    """Cleaned variants of ``book`` built by a scan job that is not finished (#127)."""
    cleaned = {
        variant.id for variant in variants if variant.source_type == FileVariant.SourceType.CLEANED
    }
    if not cleaned:
        return set()
    shown = library_visibility.without_pending_scan_variants(
        FileVariant.objects.filter(pk__in=cleaned)
    ).values_list("id", flat=True)
    return cleaned - set(shown)


def _serialize_book(
    book: Book,
    *,
    include_variants: bool = False,
    include_conversion_options: bool = False,
) -> dict[str, Any]:
    book_payload: dict[str, Any] = {
        "id": book.id,
        "title": book.title,
        "subtitle": book.subtitle,
        "publisher": book.publisher,
        "edition": book.edition,
        "language": book.language,
        "description": book.description,
        # Year-only data (the usual case) leaves publication_date null (#38).
        "publication_year": book.published_year,
        "publication_date": book.publication_date.isoformat() if book.publication_date else None,
        "authors": [author.full_name for author in book.authors.all()],
        "categories": [category.name for category in book.categories.all()],
        "genres": [genre.name for genre in book.genres.all()],
        "created_at": book.created_at.isoformat(),
        "updated_at": book.updated_at.isoformat(),
        "cover_front": None,
        "cover_back": None,
    }

    if book.front_cover_id and book.front_cover is not None:
        book_payload["cover_front"] = _serialize_cover(book.front_cover)
        refs = thumbnails_module.annotated_thumbnails(book)
        if refs is not None:  # list queries annotate the current thumbnails (#28)
            book_payload["cover_front"]["thumbnails"] = {
                size: ref.url for size, ref in refs.items()
            }
    if book.back_cover_id and book.back_cover is not None:
        book_payload["cover_back"] = _serialize_cover(book.back_cover)
    if include_variants:
        file_variants = sorted(
            _complete_variants(book),
            # Complete representations first, then the parts of a split work in order.
            key=lambda variant: (
                variant.part_index is not None,
                variant.part_index or 0,
                variant.id,
            ),
        )
        variants = [
            {
                "id": file_variant.id,
                "format": file_variant.format,
                "mime_type": file_variant.mime_type,
                "size_bytes": file_variant.size_bytes,
                "original_filename": file_variant.original_filename,
                "source_type": file_variant.source_type,
                "imported_at": (
                    file_variant.imported_at.isoformat() if file_variant.imported_at else None
                ),
                "part_index": file_variant.part_index,
                "part_label": file_variant.part_label,
            }
            for file_variant in file_variants
        ]
        book_payload["variants"] = variants
        book_payload["formats_available"] = sorted(
            {file_variant.format for file_variant in file_variants}
        )
    else:
        book_payload["formats_available"] = sorted(
            {variant.format for variant in _complete_variants(book)}
        )
    if include_conversion_options:
        book_payload["conversion_options"] = conversion.conversion_options_for_book(book)
    return book_payload


def _serialize_cover(cover: Any) -> dict[str, Any]:
    if cover is None:
        return {}
    return {
        "id": cover.id,
        "position": cover.position,
        "mime_type": cover.mime_type,
        "size_bytes": cover.size_bytes,
        "source_variant_id": cover.source_variant_id,
        "extractor_tool": cover.extractor_tool,
        "extractor_version": cover.extractor_version,
        "url": f"/library/books/{cover.book_id}/covers/{cover.position}/",
    }


_LIBRARY_FILTER_PARAMS: Final = (
    ("q", "query"),
    ("title", "title"),
    ("author", "author"),
    ("category", "category"),
    ("genre", "genre"),
    ("language", "language"),
    ("format", "book_format"),
)


def _optional_id(raw: str | None) -> int | None:
    value = (raw or "").strip()
    return int(value) if value.isdigit() and 0 < len(value) <= 18 else None


def _optional_decade(raw: str | None) -> int | None:
    value = (raw or "").strip().removesuffix("s")
    if not value.isdigit() or len(value) > 4:
        return None
    return (int(value) // 10) * 10


def _optional_flag(raw: str | None) -> bool | None:
    value = (raw or "").strip().lower()
    if value in ("1", "true", "yes"):
        return True
    if value in ("0", "false", "no"):
        return False
    return None


def _active_filter_chips(
    request: HttpRequest, search_request: search.SearchRequest
) -> list[dict[str, str]]:
    """Removable labels for the filters in effect (one name query per entity filter)."""
    chips: list[tuple[str, str, str]] = []
    for param, label, value in (
        ("q", "Search", search_request.query),
        ("title", "Title", search_request.title),
        ("author", "Author", search_request.author),
        ("category", "Category", search_request.category),
        ("genre", "Genre", search_request.genre),
        ("language", "Language", search_request.language),
        ("format", "Format", search_request.book_format.upper()),
    ):
        if value:
            chips.append((param, label, f"{label}: {value}"))
    for param, label, model, field_name, entity_id in (
        ("author_id", "Author", Author, "full_name", search_request.author_id),
        ("genre_id", "Genre", Genre, "name", search_request.genre_id),
        ("category_id", "Category", Category, "name", search_request.category_id),
    ):
        if entity_id is not None:
            name = model.objects.filter(pk=entity_id).values_list(field_name, flat=True).first()
            chips.append((param, label, f"{label}: {name or 'unknown'}"))
    if search_request.decade is not None:
        chips.append(("decade", "Decade", f"Decade: {search_request.decade}s"))
    if search_request.has_cover is not None:
        chips.append(
            ("has_cover", "Cover", "With cover" if search_request.has_cover else "Without cover")
        )
    result = []
    for param, name, text in chips:
        params = request.GET.copy()
        for dropped in (param, "cursor", "page"):
            params.pop(dropped, None)
        result.append({"label": text, "name": name.lower(), "remove_url": f"?{params.urlencode()}"})
    return result


def _library_search_request(request: HttpRequest) -> search.SearchRequest:
    raw_page = request.GET.get("page")
    page_size = min(_as_positive_int(request.GET.get("page_size"), 25), 100)
    filters = {
        attribute: request.GET.get(param, "").strip() for param, attribute in _LIBRARY_FILTER_PARAMS
    }
    return search.SearchRequest(
        query=filters["query"],
        title=filters["title"],
        author=filters["author"],
        category=filters["category"],
        genre=filters["genre"],
        language=filters["language"],
        book_format=filters["book_format"],
        author_id=_optional_id(request.GET.get("author_id")),
        genre_id=_optional_id(request.GET.get("genre_id")),
        category_id=_optional_id(request.GET.get("category_id")),
        decade=_optional_decade(request.GET.get("decade")),
        has_cover=_optional_flag(request.GET.get("has_cover")),
        sort=request.GET.get("sort", "").strip(),
        cursor=request.GET.get("cursor", "").strip(),
        page=_as_positive_int(raw_page, 1) if raw_page is not None else None,
        page_size=page_size,
    )


def _library_books_url(request: HttpRequest, **changes: str) -> str:
    params = request.GET.copy()
    for name in ("cursor", "page"):
        params.pop(name, None)
    for name, value in changes.items():
        params[name] = value
    return f"?{params.urlencode()}"


def _next_page_url(request: HttpRequest, result: search.SearchPage, page_size: int) -> str | None:
    if result.next_cursor:
        return _library_books_url(request, cursor=result.next_cursor)
    if result.page is not None and not result.previous_cursor:
        cap = min(result.count, int(result.extra.get("max_results", result.count)))
        if result.page * page_size < cap:
            return _library_books_url(request, page=str(result.page + 1))
    return None


def _previous_page_url(request: HttpRequest, result: search.SearchPage) -> str | None:
    if result.previous_cursor:
        return _library_books_url(request, cursor=result.previous_cursor)
    if result.page is not None and result.page > 1:
        return _library_books_url(request, page=str(result.page - 1))
    return None


@require_GET
@login_required
def library_books(request: HttpRequest) -> HttpResponseBase:
    """Browse/search the library (#28): keyset cursors for browsing, ranked search by offset.

    `?cursor=` (from `next_cursor`/`previous_cursor`) pages at constant cost at
    any depth. `?page=` still works for one release and is marked deprecated
    for browse sorts. `?facets=language,format,...` adds facet counts.
    """
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    search_request = _library_search_request(request)
    backend = search.get_search_backend()
    started = time.perf_counter()
    try:
        result = backend.search(search_request)
    except search.InvalidCursorError:
        return JsonResponse({"detail": "invalid_cursor"}, status=400)
    books_by_id = thumbnails_module.with_thumbnail_refs(
        library_visibility.visible_books()
        .select_related("front_cover", "back_cover")
        .prefetch_related(*_book_list_prefetches())
    ).in_bulk(result.book_ids)
    serialized = [
        _serialize_book(books_by_id[book_id], include_variants=False)
        for book_id in result.book_ids
        if book_id in books_by_id
    ]
    requested_facets = [
        name.strip()
        for name in request.GET.get("facets", "").split(",")
        if name.strip() in search.FACETS
    ]
    facet_payload = {
        name: [
            {"value": item.value, "count": item.count, "key": item.filter_key} for item in counts
        ]
        for name, counts in (
            backend.facet_counts(search_request, requested_facets).items()
            if requested_facets
            else ()
        )
    }
    # Never log the query text itself (#28 privacy).
    logger.info(
        "library_search",
        backend=result.backend,
        kind="search" if search_request.query else "browse",
        sort=result.sort,
        result_count=result.count,
        fuzzy=result.fuzzy,
        duration_ms=round((time.perf_counter() - started) * 1000, 1),
    )
    if _wants_html(request):
        for book_payload in serialized:
            book_payload["cover_image"] = _cover_image_payload(books_by_id[book_payload["id"]])
        return render(
            request,
            "core/library_books.html",
            {
                "books": serialized,
                "active_filters": _active_filter_chips(request, search_request),
                "hidden_filters": [
                    (param, request.GET[param])
                    for param in ("author_id", "genre_id", "category_id", "decade", "has_cover")
                    if request.GET.get(param, "").strip()
                ],
                "count": result.count,
                "fuzzy": result.fuzzy,
                "page_number": result.page,
                "next_url": _next_page_url(request, result, search_request.page_size),
                "previous_url": _previous_page_url(request, result),
                "filters": {
                    "q": search_request.query,
                    "title": search_request.title,
                    "author": search_request.author,
                    "category": search_request.category,
                    "genre": search_request.genre,
                    "language": search_request.language,
                    "format": search_request.book_format,
                    "sort": result.sort,
                },
                "all_genres": list(Genre.objects.order_by("name")),
            },
        )
    payload: dict[str, Any] = {
        "count": result.count,
        "page": result.page,
        "page_size": len(serialized),
        "sort": result.sort,
        "next_cursor": result.next_cursor,
        "previous_cursor": result.previous_cursor,
        "fuzzy": result.fuzzy,
        "items": serialized,
    }
    if requested_facets:
        payload["facets"] = facet_payload
    response = JsonResponse(payload)
    if search_request.page is not None and result.sort != search.RELEVANCE:
        # Offset pages over the whole collection give way to cursors (#28).
        response["Deprecation"] = "true"
    return response


@require_GET
@login_required
def library_book_detail(request: HttpRequest, book_id: int) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        book = (
            library_visibility.visible_books()
            .prefetch_related(
                "authors",
                "categories",
                "genres",
                "identifiers",
                "file_variants",
                "front_cover",
                "back_cover",
            )
            .get(pk=book_id)
        )
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    payload = _serialize_book(
        book,
        include_variants=True,
        include_conversion_options=True,
    )
    if book.front_cover is not None:
        payload["cover_front"] = _serialize_cover(book.front_cover)
    if book.back_cover is not None:
        payload["cover_back"] = _serialize_cover(book.back_cover)
    payload["identifiers"] = [
        {"kind": identifier.kind, "value": identifier.value}
        for identifier in book.identifiers.all()
    ]
    payload["combine_options"] = combine.combine_options_for_book(book)
    if _wants_html(request):
        return render(
            request,
            "core/library_book_detail.html",
            {
                "book": book,
                "book_payload": payload,
                "variants": payload.get("variants", []),
                "identifiers": payload["identifiers"],
                "conversion_options": payload.get("conversion_options", []),
                "combine_options": payload["combine_options"],
            },
        )
    return JsonResponse(payload)


@require_GET
@login_required
def library_book_download(
    request: HttpRequest,
    book_id: int,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        book = (
            library_visibility.visible_books()
            .prefetch_related("file_variants__storage_location")
            .get(pk=book_id)
        )
    except Book.DoesNotExist:
        return HttpResponseNotFound()
    servable = library_visibility.library_variants(
        book.file_variants.filter(processing_state=FileVariant.ProcessingState.COMPLETE)
    )

    variant = None
    variant_id = request.GET.get("variant_id")
    requested_format = request.GET.get("format", "").strip()
    if variant_id:
        try:
            variant = servable.get(pk=int(variant_id))
        except (ValueError, FileVariant.DoesNotExist):
            return HttpResponseBadRequest("variant_id invalid")
    elif requested_format:
        variant = servable.filter(format__iexact=requested_format).order_by("id").first()
    if variant is None and requested_format:
        return HttpResponseNotFound("requested format not available")
    if variant is None:
        variant = servable.order_by("id").first()

    if variant is None:
        return HttpResponseNotFound("variant not available")

    try:
        ensure_reference_intact(variant)
    except ReferencedFileError:
        # Indexed-in-place file is missing or changed on its share: never serve
        # different bytes under this record. The variant is flagged for an admin.
        return HttpResponse("file changed or missing on storage", status=409)
    try:
        storage = backend_for_location(variant.storage_location)
    except StorageConfigurationError:
        return HttpResponse("variant storage unavailable", status=503)
    try:
        stream = storage.open(variant.storage_identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("variant missing")
    except OSError:
        return HttpResponse("storage unavailable", status=503)

    response = FileResponse(stream, as_attachment=True, filename=variant.original_filename)
    response["Content-Type"] = variant.mime_type
    response["Content-Length"] = str(variant.size_bytes)
    return response


@require_GET
@login_required
def library_book_cover(
    request: HttpRequest,
    book_id: int,
    position: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    if position not in {"front", "back"}:
        return HttpResponseBadRequest("invalid cover position")
    try:
        cover = CoverImage.objects.select_related("storage_location").get(
            book_id=book_id,
            book__in=library_visibility.visible_books(),
            position=position,
        )
    except CoverImage.DoesNotExist:
        return HttpResponseNotFound("cover not available")

    try:
        storage = backend_for_location(cover.storage_location)
    except StorageConfigurationError:
        return HttpResponse("cover storage unavailable", status=503)
    try:
        stream = storage.open(cover.storage_identifier)
    except FileNotFoundError:
        return HttpResponseNotFound("cover missing")
    except OSError:
        return HttpResponse("storage unavailable", status=503)

    response = FileResponse(stream, as_attachment=False, filename=f"{position}-cover")
    response["Content-Type"] = cover.mime_type
    response["Content-Length"] = str(cover.size_bytes)
    response["Cache-Control"] = "private, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response


@require_http_methods(["GET", "POST"])
@login_required
def library_book_delete(request: HttpRequest, book_id: int) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None or not profile.is_admin():
        return HttpResponseForbidden()
    try:
        book = Book.objects.prefetch_related(
            "file_variants__storage_location",
            "cover_images__storage_location",
        ).get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    if request.method == "GET":
        if _wants_html(request):
            return render(request, "core/book_confirm_delete.html", {"book": book})
        return JsonResponse(
            {
                "book_id": book.id,
                "title": book.title,
                "confirmation_required": book.title,
                "artifact_count": book.file_variants.count() + book.cover_images.count(),
            },
        )

    payload = _request_payload(request)
    if payload.get("confirm_title") != book.title:
        return HttpResponseBadRequest("confirm_title must exactly match the book title")
    verify_hashes = _coerce_optional_bool(payload.get("verify_hashes"), default=True)
    try:
        report = book_deletion.delete_book_safely(book, verify_hashes=verify_hashes)
    except book_deletion.BookDeletionError as exc:
        return HttpResponseBadRequest(str(exc))

    if _wants_html(request):
        return redirect("library-books")
    return JsonResponse(
        {
            "book_id": report.book_id,
            "deleted": True,
            "artifact_count": report.artifact_count,
            "cleanup_warnings": list(report.cleanup_warnings),
        },
    )


@require_POST
@login_required
def library_book_conversion_request(
    request: HttpRequest,
    book_id: int,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        book = Book.objects.prefetch_related("file_variants").get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    payload = _request_payload(request)
    raw_source_variant_id = payload.get("source_variant_id")
    source_variant_id: int
    if isinstance(raw_source_variant_id, int):
        source_variant_id = raw_source_variant_id
    elif isinstance(raw_source_variant_id, str) and raw_source_variant_id.isdigit():
        source_variant_id = int(raw_source_variant_id)
    else:
        return HttpResponseBadRequest("source_variant_id required")

    raw_target_format = payload.get("target_format")
    if not isinstance(raw_target_format, str) or not raw_target_format.strip():
        return HttpResponseBadRequest("target_format required")

    try:
        source_variant = book.file_variants.get(
            pk=source_variant_id,
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )
    except (ValueError, FileVariant.DoesNotExist):
        return HttpResponseBadRequest("source variant does not belong to book")

    request_options = payload.get("request_options")
    if not isinstance(request_options, dict):
        request_options = None
    try:
        job = conversion.request_conversion(
            source_variant=source_variant,
            target_format=raw_target_format,
            requested_by_user_id=profile.user_id,
            request_options=request_options,
        )
    except conversion.ConversionError as exc:
        return HttpResponseBadRequest(str(exc))

    if _wants_html(request):
        return redirect("conversion-job-status", job_id=job.job_id)
    return JsonResponse(conversion.conversion_job_payload(job), status=202)


@require_POST
@login_required
def library_book_ocr_request(
    request: HttpRequest,
    book_id: int,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        book = Book.objects.prefetch_related("file_variants").get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()

    payload = _request_payload(request)
    raw_source_variant_id = payload.get("source_variant_id")
    if isinstance(raw_source_variant_id, int):
        source_variant_id = raw_source_variant_id
    elif isinstance(raw_source_variant_id, str) and raw_source_variant_id.isdigit():
        source_variant_id = int(raw_source_variant_id)
    else:
        return HttpResponseBadRequest("source_variant_id required")

    try:
        source_variant = book.file_variants.get(
            pk=source_variant_id,
            processing_state=FileVariant.ProcessingState.COMPLETE,
        )
    except (ValueError, FileVariant.DoesNotExist):
        return HttpResponseBadRequest("source variant does not belong to book")

    try:
        job = ocr.request_ocr_from_variant(
            source_variant=source_variant,
            requested_by_user_id=profile.user_id,
            target_formats=payload.get("target_formats"),
            request_language=payload.get("language")
            if isinstance(payload.get("language"), str)
            else None,
            engine=payload.get("engine") if isinstance(payload.get("engine"), str) else None,
            model=payload.get("model") if isinstance(payload.get("model"), str) else None,
            request_options=payload.get("request_options")
            if isinstance(payload.get("request_options"), dict)
            else None,
        )
    except ocr.OCRJobError as exc:
        return HttpResponseBadRequest(str(exc))

    if _wants_html(request):
        return redirect("ocr-job-status", job_id=job.job_id)
    return JsonResponse(ocr.ocr_job_payload(job), status=202)


@require_GET
@login_required
def conversion_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.CONVERSION:
        return HttpResponseNotFound()
    payload = conversion.conversion_job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Conversion"},
        )
    return JsonResponse(payload)


@require_POST
@login_required
def library_book_combine_parts(request: HttpRequest, book_id: int) -> HttpResponseBase:
    """Queue a combined whole-book file built from a multi-part work's parts (#60)."""
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        book = Book.objects.get(pk=book_id)
    except Book.DoesNotExist:
        return HttpResponseNotFound()
    payload = _request_payload(request)
    raw_format = payload.get("target_format")
    if not isinstance(raw_format, str) or not raw_format.strip():
        return HttpResponseBadRequest("target_format required")
    try:
        job = combine.request_combined_variant(
            book=book,
            target_format=raw_format,
            requested_by_user_id=profile.user_id,
        )
    except combine.CombineError as exc:
        return HttpResponseBadRequest(str(exc))
    if _wants_html(request):
        return redirect("combine-job-status", job_id=job.job_id)
    return JsonResponse(combine.combine_job_payload(job), status=202)


@require_GET
@login_required
def combine_job_status(request: HttpRequest, job_id: str) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()
    try:
        job = ProcessingJob.objects.get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.COMBINE_PARTS:
        return HttpResponseNotFound()
    payload = combine.combine_job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "Combine parts"},
        )
    return JsonResponse(payload)


@require_GET
@login_required
def ocr_job_status(
    request: HttpRequest,
    job_id: str,
) -> HttpResponseBase:
    profile = _active_library_profile(request)
    if profile is None:
        return HttpResponseForbidden()

    try:
        job = ProcessingJob.objects.select_related("file_variant").get(job_id=job_id)
    except (ProcessingJob.DoesNotExist, ValueError):
        return HttpResponseNotFound()
    if job.job_type != ProcessingJob.JobType.OCR:
        return HttpResponseNotFound()
    payload = ocr.ocr_job_payload(job)
    if _wants_html(request):
        return render(
            request,
            "core/processing_job.html",
            {"job": job, "payload": payload, "kind": "OCR"},
        )
    return JsonResponse(payload)
