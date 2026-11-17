"""Subject tags become Genres with provenance (issue #50)."""

from __future__ import annotations

import importlib
from io import StringIO
from pathlib import Path
from typing import Any
from zipfile import ZipFile

import pytest
from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client

from ebook_server.core import genres, identification, intake
from ebook_server.core.models import (
    Book,
    BookGenre,
    Genre,
    GenreAlias,
    ImportJob,
    ReviewTask,
    StorageLocation,
    genre_key,
    get_or_create_profile,
)
from ebook_server.storage import LocalFilesystemStorage

DC = "http://purl.org/dc/elements/1.1/"
SEED = importlib.import_module("ebook_server.core.migrations.0027_seed_genres")


@pytest.fixture
def seeded() -> None:
    """Re-run the seed migration: transactional tests flush migration data."""
    SEED.seed_genres(apps, None)


def _genre(name: str) -> Genre:
    return Genre.objects.get(name=name)


def _build_epub(path: Path, subjects: list[str]) -> None:
    subject_xml = "".join(f"<dc:subject>{subject}</dc:subject>" for subject in subjects)
    with ZipFile(path, "w") as archive:
        archive.writestr(
            "META-INF/container.xml",
            '<?xml version="1.0"?><container version="1.0" '
            'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
            '<rootfile full-path="OEBPS/content.opf" '
            'media-type="application/oebps-package+xml"/></rootfiles></container>',
        )
        archive.writestr(
            "OEBPS/content.opf",
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            f'<metadata xmlns:dc="{DC}"><dc:title>Tagged Book</dc:title>'
            f"<dc:creator>Jane Doe</dc:creator>{subject_xml}</metadata></package>",
        )


def _upload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider_subjects: list[str],
    embedded_subjects: list[str],
    auto_accept: bool,
    username: str = "genre-user",
) -> tuple[Client, ImportJob]:
    class StubProvider:
        provider_id = "stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, Any]]:
            del title, author, isbn, folder_hint
            return [
                {
                    "title": "Tagged Book",
                    "author_name": ["Jane Doe"],
                    "subject": provider_subjects,
                }
            ]

    monkeypatch.setattr(identification, "_provider_instances", lambda: [StubProvider()])
    monkeypatch.setattr(intake, "AUTO_ACCEPT_SCORE", 0.1 if auto_accept else 0.999)
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    storage = LocalFilesystemStorage(str(tmp_path / "library-artifacts"))
    monkeypatch.setattr(intake, "_default_storage", lambda: storage)
    profile = get_or_create_profile(username)
    client = Client()
    client.force_login(profile.user)
    epub_path = tmp_path / f"{username}.epub"
    _build_epub(epub_path, embedded_subjects)
    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                epub_path.name, epub_path.read_bytes(), content_type="application/epub+zip"
            )
        },
    )
    assert response.status_code == 201
    return client, ImportJob.objects.get(pk=response.json()["job_id"])


def _links(book: Book) -> dict[str, tuple[str, str]]:
    return {
        link.genre.name: (link.source, link.source_tag)
        for link in BookGenre.objects.filter(book=book).select_related("genre")
    }


# --- normalization -------------------------------------------------------------------


def test_genre_key_folds_case_punctuation_spacing_and_accents() -> None:
    assert genre_key("Sci-Fi") == genre_key("SciFi") == genre_key(" sci  fi ") == "scifi"
    assert genre_key("Mystery & Detective") == genre_key("mystery and detective")
    assert genre_key("Ròman") == genre_key("roman")
    assert genre_key("---") == ""


@pytest.mark.django_db
def test_seed_migration_collapses_common_spellings(seeded: None) -> None:
    science_fiction = _genre("Science fiction")
    tags = [
        "SciFi",
        "Sci-Fi",
        "Science Fiction",
        "FICTION / Science Fiction / General",
        "Fiction / Science Fiction / Space Opera",
        "Fiction, science fiction, general",
    ]
    assert [match.genre for match in genres.resolve_tags(tags)] == [science_fiction] * len(tags)


@pytest.mark.django_db
def test_unknown_or_ambiguous_tags_stay_unmapped(seeded: None) -> None:
    before = Genre.objects.count()
    tags = ["Space opera", "Fiction", "Science fiction, history and criticism", "FICTION"]

    assert [match.genre for match in genres.resolve_tags(tags)] == [None] * len(tags)
    assert Genre.objects.count() == before  # never auto-created


