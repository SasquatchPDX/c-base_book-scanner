"""The measurement uses real intake, isolates caller data, and repeats exactly."""

from __future__ import annotations

import json
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from django.conf import settings
from django.core.management import call_command
from django.core.management.base import CommandError

from ebook_server.core.intake_evaluation import Corpus, FileLabel, read_corpus
from ebook_server.core.management.commands import evaluate_intake as evaluator_command
from ebook_server.core.management.commands.evaluate_intake import (
    fingerprint,
    isolated_environment,
    validate_worker_root,
)
from ebook_server.core.models import Book

CORPUS = Path(__file__).parent / "fixtures/intake_evaluation/corpus.json"
SCORECARD = Path(__file__).parent.parent / "docs/design/data/intake-scorecard.json"


def test_corpus_has_explicit_work_edition_and_review_labels_for_all_groups() -> None:
    corpus = read_corpus(CORPUS)
    assert len(corpus.cases) == 31
    assert sum(len(case.files) for case in corpus.cases) == 33
    assert {
        "isbn",
        "no-isbn",
        "editions",
        "work-only",
        "coauthors",
        "sidecars",
        "sibling-formats",
        "local-consensus",
        "provider-failure",
        "threshold-boundary",
    } <= {group for case in corpus.cases for group in case.groups}
    assert all(case.rationale for case in corpus.cases)
    assert any(file.require_review for case in corpus.cases for file in case.files)


@pytest.mark.parametrize(
    "path",
    [
        "../escape.epub",
        "a/../../escape.epub",
        "/absolute.epub",
        "C:/absolute.epub",
        "C:relative.epub",
        "a\\escape.epub",
        "a//empty.epub",
        "a/./dot.epub",
        "run.py",
    ],
)
def test_fixture_file_paths_cannot_escape_private_source_root(path: str) -> None:
    with pytest.raises(ValueError, match="fixture paths"):
        FileLabel(path=path, work="labelled")


def test_unknown_edition_labels_fail_instead_of_skewing_the_scorecard() -> None:
    raw = json.loads(CORPUS.read_text(encoding="utf-8"))
    raw["cases"][0]["files"][0]["edition"] = "unlabelled"
    with pytest.raises(ValueError, match="edition must belong"):
        Corpus.model_validate(raw)


def test_isolated_process_does_not_inherit_deployment_or_proxy_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BOOK_SCANNER_ENVIRONMENT", "production")
    monkeypatch.setenv("BOOK_SCANNER_REDIS_URL", "redis://unreachable/3")
    monkeypatch.setenv("BOOK_SCANNER_STORAGE_ROOT", "unrelated-original-storage")
    monkeypatch.setenv("BOOK_SCANNER_LLM_ENABLED", "true")
    monkeypatch.setenv("HTTPS_PROXY", "http://unreachable-proxy")
    environment = isolated_environment(tmp_path)
    assert environment["BOOK_SCANNER_ENVIRONMENT"] == "test"
    assert environment["BOOK_SCANNER_REDIS_URL"] == ""
    assert environment["BOOK_SCANNER_STORAGE_ROOT"] == str(tmp_path / "storage")
    assert environment["BOOK_SCANNER_LLM_ENABLED"] == "false"
    assert environment["PYTHONPATH"] == str(settings.BASE_DIR)
    assert "HTTPS_PROXY" not in environment


