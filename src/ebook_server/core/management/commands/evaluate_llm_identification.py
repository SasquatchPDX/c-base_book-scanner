"""Offline evaluation of the LLM identification assist against hand-labelled files (#26).

Run by hand against the real AI server, never in CI (the test suite runs it against a
fake client). Path-only mode: the fixture has names and paths, no file content, so this
measures the hardest case. Example::

    uv run python manage.py evaluate_llm_identification \\
        --sample tests/fixtures/real_world_samples/ebooks-share-filetree.csv \\
        --labels docs/design/data/llm-eval-labels.csv --with-providers

The command ignores ``BOOK_SCANNER_LLM_ENABLED`` (it is how you decide whether to turn
the automatic trigger on) but still enforces the local-only service check.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from ebook_server.core import identification, llm, llm_identification

DEFAULT_SAMPLE = "tests/fixtures/real_world_samples/ebooks-share-filetree.csv"
DEFAULT_LABELS = "docs/design/data/llm-eval-labels.csv"
TITLE_MATCH_THRESHOLD = 0.85
AUTHOR_MATCH_THRESHOLD = 0.9


@dataclass
class Tally:
    rows: int = 0
    schema_valid: int = 0
    invalid_output: int = 0
    failed: int = 0
    title_proposed: int = 0
    title_correct: int = 0
    author_labelled: int = 0
    author_proposed: int = 0
    author_correct: int = 0
    grounded_hints: int = 0
    ungrounded_hints: int = 0
    isbn_dropped: int = 0
    chapter_predicted: int = 0
    chapter_predicted_correct: int = 0
    chapter_labelled: int = 0
    latencies: list[float] = field(default_factory=list)
    provider_rows: int = 0
    baseline_hits: int = 0
    llm_hits: int = 0
    baseline_top1: int = 0
    llm_top1: int = 0
    baseline_top5: int = 0
    llm_top5: int = 0


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 3) if denominator else None


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(fraction * (len(ordered) - 1))))
    return round(ordered[index], 3)


def _title_matches(hint: str, label: str) -> bool:
    return identification._title_match_score(hint, label) >= TITLE_MATCH_THRESHOLD


def _author_matches(hint: str, label: str) -> bool:
    return identification._author_match_score([hint], [label]) >= AUTHOR_MATCH_THRESHOLD


def _bool(value: str) -> bool | None:
    lowered = value.strip().lower()
    if lowered in {"true", "yes", "1"}:
        return True
    if lowered in {"false", "no", "0"}:
        return False
    return None


def _read_csv(path: Path) -> list[dict[str, str]]:
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))
    except OSError as exc:
        raise CommandError(f"cannot read {path}: {exc}") from exc


def _candidates_hit(
    candidates: list[identification.IdentificationCandidate],
    label_title: str,
) -> tuple[bool, bool, bool]:
    hit = bool(candidates)
    top1 = bool(candidates) and _title_matches(candidates[0].title, label_title)
    top5 = any(_title_matches(candidate.title, label_title) for candidate in candidates[:5])
    return hit, top1, top5


class Command(BaseCommand):
    help = (
        "Run the LLM identification assist over a hand-labelled sample and report hint "
        "precision, grounding, chapter detection and latency (and, with "
        "--with-providers, provider hit rates against today's pipeline)."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--sample", default=DEFAULT_SAMPLE, help="file-tree CSV")
        parser.add_argument("--labels", default=DEFAULT_LABELS, help="labels CSV")
        parser.add_argument("--limit", type=int, default=0, help="evaluate at most N rows")
        parser.add_argument(
            "--with-providers",
            action="store_true",
            help="also query the configured metadata providers (network; slow, rate-limited)",
        )
        parser.add_argument("--json", action="store_true", help="print the report as JSON")

    def handle(self, *args: Any, **options: Any) -> None:
        base_dir = Path(settings.BASE_DIR).parent
        sample_path = Path(options["sample"])
        labels_path = Path(options["labels"])
        if not sample_path.is_absolute():
            sample_path = base_dir / sample_path
        if not labels_path.is_absolute():
            labels_path = base_dir / labels_path
        sample = _read_csv(sample_path)
        labels = _read_csv(labels_path)
        if options["limit"] > 0:
            labels = labels[: options["limit"]]

        by_path = {row["relative_path"]: row for row in sample}
        by_folder: dict[str, list[str]] = defaultdict(list)
        for row in sample:
            by_folder[row.get("folder", "")].append(row["filename"])

        try:
            client = llm.get_llm_client()
        except llm.LLMConfigurationError as exc:
            raise CommandError(str(exc)) from exc

        tally = Tally()
        missing: list[str] = []
        for label in labels:
            sample_row = by_path.get(label["relative_path"])
            if sample_row is None:
                missing.append(label["relative_path"])
                continue
            tally.rows += 1
            self._evaluate_row(
                client, tally, sample_row, label, by_folder, options["with_providers"]
            )

        report = self._report(tally, client, missing, options["with_providers"])
        if options["json"]:
            self.stdout.write(json.dumps(report, indent=2, sort_keys=True))
            return
        for key, value in report.items():
            self.stdout.write(f"{key}: {value}")

    def _evaluate_row(
        self,
        client: llm.LLMClientProtocol,
        tally: Tally,
        row: dict[str, str],
        label: dict[str, str],
        by_folder: dict[str, list[str]],
        with_providers: bool,
    ) -> None:
        relative_path = row["relative_path"]
        filename = row["filename"]
        siblings = [name for name in by_folder.get(row.get("folder", ""), []) if name != filename]
        bundle = llm.build_evidence_bundle(
            relative_path=llm.bounded_relative_path(relative_path),
            filename=filename,
            siblings=sorted(siblings),
        )
        label_title = label.get("title", "").strip()
        label_author = label.get("author", "").strip()
        label_single = _bool(label.get("is_single_book", ""))
        if label_single is False:
            tally.chapter_labelled += 1
        if label_author:
            tally.author_labelled += 1

        started = time.monotonic()
        try:
            completion = client.complete(bundle)
        except llm.LLMOutputInvalid:
            tally.invalid_output += 1
            tally.latencies.append(time.monotonic() - started)
            return
        except (llm.LLMClientError, llm.LLMConfigurationError):
            tally.failed += 1
            return
        tally.latencies.append(time.monotonic() - started)
        tally.schema_valid += 1
        hints = llm.ground_output(completion.output, bundle)
        tally.grounded_hints += hints.grounded_count
        tally.ungrounded_hints += hints.ungrounded_count
        tally.isbn_dropped += len(hints.report.get("isbn_hints_dropped", []))

        if hints.title_hints:
            tally.title_proposed += 1
            if label_title and _title_matches(hints.title_hints[0], label_title):
                tally.title_correct += 1
        if label_author and hints.author_hints:
            tally.author_proposed += 1
            if _author_matches(hints.author_hints[0], label_author):
                tally.author_correct += 1
        if completion.output.is_single_book is False:
            tally.chapter_predicted += 1
            if label_single is False:
                tally.chapter_predicted_correct += 1

        if with_providers and label_title:
            tally.provider_rows += 1
            base = identification.build_identification_input(
                source_path=str(PurePosixPath(relative_path)),
                original_filename=filename,
                extracted_metadata={},
            )
            baseline = identification.resolve_candidates(base)
            hinted = (
                identification.resolve_candidates(
                    llm_identification.hint_identification_input(hints, base),
                )
                if hints.has_hints()
                else []
            )
            hit, top1, top5 = _candidates_hit(baseline, label_title)
            tally.baseline_hits += hit
            tally.baseline_top1 += top1
            tally.baseline_top5 += top5
            # The assist only adds candidates, so the combined list is what a reviewer sees.
            combined = sorted([*baseline, *hinted], key=lambda item: item.score, reverse=True)
            hit, top1, top5 = _candidates_hit(combined, label_title)
            tally.llm_hits += hit
            tally.llm_top1 += top1
            tally.llm_top5 += top5

    def _report(
        self,
        tally: Tally,
        client: llm.LLMClientProtocol,
        missing: list[str],
        with_providers: bool,
    ) -> dict[str, Any]:
        total_hints = tally.grounded_hints + tally.ungrounded_hints
        report: dict[str, Any] = {
            "model": getattr(client, "model", ""),
            "prompt_template_version": llm.PROMPT_TEMPLATE_VERSION,
            "rows_evaluated": tally.rows,
            "rows_missing_from_sample": len(missing),
            "schema_valid": tally.schema_valid,
            "invalid_output": tally.invalid_output,
            "request_failures": tally.failed,
            "title_hint_precision": _ratio(tally.title_correct, tally.title_proposed),
            "title_hint_coverage": _ratio(tally.title_proposed, tally.schema_valid),
            "author_hint_precision": _ratio(tally.author_correct, tally.author_proposed),
            "author_hint_coverage": _ratio(tally.author_proposed, tally.author_labelled),
            "ungrounded_hint_share": _ratio(tally.ungrounded_hints, total_hints),
            "isbn_hints_dropped": tally.isbn_dropped,
            "chapter_detection_precision": _ratio(
                tally.chapter_predicted_correct, tally.chapter_predicted
            ),
            "chapter_detection_recall": _ratio(
                tally.chapter_predicted_correct, tally.chapter_labelled
            ),
            "latency_p50_seconds": _percentile(tally.latencies, 0.5),
            "latency_p95_seconds": _percentile(tally.latencies, 0.95),
            "latency_mean_seconds": (
                round(statistics.fmean(tally.latencies), 3) if tally.latencies else None
            ),
        }
        if with_providers:
            report.update(
                {
                    "provider_rows": tally.provider_rows,
                    "baseline_hit_rate": _ratio(tally.baseline_hits, tally.provider_rows),
                    "with_llm_hit_rate": _ratio(tally.llm_hits, tally.provider_rows),
                    "baseline_top1_correct": _ratio(tally.baseline_top1, tally.provider_rows),
                    "with_llm_top1_correct": _ratio(tally.llm_top1, tally.provider_rows),
                    "baseline_top5_correct": _ratio(tally.baseline_top5, tally.provider_rows),
                    "with_llm_top5_correct": _ratio(tally.llm_top5, tally.provider_rows),
                },
            )
        return report