@pytest.mark.django_db
def test_admin_alias_is_matched_and_validated(seeded: None) -> None:
    fantasy = _genre("Fantasy")
    alias = GenreAlias(genre=fantasy, alias="Sword & Sorcery")
    alias.full_clean()
    alias.save()
    assert alias.key == "swordandsorcery"
    assert genres.resolve_tags(["sword and sorcery"])[0].genre == fantasy

    with pytest.raises(ValidationError):
        GenreAlias(genre=fantasy, alias="SWORD-AND-SORCERY").full_clean()
    with pytest.raises(ValidationError):  # another genre's own name
        GenreAlias(genre=fantasy, alias="Horror").full_clean()


@pytest.mark.django_db
def test_suggestion_prefers_provider_subjects_then_embedded(seeded: None) -> None:
    provider = genres.suggest(["Fiction / Fantasy"], ["Horror"])
    assert provider.source == BookGenre.Source.PROVIDER
    assert [genre.name for genre in provider.genres] == ["Fantasy"]

    embedded = genres.suggest(["Space opera"], ["Horror"])
    assert embedded.source == BookGenre.Source.EMBEDDED
    assert [genre.name for genre in embedded.genres] == ["Horror"]

    nothing = genres.suggest(["Space opera"], [])
    assert nothing.source == ""
    assert nothing.genres == []


# --- assignment rules ----------------------------------------------------------------


@pytest.mark.django_db
def test_automatic_assignment_records_provenance_and_respects_curation(seeded: None) -> None:
    book = Book.objects.create(title="Book")
    added = genres.assign_automatic(
        book, provider_subjects=["Science Fiction", "SciFi", "Thrillers"], embedded_subjects=[]
    )
    assert [genre.name for genre in added] == ["Science fiction", "Thriller"]
    assert _links(book) == {
        "Science fiction": ("provider", "Science Fiction"),
        "Thriller": ("provider", "Thrillers"),
    }

    curated = Book.objects.create(title="Curated")
    curated.genres.add(_genre("Romance"))  # admin inline / plain add => admin-set
    assert (
        genres.assign_automatic(curated, provider_subjects=["Horror"], embedded_subjects=[]) == []
    )
    assert _links(curated) == {"Romance": ("admin", "")}


@pytest.mark.django_db
def test_reviewer_assignment_upgrades_automatic_links_but_not_admin_links(seeded: None) -> None:
    book = Book.objects.create(title="Book")
    genres.assign_automatic(book, provider_subjects=["Horror"], embedded_subjects=[])
    BookGenre.objects.create(book=book, genre=_genre("Poetry"), source=BookGenre.Source.ADMIN)

    genres.assign_reviewer(book, [_genre("Horror"), _genre("Poetry"), _genre("Drama")])

    assert _links(book) == {
        "Horror": ("reviewer", "Horror"),
        "Poetry": ("admin", ""),
        "Drama": ("reviewer", ""),
    }


# --- pipeline and review integration --------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_auto_accepted_import_gets_provider_genres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    _, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Fiction / Science Fiction / General", "Space opera"],
        embedded_subjects=["Horror"],
        auto_accept=True,
    )

    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None
    assert _links(job.book) == {
        "Science fiction": ("provider", "Fiction / Science Fiction / General")
    }


@pytest.mark.django_db(transaction=True)
def test_auto_accept_falls_back_to_embedded_genres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    _, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Space opera"],
        embedded_subjects=["Horror fiction", "Weird stuff"],
        auto_accept=True,
    )

    assert job.book is not None
    assert _links(job.book) == {"Horror": ("embedded", "Horror fiction")}
    assert Genre.objects.filter(name="Weird stuff").count() == 0