def test_internal_worker_refuses_callers_database_before_migrating(tmp_path: Path) -> None:
    with pytest.raises(CommandError, match="isolated SQLite"):
        validate_worker_root(tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_internal_worker_also_refuses_an_existing_database_at_expected_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "evaluation.sqlite3"
    database.write_bytes(b"must not migrate an existing database")
    monkeypatch.setenv("BOOK_SCANNER_ENVIRONMENT", "test")
    monkeypatch.setattr("django.conf.settings.APP_DATA_DIR", tmp_path / "storage")
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_QUEUE_URL", "")
    monkeypatch.setattr(
        evaluator_command,
        "connections",
        {
            "default": SimpleNamespace(
                settings_dict={"ENGINE": "django.db.backends.sqlite3", "NAME": str(database)}
            )
        },
    )
    with pytest.raises(CommandError, match="isolated SQLite"):
        validate_worker_root(tmp_path)
    assert database.read_bytes() == b"must not migrate an existing database"


def test_source_fingerprints_ignore_git_line_ending_differences(tmp_path: Path) -> None:
    unix = tmp_path / "unix.py"
    windows = tmp_path / "windows.py"
    unix.write_bytes(b"first\nsecond\n")
    windows.write_bytes(b"first\r\nsecond\r\n")
    assert fingerprint(unix) == fingerprint(windows)


@pytest.mark.django_db
def test_real_intake_scorecard_is_repeatable_without_mutating_caller_data(tmp_path: Path) -> None:
    sentinel = Book.objects.create(title="Caller database must survive")
    untouched = tmp_path / "original.txt"
    untouched.write_bytes(b"caller original")
    threshold = settings.BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT

    def measure() -> tuple[str, dict[str, Any]]:
        output = StringIO()
        call_command("evaluate_intake", corpus=str(CORPUS), trial_threshold=0.8, stdout=output)
        payload = output.getvalue()
        return payload, json.loads(payload)

    first_payload, report = measure()
    second_payload, _ = measure()
    assert first_payload == second_payload
    assert Book.objects.count() == 1
    sentinel.refresh_from_db()
    assert sentinel.title == "Caller database must survive"
    assert untouched.read_bytes() == b"caller original"
    assert settings.BOOK_SCANNER_IDENTIFICATION_AUTO_ACCEPT == threshold
    baseline = report["baseline"]
    trial = report["trial"]
    assert baseline["threshold"] == threshold
    assert baseline["totals"]["rows"] == trial["totals"]["rows"] == 33
    assert baseline["totals"]["failed"] == trial["totals"]["failed"] == 0
    assert baseline["totals"]["provider_failure_rows"] == 2
    assert trial["totals"]["review"] < baseline["totals"]["review"]
    snapshot = json.loads(SCORECARD.read_text(encoding="utf-8"))
    # Evidence improvements are welcome: regenerate the snapshot, never require a bug.
    for name in ("baseline", "trial"):
        assert report[name]["totals"] == snapshot[name]["totals"]
        assert report[name]["groups"] == snapshot[name]["groups"]
    rows = {row["case"]: row for row in baseline["rows"]}
    assert rows["local-no-provider-record"]["outcome"] == "review"
    assert rows["local-no-provider-record"]["local_suggestion"]
    assert not rows["local-path-only"]["local_suggestion"]
    assert rows["work-only-translations"]["correct_edition"]
    assert rows["work-only-translations"]["selected"]["identifiers"] == []
    assert rows["work-only-translations"]["selected"]["language"] == ""
    siblings = next(case for case in baseline["cases"] if case["case"] == "sibling-formats-opt-in")
    assert siblings["canonical_books"] == 1
    assert siblings["linked_formats"] == ["epub", "txt"]
    # Both files are verified by a provider search run; their identical query is sent
    # once and the result reused for the second format (#220, G2).
    assert siblings["provider_calls"] >= 1
    sibling_rows = [row for row in baseline["rows"] if row["case"] == "sibling-formats-opt-in"]
    assert len(sibling_rows) == 2
    assert all(row["provider_search_runs"] >= 1 for row in sibling_rows)
    assert "no-isbn-fuzzy-threshold" in {row["case"] for row in report["changed_rows"]}
    for name in ("baseline", "trial"):
        measured = {row["case"]: row for row in report[name]["rows"]}
        for case in ("isbn-other-edition", "isbn-author-conflict", "sidecar-contradicts"):
            assert measured[case]["outcome"] == "review"
