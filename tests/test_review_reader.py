"""Review-page reader panel (issue #6): permissions, previews, sanitization, and limits."""

from __future__ import annotations

import base64
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from django.test import Client
from PIL import Image

from ebook_server.core import review_reader
from ebook_server.core.models import (
    Book,
    FileVariant,
    ImportJob,
    ReviewTask,
    StorageLocation,
    UserRole,
    get_or_create_profile,
)


def _png_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (4, 6), (200, 30, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


PNG = _png_bytes()


def _epub_bytes(chapter_markup: str, *, extra: dict[str, bytes] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("mimetype", "application/epub+zip")
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            "<manifest>"
            '<item id="c1" href="text/one.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c2" href="text/two.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="evil" href="../../etc/passwd" media-type="application/xhtml+xml"/>'
            '<item id="img" href="images/pic.png" media-type="image/png"/>'
            "</manifest>"
            '<spine><itemref idref="c1"/><itemref idref="evil"/><itemref idref="c2"/></spine>'
            "</package>",
        )
        archive.writestr("OEBPS/text/one.xhtml", chapter_markup)
        archive.writestr(
            "OEBPS/text/two.xhtml",
            "<html><body><p>Second chapter</p></body></html>",
        )
        archive.writestr("OEBPS/images/pic.png", PNG)
        for name, payload in (extra or {}).items():
            archive.writestr(name, payload)
    return buffer.getvalue()


HOSTILE_CHAPTER = (
    "<html><head><title>leak</title><style>body{display:none}</style>"
    "<script>alert('head')</script></head><body>"
    "<h1 onclick=\"alert('h')\">Chapter One</h1>"
    "<p>Real text<script>alert('x')</script></p>"
    '<img src="../images/pic.png" onerror="alert(1)">'
    '<img src="https://tracker.example/pixel.gif">'
    '<a href="javascript:alert(2)">bad link</a><a href="#note">note</a>'
    '<iframe src="https://evil.example"></iframe>'
    '<svg><image xlink:href="../images/pic.png"/></svg>'
    "</body></html>"
)


@pytest.fixture
def storage_root(tmp_path: Path) -> Path:
    root = tmp_path / "reader-storage"
    root.mkdir()
    return root


@pytest.fixture
def location(storage_root: Path) -> StorageLocation:
    return StorageLocation.objects.create(
        identifier="reader-test",
        backend=StorageLocation.BackendType.LOCAL,
        base_path=str(storage_root),
    )


def _write(storage_root: Path, name: str, payload: bytes) -> str:
    (storage_root / name).write_bytes(payload)
    return name


def _variant(
    book: Book,
    location: StorageLocation,
    storage_root: Path,
    file_format: str,
    payload: bytes,
    *,
    mime_type: str = "application/octet-stream",
) -> FileVariant:
    name = f"book-{book.pk}.{file_format}"
    return FileVariant.objects.create(
        book=book,
        format=file_format,
        mime_type=mime_type,
        original_filename=name,
        storage_location=location,
        storage_identifier=_write(storage_root, name, payload),
        size_bytes=len(payload),
        sha256=f"{file_format}-{book.pk}".ljust(64, "0"),
        source_type=FileVariant.SourceType.UPLOAD,
        processing_state=FileVariant.ProcessingState.COMPLETE,
    )


def _task(
    owner_name: str,
    location: StorageLocation,
    storage_root: Path,
    *,
    file_format: str = "txt",
    payload: bytes = b"Hello reviewer",
    book: Book | None = None,
) -> ReviewTask:
    owner = get_or_create_profile(owner_name)
    job = ImportJob.objects.create(
        source_type=ImportJob.SourceType.UPLOAD,
        state=ImportJob.State.LOW_CONFIDENCE,
        uploader=owner.user,
        source_path="/nonexistent/staging",
        original_filename=f"upload.{file_format}",
        detected_format=file_format,
        mime_type="application/octet-stream",
        size_bytes=len(payload),
        storage_location=location,
        storage_identifier=_write(storage_root, f"import-{owner_name}.{file_format}", payload),
        book=book,
    )
    return ReviewTask.objects.create(import_job=job, source_filename=job.original_filename)


def _client(username: str, *, role: str = UserRole.NORMAL_USER) -> Client:
    client = Client()
    client.force_login(get_or_create_profile(username, role=role).user)
    return client


def _url(task: ReviewTask, key: str, endpoint: str) -> str:
    return f"/reviews/{task.pk}/reader/{key}/{endpoint}/"


# --- sources and permissions --------------------------------------------------------------


@pytest.mark.django_db
def test_sources_include_import_file_and_every_book_variant(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    book = Book.objects.create(title="Shared Book")
    epub = _variant(book, location, storage_root, "epub", _epub_bytes("<p>x</p>"))
    mobi = _variant(book, location, storage_root, "mobi", b"BOOKMOBI")
    pending = _variant(book, location, storage_root, "pdf", b"%PDF-1.4")
    pending.processing_state = FileVariant.ProcessingState.QUEUED
    pending.save()
    other_book = Book.objects.create(title="Unrelated")
    _variant(other_book, location, storage_root, "txt", b"secret")
    task = _task("reader-owner", location, storage_root, book=book)

    sources = review_reader.reader_sources(task)

    assert [source.key for source in sources] == ["import", f"v{epub.pk}", f"v{mobi.pk}"]
    assert [source.kind for source in sources] == ["text", "document", "none"]


@pytest.mark.django_db
def test_review_page_renders_collapsed_reader_with_format_tabs(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    book = Book.objects.create(title="Shared Book")
    variant = _variant(book, location, storage_root, "mobi", b"BOOKMOBI")
    task = _task("reader-page", location, storage_root, book=book)

    response = _client("reader-page").get(f"/reviews/{task.pk}/?view=html")

    assert response.status_code == 200
    html = response.content.decode()
    assert "data-review-reader" in html
    assert 'id="review-reader-body" data-reader-body hidden' in html
    assert 'aria-expanded="false"' in html
    assert 'data-reader-source="import"' in html
    assert f'data-reader-source="v{variant.pk}"' in html
    assert "Kindle/MOBI files cannot be rendered" in html
    assert "core/dist/review-reader.js" in html


@pytest.mark.django_db
def test_reader_endpoints_enforce_review_permissions(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    task = _task("reader-alice", location, storage_root)
    url = _url(task, "import", "text")

    assert Client().get(url).status_code == 302
    assert _client("reader-mallory").get(url).status_code == 403
    assert _client("reader-alice").get(url).status_code == 200
    assert _client("reader-admin", role=UserRole.ADMIN).get(url).status_code == 200
    get_or_create_profile("reader-banned", is_disabled=True)
    banned = Client()
    banned.force_login(get_or_create_profile("reader-banned").user)
    assert banned.get(url).status_code in {302, 403}


@pytest.mark.django_db
def test_reader_rejects_variants_not_attached_to_the_review(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    other_book = Book.objects.create(title="Someone else's book")
    foreign = _variant(other_book, location, storage_root, "txt", b"not yours")
    task = _task("reader-idor", location, storage_root)
    client = _client("reader-idor")

    assert client.get(_url(task, f"v{foreign.pk}", "text")).status_code == 404
    assert client.get(_url(task, "..", "file")).status_code == 404
    assert client.get(f"/reviews/{task.pk}/reader/v0/file/").status_code == 404


# --- raw file streaming -------------------------------------------------------------------


@pytest.mark.django_db
def test_pdf_streams_inline_with_range_support(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    payload = b"%PDF-1.4\n" + bytes(range(256)) * 4
    task = _task("reader-pdf", location, storage_root, file_format="pdf", payload=payload)
    client = _client("reader-pdf")
    url = _url(task, "import", "file")

    full = client.get(url)
    assert full.status_code == 200
    assert full["Content-Type"] == "application/pdf"
    assert full["Content-Disposition"].startswith("inline")
    assert full["X-Content-Type-Options"] == "nosniff"
    assert full["Accept-Ranges"] == "bytes"
    assert "sandbox" in full["Content-Security-Policy"]
    assert full.getvalue() == payload

    partial = client.get(url, HTTP_RANGE="bytes=9-18")
    assert partial.status_code == 206
    assert partial["Content-Range"] == f"bytes 9-18/{len(payload)}"
    assert partial.getvalue() == payload[9:19]

    suffix = client.get(url, HTTP_RANGE="bytes=-5")
    assert suffix.getvalue() == payload[-5:]

    unsatisfiable = client.get(url, HTTP_RANGE=f"bytes={len(payload) + 10}-")
    assert unsatisfiable.status_code == 416


@pytest.mark.django_db
def test_untrusted_formats_are_only_ever_served_as_attachments(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    task = _task(
        "reader-html-raw",
        location,
        storage_root,
        file_format="html",
        payload=b"<script>alert(1)</script>",
    )

    response = _client("reader-html-raw").get(_url(task, "import", "file"))

    assert response.status_code == 200
    assert response["Content-Type"] == "application/octet-stream"
    assert response["Content-Disposition"].startswith("attachment")
    assert response["X-Content-Type-Options"] == "nosniff"


# --- text ---------------------------------------------------------------------------------


@pytest.mark.django_db
def test_text_preview_detects_charset_and_pages(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    text = "Grüße aus der Bibliothek. " * 20_000
    task = _task(
        "reader-text",
        location,
        storage_root,
        payload=text.encode("cp1252"),
    )
    client = _client("reader-text")

    first = json.loads(client.get(_url(task, "import", "text")).content)
    assert first["page"] == 1
    assert first["page_count"] == 3
    assert first["text"].startswith("Grüße aus der Bibliothek.")
    assert first["truncated"] is False

    last = json.loads(client.get(_url(task, "import", "text") + "?page=99").content)
    assert last["page"] == 3


@pytest.mark.django_db
def test_text_preview_is_size_limited(
    location: StorageLocation,
    storage_root: Path,
    settings: Any,
) -> None:
    settings.BOOK_SCANNER_READER_MAX_TEXT_BYTES = 1000
    task = _task("reader-big-text", location, storage_root, payload=b"a" * 5000)

    payload = json.loads(_client("reader-big-text").get(_url(task, "import", "text")).content)

    assert payload["truncated"] is True
    assert len(payload["text"]) == 1000


# --- sanitized documents ------------------------------------------------------------------


def test_sanitizer_strips_active_content_and_remote_resources() -> None:
    cleaned = review_reader.sanitize_document(
        HOSTILE_CHAPTER,
        lambda href: review_reader.image_data_uri(PNG) if href.endswith("pic.png") else None,
    )

    lowered = cleaned.lower()
    for forbidden in (
        "<script",
        "alert(",
        "onclick",
        "onerror",
        "javascript:",
        "tracker.example",
        "<iframe",
        "evil.example",
        "leak",
        "display:none",
        "<svg",
    ):
        assert forbidden not in lowered, forbidden
    assert "Chapter One" in cleaned
    assert "Real text" in cleaned
    assert 'href="#note"' in cleaned
    assert cleaned.count('src="data:image/png;base64,') == 2


@pytest.mark.django_db
def test_epub_chapters_are_sanitized_and_served_in_a_locked_down_document(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    book = Book.objects.create(title="EPUB Book")
    variant = _variant(book, location, storage_root, "epub", _epub_bytes(HOSTILE_CHAPTER))
    task = _task("reader-epub", location, storage_root, book=book)
    client = _client("reader-epub")
    key = f"v{variant.pk}"

    parts = json.loads(client.get(_url(task, key, "parts")).content)["parts"]
    # The manifest entry pointing outside the archive is never part of the spine.
    assert parts == [
        {"index": 1, "label": "one.xhtml"},
        {"index": 2, "label": "two.xhtml"},
    ]

    response = client.get(_url(task, key, "document") + "?part=1")
    assert response.status_code == 200
    csp = response["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "sandbox" in csp
    assert "img-src data:" in csp
    assert response["X-Frame-Options"] == "SAMEORIGIN"
    assert response["X-Content-Type-Options"] == "nosniff"
    body = response.content.decode()
    assert "Chapter One" in body
    assert "<script" not in body.lower()
    assert "data:image/png;base64," in body

    second = client.get(_url(task, key, "document") + "?part=2").content.decode()
    assert "Second chapter" in second


def test_epub_member_references_cannot_escape_the_archive() -> None:
    assert review_reader._resolve_member_name("OEBPS/text", "../images/a.png") == (
        "OEBPS/images/a.png"
    )
    assert review_reader._resolve_member_name("OEBPS", "../../etc/passwd") is None
    assert review_reader._resolve_member_name("OEBPS", "/etc/passwd") is None
    assert review_reader._resolve_member_name("OEBPS", "https://evil.example/x.png") is None
    assert review_reader._resolve_member_name("OEBPS", "file:///etc/passwd") is None
    assert review_reader._resolve_member_name("OEBPS", "..\\..\\boot.ini") is None


def test_epub_with_unsafe_member_names_is_refused() -> None:
    hostile = _epub_bytes("<p>x</p>", extra={"../escape.txt": b"boom"})

    with pytest.raises(review_reader.PreviewUnavailable):
        review_reader.epub_parts(io.BytesIO(hostile))


def test_epub_oversized_chapter_is_not_inflated(settings: Any) -> None:
    settings.BOOK_SCANNER_READER_MAX_DOCUMENT_BYTES = 2_000
    epub = _epub_bytes("<html><body><p>" + "x" * 50_000 + "</p></body></html>")

    with pytest.raises(review_reader.PreviewUnavailable, match="too large"):
        review_reader.epub_part_html(io.BytesIO(epub), 1)


def test_epub_inline_images_respect_the_budget(settings: Any) -> None:
    settings.BOOK_SCANNER_READER_MAX_INLINE_IMAGE_BYTES = 10
    epub = _epub_bytes('<html><body><img src="../images/pic.png"></body></html>')

    cleaned = review_reader.epub_part_html(io.BytesIO(epub), 1)

    assert "data:image" not in cleaned


@pytest.mark.django_db
def test_html_preview_is_sanitized_and_size_limited(
    location: StorageLocation,
    storage_root: Path,
    settings: Any,
) -> None:
    task = _task(
        "reader-html",
        location,
        storage_root,
        file_format="html",
        payload=HOSTILE_CHAPTER.encode(),
    )
    client = _client("reader-html")

    body = client.get(_url(task, "import", "document")).content.decode()
    assert "Chapter One" in body
    assert "<script" not in body.lower()
    assert "javascript:" not in body.lower()

    settings.BOOK_SCANNER_READER_MAX_DOCUMENT_BYTES = 100
    limited = client.get(_url(task, "import", "document")).content.decode()
    assert "too large to preview" in limited
    assert "Chapter One" not in limited


def test_fb2_is_converted_to_sanitized_html() -> None:
    encoded = base64.b64encode(PNG).decode()
    fb2 = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" '
        'xmlns:l="http://www.w3.org/1999/xlink"><body>'
        "<title><p>The Title</p></title>"
        "<section><p>Hello <emphasis>world</emphasis> &lt;script&gt;alert(1)&lt;/script&gt;</p>"
        '<image l:href="#cover.png"/><empty-line/></section></body>'
        f'<binary id="cover.png" content-type="image/png">{encoded}</binary>'
        "</FictionBook>"
    )

    cleaned = review_reader.fb2_document(io.BytesIO(fb2.encode()))

    assert "<em>world</em>" in cleaned
    assert "<h2>" in cleaned
    assert "<script" not in cleaned
    assert "&lt;script&gt;" in cleaned
    assert "data:image/png;base64," in cleaned


def test_fb2_external_entities_are_rejected() -> None:
    xxe = (
        '<?xml version="1.0"?><!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]>'
        "<FictionBook><body><p>&e;</p></body></FictionBook>"
    )

    with pytest.raises(review_reader.PreviewUnavailable):
        review_reader.fb2_document(io.BytesIO(xxe.encode()))


# --- comic archives -----------------------------------------------------------------------


def _cbz_bytes(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


@pytest.mark.django_db
def test_cbz_pages_are_listed_in_natural_order_and_validated(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    cbz = _cbz_bytes(
        {
            "page10.png": PNG,
            "page2.png": PNG,
            "page1.png": PNG,
            "__MACOSX/page1.png": b"junk",
            "notes.txt": b"text",
            "fake.jpg": b"<svg onload=alert(1)>",
        },
    )
    task = _task("reader-cbz", location, storage_root, file_format="cbz", payload=cbz)
    client = _client("reader-cbz")

    parts = json.loads(client.get(_url(task, "import", "parts")).content)["parts"]
    assert [part["label"] for part in parts] == ["fake.jpg", "page1.png", "page2.png", "page10.png"]

    page = client.get(_url(task, "import", "pages/2"))
    assert page.status_code == 200
    assert page["Content-Type"] == "image/png"
    assert page["X-Content-Type-Options"] == "nosniff"
    assert page.content == PNG

    spoofed = client.get(_url(task, "import", "pages/1"))
    assert spoofed.status_code == 422
    assert client.get(_url(task, "import", "pages/99")).status_code == 422


def test_cbz_compression_bombs_are_not_inflated() -> None:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("001.png", PNG + b"\x00" * 5_000_000)

    with pytest.raises(review_reader.PreviewUnavailable, match="too large"):
        review_reader.cbz_page(io.BytesIO(buffer.getvalue()), 1)


@pytest.mark.django_db
def test_endpoints_refuse_formats_without_that_preview(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    task = _task("reader-mobi", location, storage_root, file_format="mobi", payload=b"BOOKMOBI")
    client = _client("reader-mobi")

    for endpoint in ("text", "parts", "document", "pages/1"):
        assert client.get(_url(task, "import", endpoint)).status_code == 404
    download = client.get(_url(task, "import", "file") + "?download=1")
    assert download.status_code == 200
    assert download["Content-Disposition"].startswith("attachment")


@pytest.mark.django_db
def test_missing_file_reports_not_found(
    location: StorageLocation,
    storage_root: Path,
) -> None:
    task = _task("reader-missing", location, storage_root)
    (storage_root / task.import_job.storage_identifier).unlink()  # type: ignore[union-attr]

    assert _client("reader-missing").get(_url(task, "import", "text")).status_code == 404