@pytest.mark.django_db(transaction=True)
def test_review_page_shows_tag_to_genre_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    client, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["SciFi", "Space opera"],
        embedded_subjects=[],
        auto_accept=False,
    )
    task = ReviewTask.objects.get(import_job=job)
    science_fiction = _genre("Science fiction")

    detail = client.get(f"/reviews/{task.id}/", HTTP_ACCEPT="application/json").json()
    suggestion = detail["candidates"][0]["genre_suggestion"]
    assert suggestion["source"] == "provider"
    assert suggestion["genre_ids"] == [science_fiction.pk]
    assert suggestion["tags"] == [
        {"tag": "SciFi", "genre_id": science_fiction.pk, "genre": "Science fiction"},
        {"tag": "Space opera", "genre_id": None, "genre": None},
    ]

    html = client.get(f"/reviews/{task.id}/", HTTP_ACCEPT="text/html").content.decode()
    assert "SciFi → Science fiction" in html
    assert f'name="genre_ids" value="{science_fiction.pk}" checked' in html
    assert 'name="genres_reviewed" value="1"' in html


@pytest.mark.django_db(transaction=True)
def test_reviewer_can_deselect_and_add_genres_on_accept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    client, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Science Fiction", "Thriller"],
        embedded_subjects=[],
        auto_accept=False,
    )
    task = ReviewTask.objects.get(import_job=job)
    candidate = job.metadata_candidates.get()
    poetry = _genre("Poetry")

    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {
            "candidate_id": candidate.id,
            "genres_reviewed": "1",
            # Thriller left unticked, Science fiction kept, Poetry added.
            "genre_ids": [_genre("Science fiction").pk, poetry.pk],
        },
    )

    assert response.status_code == 200
    task.refresh_from_db()
    assert task.resolved_book is not None
    assert _links(task.resolved_book) == {
        "Science fiction": ("reviewer", "Science Fiction"),
        "Poetry": ("reviewer", ""),
    }
    assert task.decision_payload["reviewer_genres"] == ["Science fiction", "Poetry"]


@pytest.mark.django_db(transaction=True)
def test_reviewer_can_accept_with_no_genres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    client, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Science Fiction"],
        embedded_subjects=[],
        auto_accept=False,
    )
    task = ReviewTask.objects.get(import_job=job)
    candidate = job.metadata_candidates.get()

    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": candidate.id, "genres_reviewed": "1"},
    )

    assert response.status_code == 200
    task.refresh_from_db()
    assert task.resolved_book is not None
    assert _links(task.resolved_book) == {}
    assert task.resolved_book.genres_curated_at is not None
    assert task.resolved_book.genres_curated_by is not None


@pytest.mark.django_db(transaction=True)
def test_empty_reviewer_genre_choice_survives_backfill_and_automatic_assignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    """Regression (PR #100 review): "no genres" is a curated decision, not an absence."""
    client, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Science Fiction"],
        embedded_subjects=["Horror"],
        auto_accept=False,
    )
    task = ReviewTask.objects.get(import_job=job)
    candidate = job.metadata_candidates.get()
    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": candidate.id, "genres_reviewed": "1"},
    )
    assert response.status_code == 200
    task.refresh_from_db()
    book = task.resolved_book
    assert book is not None
    assert task.decision_payload["reviewer_genres"] == []

    out = StringIO()
    call_command("assign_genres", stdout=out)
    assert "Linked 0 genre(s) to 0 book(s)." in out.getvalue()
    assert _links(book) == {}

    stale = Book.objects.get(pk=book.pk)
    assert (
        genres.assign_automatic(
            stale, provider_subjects=["Science Fiction"], embedded_subjects=["Horror"]
        )
        == []
    )
    assert _links(book) == {}


@pytest.mark.django_db
def test_admin_genre_edit_marks_book_curated_even_when_emptied(seeded: None) -> None:
    from django.contrib import admin as django_admin
    from django.contrib.auth import get_user_model
    from django.test import RequestFactory

    from ebook_server.core.admin import BookAdmin, BookGenreInline

    book = Book.objects.create(title="Admin curated")
    genres.assign_automatic(book, provider_subjects=["Horror"], embedded_subjects=[])
    link = BookGenre.objects.get(book=book)
    admin_user = get_user_model().objects.create_superuser("genre-admin", "", "x")
    request = RequestFactory().post("/")
    request.user = admin_user

    inline = BookGenreInline(Book, django_admin.site)
    formset_class = inline.get_formset(request, book)
    prefix = formset_class.get_default_prefix()
    data = {
        f"{prefix}-TOTAL_FORMS": "1",
        f"{prefix}-INITIAL_FORMS": "1",
        f"{prefix}-MIN_NUM_FORMS": "0",
        f"{prefix}-MAX_NUM_FORMS": "1000",
        f"{prefix}-0-id": str(link.pk),
        f"{prefix}-0-book": str(book.pk),
        f"{prefix}-0-genre": str(link.genre_id),
        f"{prefix}-0-source": link.source,
        f"{prefix}-0-DELETE": "on",
    }
    formset = formset_class(data, instance=book, prefix=prefix)
    assert formset.is_valid(), formset.errors

    class _Form:
        instance = book

    BookAdmin(Book, django_admin.site).save_formset(request, _Form(), formset, change=True)

    book.refresh_from_db()
    assert _links(book) == {}
    assert book.genres_curated_at is not None
    assert book.genres_curated_by == admin_user
    assert genres.assign_automatic(book, provider_subjects=["Horror"], embedded_subjects=[]) == []


