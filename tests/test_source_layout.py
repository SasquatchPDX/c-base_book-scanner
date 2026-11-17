"""Folder layouts as author/title identification hints (#35)."""

from __future__ import annotations

import json
import time
from io import StringIO
from pathlib import Path

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command

from ebook_server.core.identification import (
    PATH_LAYOUT_EVIDENCE_KEY,
    build_identification_input,
)
from ebook_server.core.models import ImportJob, ReviewTask, SourceLocation, StorageLocation
from ebook_server.core.source_layout import (
    LayoutTemplateError,
    compile_template,
    hints_for_source_file,
    match_path,
    normalize_person_name,
    validate_templates,
)
from ebook_server.storage import LocalFilesystemStorage

CALIBRE = "{author}/{title} ({*})/{*}"


@pytest.mark.parametrize(
    ("relative", "templates", "authors", "title"),
    [
        ("Austen, Jane/Pride and Prejudice.txt", ["{author}/**"], ["Jane Austen"], None),
        ("Austen, Jane/Novels/Emma.epub", ["{author}/**"], ["Jane Austen"], None),
        (
            "Neal Stephenson/Snow Crash (966)/Snow Crash - Neal Stephenson.mobi",
            [CALIBRE],
            ["Neal Stephenson"],
            "Snow Crash",
        ),
        (
            "Snow Crash - Neal Stephenson.epub",
            ["{title} - {author}"],
            ["Neal Stephenson"],
            "Snow Crash",
        ),
        (
            "SF/Weber, David & Evans, Linda/Hell's Gate.epub",
            ["{*}/{authors}/{title}"],
            ["David Weber", "Linda Evans"],
            "Hell's Gate",
        ),
        (
            "deep/nest/Gibson, William/Neuromancer.txt",
            ["**/{author}/{title}"],
            ["William Gibson"],
            "Neuromancer",
        ),
    ],
)
def test_match_path_templates(
    relative: str, templates: list[str], authors: list[str], title: str | None
) -> None:
    hints = match_path(relative, templates)
    assert hints is not None
    assert hints.authors == authors
    assert hints.title == title


def test_trailing_glob_requires_a_folder() -> None:
    # A file directly in the root is not "inside an author folder".
    assert match_path("Bachmann, Ingeborg - Anrufung.txt", ["{author}/**"]) is None


def test_first_matching_template_wins_and_ignored_values_fall_through() -> None:
    templates = ["{author}/**", "{author} - {title}"]
    flat = match_path("Bas, Juan - Skorpione.txt", templates)
    assert flat is not None
    assert flat.template == "{author} - {title}"
    assert flat.authors == ["Juan Bas"]
    assert flat.title == "Skorpione"
    ignored = match_path(
        "Cliff Notes/king lear.txt",
        ["{author}/**", "{*}/{title}"],
        ignore_values=["cliff notes"],
    )
    assert ignored is not None
    assert ignored.authors == []
    assert ignored.title == "king lear"


def test_no_template_matches_returns_none() -> None:
    assert match_path("a/b/c.epub", ["{author}/{title}"]) is None


@pytest.mark.parametrize(
    ("raw", "order", "expected"),
    [
        ("Austen, Jane", "auto", "Jane Austen"),
        ("Neal Stephenson", "auto", "Neal Stephenson"),
        ("Austen, Jane", "first_last", "Austen, Jane"),
        ("Austen Jane", "last_first", "Jane Austen"),
        ("Le_Guin,  Ursula K.", "auto", "Ursula K. Le Guin"),
    ],
)
def test_normalize_person_name(raw: str, order: str, expected: str) -> None:
    assert normalize_person_name(raw, order) == expected


@pytest.mark.parametrize(
    "template",
    [
        "",
        "{writer}/**",
        "{author}/{author}",
        "{author}/{authors}",
        "a**/{title}",
        "{title",
        "a//{title}",
        "**",
    ],
)
def test_invalid_templates_are_rejected(template: str) -> None:
    with pytest.raises(LayoutTemplateError):
        compile_template(template)


def test_validate_templates_reports_shape_errors() -> None:
    assert validate_templates([]) == []
    assert validate_templates("{author}/**")  # not a list
    assert validate_templates(["{author}/**", "{nope}"])


@pytest.mark.django_db
def test_source_location_clean_validates_layout(tmp_path: Path) -> None:
    location = SourceLocation(name="x", root_path=str(tmp_path), path_layouts=["{bogus}"])
    with pytest.raises(ValidationError):
        location.full_clean()
    location.path_layouts = ["{author}/**"]
    location.layout_ignore_values = ["Unknown"]
    location.full_clean()


def test_layout_author_replaces_filename_guesses_but_keeps_embedded() -> None:
    signals = build_identification_input(
        source_path="/x/Austen, Jane/Pride and Prejudice - Volume 1.txt",
        original_filename="Pride and Prejudice - Volume 1.txt",
        extracted_metadata={
            "authors": ["J. Austen"],
            PATH_LAYOUT_EVIDENCE_KEY: {"authors": ["Jane Austen"], "title": None},
        },
    )
    assert signals.author_hints == ["J. Austen", "Jane Austen"]
    assert "Volume 1" not in signals.author_hints