@pytest.mark.django_db(transaction=True)
def test_api_accept_without_genre_choice_applies_provider_genres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    client, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Thrillers"],
        embedded_subjects=[],
        auto_accept=False,
    )
    task = ReviewTask.objects.get(import_job=job)
    candidate = job.metadata_candidates.get()

    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": candidate.id},
        content_type="application/json",
    )

    assert response.status_code == 200
    task.refresh_from_db()
    assert task.resolved_book is not None
    assert _links(task.resolved_book) == {"Thriller": ("provider", "Thrillers")}
    assert task.decision_payload["automatic_genres"] == ["Thriller"]


@pytest.mark.django_db(transaction=True)
def test_accept_rejects_unknown_genre_ids_without_side_effects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    client, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Thrillers"],
        embedded_subjects=[],
        auto_accept=False,
    )
    task = ReviewTask.objects.get(import_job=job)
    candidate = job.metadata_candidates.get()
    books_before = Book.objects.count()

    response = client.post(
        f"/reviews/{task.id}/accept-candidate/",
        {"candidate_id": candidate.id, "genre_ids": [999999]},
        content_type="application/json",
    )

    assert response.status_code == 400
    task.refresh_from_db()
    assert task.state == ReviewTask.State.OPEN
    assert Book.objects.count() == books_before


@pytest.mark.django_db(transaction=True)
def test_manual_entry_uses_embedded_genres(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    client, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Thrillers"],
        embedded_subjects=["Love stories"],
        auto_accept=False,
    )
    task = ReviewTask.objects.get(import_job=job)

    response = client.post(
        f"/reviews/{task.id}/manual-entry/",
        {"title": "Typed In", "authors": ["Jane Doe"]},
        content_type="application/json",
    )

    assert response.status_code == 200
    book = Book.objects.get(title="Typed In")
    assert _links(book) == {"Romance": ("embedded", "Love stories")}


# --- browse --------------------------------------------------------------------------


@pytest.mark.django_db
def test_library_genre_filter_resolves_aliases(seeded: None) -> None:
    profile = get_or_create_profile("genre-browser")
    client = Client()
    client.force_login(profile.user)
    space = Book.objects.create(title="Space Book")
    genres.assign_automatic(space, provider_subjects=["Science Fiction"], embedded_subjects=[])
    Book.objects.create(title="Untagged Book")

    for value in ("sci-fi", "Science fiction", "fiction"):
        payload = client.get("/library/books/", {"genre": value}).json()
        assert [item["title"] for item in payload["items"]] == ["Space Book"], value
        assert payload["items"][0]["genres"] == ["Science fiction"]

    html = client.get("/library/books/", {"view": "html", "genre": "sci-fi"}).content.decode()
    assert '<option value="Science fiction">' in html
    assert "Space Book" in html and "Untagged Book" not in html

    detail = client.get(f"/library/books/{space.id}/", {"view": "html"}).content.decode()
    assert "Science fiction</a>" in detail


# --- backfill command ----------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_assign_genres_command_backfills_and_reports_unmapped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seeded: None
) -> None:
    _, job = _upload(
        tmp_path,
        monkeypatch,
        provider_subjects=["Space opera"],
        embedded_subjects=[],
        auto_accept=True,
    )
    assert job.book is not None
    assert _links(job.book) == {}

    # An admin promotes the unmapped tag, then re-runs the backfill.
    GenreAlias.objects.create(genre=_genre("Science fiction"), alias="Space opera")
    dry = StringIO()
    call_command("assign_genres", "--dry-run", stdout=dry)
    assert "Would link 1 genre(s) to 1 book(s)." in dry.getvalue()
    assert _links(job.book) == {}

    out = StringIO()
    call_command("assign_genres", "--unmapped", "5", stdout=out)
    assert "Linked 1 genre(s) to 1 book(s)." in out.getvalue()
    assert _links(job.book) == {"Science fiction": ("provider", "Space opera")}


# --- migrations ----------------------------------------------------------------------


@pytest.mark.django_db(transaction=True)
def test_migrations_keep_existing_links_as_admin_set_and_reverse() -> None:
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    before = [("core", "0025_importjob_cancelled_state")]
    after = [("core", "0027_seed_genres")]
    executor = MigrationExecutor(connection)
    executor.migrate(before)
    old_apps = executor.loader.project_state(before).apps
    old_book = old_apps.get_model("core", "Book").objects.create(title="Linked")
    old_genre = old_apps.get_model("core", "Genre").objects.create(name="Hand Picked")
    old_book.genres.add(old_genre)

    executor = MigrationExecutor(connection)
    executor.migrate(after)
    new_apps = executor.loader.project_state(after).apps
    link = new_apps.get_model("core", "BookGenre").objects.get(book_id=old_book.pk)
    assert (link.genre_id, link.source, link.source_tag) == (old_genre.pk, "admin", "")
    seeded_genre = new_apps.get_model("core", "Genre").objects.get(name="Science fiction")
    assert seeded_genre.aliases.filter(key="scifi").exists()

    executor = MigrationExecutor(connection)
    executor.migrate(before)
    reverted_apps = executor.loader.project_state(before).apps
    reverted_book = reverted_apps.get_model("core", "Book").objects.get(pk=old_book.pk)
    assert [genre.name for genre in reverted_book.genres.all()] == ["Hand Picked"]
    assert not reverted_apps.get_model("core", "Genre").objects.filter(name="Fantasy").exists()

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())


@pytest.mark.django_db(transaction=True)
def test_seed_reversal_keeps_pre_existing_admin_taxonomy() -> None:
    """Regression (PR #100 review): unseeding deletes only rows the seed created."""
    from django.db import connection
    from django.db.migrations.executor import MigrationExecutor

    schema_only = [("core", "0026_genre_alias_book_genre_provenance")]
    seeded_state = [("core", "0027_seed_genres")]
    executor = MigrationExecutor(connection)
    executor.migrate(schema_only)
    old_apps = executor.loader.project_state(schema_only).apps
    old_genre_model = old_apps.get_model("core", "Genre")
    old_genre_model.objects.all().delete()
    admin_genre = old_genre_model.objects.create(name="Science fiction")
    admin_alias = old_apps.get_model("core", "GenreAlias").objects.create(
        genre=admin_genre, alias="Sci-Fi", key="scifi"
    )

    executor = MigrationExecutor(connection)
    executor.migrate(seeded_state)
    new_apps = executor.loader.project_state(seeded_state).apps
    genre_model = new_apps.get_model("core", "Genre")
    alias_model = new_apps.get_model("core", "GenreAlias")
    # Forward seeding reuses the admin rows instead of creating twins.
    assert genre_model.objects.filter(name="Science fiction").count() == 1
    assert alias_model.objects.get(key="scifi").pk == admin_alias.pk
    assert alias_model.objects.filter(genre_id=admin_genre.pk, seeded=True).exists()
    assert genre_model.objects.get(name="Fantasy").seeded

    executor = MigrationExecutor(connection)
    executor.migrate(schema_only)
    reverted_apps = executor.loader.project_state(schema_only).apps
    reverted_genres = reverted_apps.get_model("core", "Genre")
    reverted_aliases = reverted_apps.get_model("core", "GenreAlias")
    assert reverted_genres.objects.filter(pk=admin_genre.pk, name="Science fiction").exists()
    assert list(reverted_aliases.objects.values_list("pk", "key")) == [(admin_alias.pk, "scifi")]
    assert not reverted_genres.objects.filter(name="Fantasy").exists()

    executor = MigrationExecutor(connection)
    executor.migrate(executor.loader.graph.leaf_nodes())