def test_layout_title_follows_embedded_title() -> None:
    signals = build_identification_input(
        source_path="/x/Neal Stephenson/Snow Crash (966)/Snow Crash - Neal Stephenson.mobi",
        original_filename="Snow Crash - Neal Stephenson.mobi",
        extracted_metadata={
            "title": "Snow Crash - Neal Stephenson",
            PATH_LAYOUT_EVIDENCE_KEY: {"authors": ["Neal Stephenson"], "title": "Snow Crash"},
        },
    )
    assert signals.title_hints[:2] == ["Snow Crash - Neal Stephenson", "Snow Crash"]
    assert signals.best_author() == "Neal Stephenson"


def test_without_layout_signals_are_unchanged() -> None:
    signals = build_identification_input(
        source_path="/x/Austen, Jane/Emma - Jane Austen.txt",
        original_filename="Emma - Jane Austen.txt",
        extracted_metadata={},
    )
    assert signals.author_hints == ["Emma", "Jane Austen"]


@pytest.mark.django_db
def test_hints_for_source_file_uses_relative_path(tmp_path: Path) -> None:
    root = tmp_path / "share"
    book = root / "Unknown" / "Some Title (12)" / "Some Title - Unknown.epub"
    book.parent.mkdir(parents=True)
    book.write_bytes(b"x")
    location = SourceLocation.objects.create(
        name="calibre",
        root_path=str(root),
        path_layouts=[CALIBRE],
        layout_ignore_values=["Unknown"],
    )
    hints = hints_for_source_file(location, str(book))
    assert hints is not None
    assert hints.authors == []
    assert hints.title == "Some Title"
    location.path_layouts = []
    assert hints_for_source_file(location, str(book)) is None


@pytest.mark.django_db(transaction=True)
def test_folder_author_drives_identification_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "member-share"
    book = root / "Gibson, William" / "Neuromancer.txt"
    book.parent.mkdir(parents=True)
    book.write_text("The sky above the port was the color of television.")
    storage_root = tmp_path / "library-artifacts"
    monkeypatch.setattr(
        "ebook_server.core.intake._default_storage",
        lambda: LocalFilesystemStorage(str(storage_root)),
    )
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )
    seen_authors: list[str | None] = []

    class StubProvider:
        provider_id = "stub"

        def search(
            self,
            title: str | None = None,
            *,
            author: str | None = None,
            isbn: str | None = None,
            folder_hint: str | None = None,
        ) -> list[dict[str, object]]:
            del isbn, folder_hint
            seen_authors.append(author)
            return [{"title": "Neuromancer", "author_name": ["William Gibson"]}]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances", lambda: [StubProvider()]
    )
    location = SourceLocation.objects.create(
        name="member",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        path_layouts=["{author}/**"],
    )

    stdout = StringIO()
    call_command(
        "ingest_sources", "--source-id", str(location.id), "--preview", "--json", stdout=stdout
    )
    preview = json.loads(stdout.getvalue())
    assert preview[0]["path_layout"]["authors"] == ["William Gibson"]

    call_command("ingest_sources", "--source-id", str(location.id))

    job = ImportJob.objects.get(source_location=location)
    assert seen_authors[0] == "William Gibson"
    assert job.evidence[PATH_LAYOUT_EVIDENCE_KEY]["template"] == "{author}/**"
    top = job.metadata_candidates.order_by("-score").first()
    assert top is not None
    # Perfect title (0.55) + perfect author (0.30) = 0.85; plain text carries no
    # language, publisher or description. The folder author is the strong-author
    # evidence the exact-title + strong-author rule (#9) needs, so the candidate is
    # raised to the auto-accept threshold and the import completes without review.
    assert float(top.score) == pytest.approx(0.86)
    evidence = top.raw_payload["_auto_accept_rule"]
    assert evidence["applied"] is True
    assert evidence["base_score"] == pytest.approx(0.85)
    assert evidence["author_hint"] == "William Gibson"
    assert job.state == ImportJob.State.COMPLETE
    assert not ReviewTask.objects.filter(import_job=job).exists()
    assert book.exists()


def test_wildcard_placeholder_and_glob_counts_are_capped() -> None:
    compile_template("{*}/{*}/{*}/{author}/{*}")  # four wildcards: allowed
    with pytest.raises(LayoutTemplateError, match="wildcards"):
        compile_template("{*}/{*}/{*}/{*}/{*}/{title}")
    with pytest.raises(LayoutTemplateError, match="placeholders per folder"):
        compile_template("{*} - {*} - {author} - {title}")
    with pytest.raises(LayoutTemplateError, match=r"'\*\*'"):
        compile_template("**/{author}/**/{title}/**")


def test_overlong_paths_and_names_are_not_matched() -> None:
    assert match_path(f"Gibson, William/{'a' * 300}.txt", ["{author}/**"]) is None
    deep = "/".join(["folder"] * 200)
    assert match_path(f"{deep}/x.txt", ["**/{author}/{title}"]) is None


def test_adversarial_names_stay_fast() -> None:

    name = "x - " * 63  # 252 characters, just inside the segment limit
    path = "/".join([name] * 3) + "/" + name + ".epub"
    templates = [
        "{*} - {author} - {title}!",
        "**/{*} - {authors} - {series}!/**",
        "{*} - {*}/{author} - {*}/**/{title}!",
    ]
    started = time.perf_counter()
    assert match_path(path, templates) is None
    assert time.perf_counter() - started < 1.0


def test_segments_match_independently() -> None:
    hints = match_path(
        "Fiction/Weber, David/Honor Harrington/On Basilisk Station.epub",
        ["**/{author}/{series}/{title}"],
    )
    assert hints is not None
    assert (hints.authors, hints.series, hints.title) == (
        ["David Weber"],
        "Honor Harrington",
        "On Basilisk Station",
    )
