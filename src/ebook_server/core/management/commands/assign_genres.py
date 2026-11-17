"""Backfill tag-derived genres and list tags that map to no genre (#50)."""

from __future__ import annotations

import argparse
from collections import Counter
from typing import Any

from django.core.management.base import BaseCommand
from django.db import transaction

from ebook_server.core import genres
from ebook_server.core.models import ImportJob, MetadataCandidate, ReviewTask


class Command(BaseCommand):
    help = (
        "Attach genres to already imported books from their accepted candidate's subjects "
        "(or the file's embedded subjects), using the same rules as intake. Books whose "
        "genres a reviewer or admin decided (including deciding on none) are skipped. "
        "Re-run after adding genres or aliases."
    )

    def add_arguments(self, parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Report what would be linked without changing anything.",
        )
        parser.add_argument(
            "--unmapped",
            type=int,
            default=0,
            metavar="N",
            help="Also list the N most frequent subject tags that map to no genre.",
        )

    def handle(self, *args: Any, **options: Any) -> None:
        linked_books = 0
        linked_genres = 0
        with transaction.atomic():
            for job in (
                ImportJob.objects.filter(state=ImportJob.State.COMPLETE, book__isnull=False)
                .select_related("book")
                .order_by("created_at")
            ):
                book = job.book
                provider_subjects = _accepted_candidate_subjects(job)
                if book is None or provider_subjects is None:
                    continue
                added = genres.assign_automatic(
                    book,
                    provider_subjects=provider_subjects,
                    embedded_subjects=genres.embedded_subjects(job.evidence),
                )
                if added:
                    linked_books += 1
                    linked_genres += len(added)
                    self.stdout.write(f"book {book.pk}: {', '.join(genre.name for genre in added)}")
            if options["dry_run"]:
                transaction.set_rollback(True)
        verb = "Would link" if options["dry_run"] else "Linked"
        self.stdout.write(f"{verb} {linked_genres} genre(s) to {linked_books} book(s).")
        if options["unmapped"]:
            self._report_unmapped(options["unmapped"])

    def _report_unmapped(self, limit: int) -> None:
        index = genres.build_index()
        counts: Counter[str] = Counter()
        for subjects in MetadataCandidate.objects.values_list("subjects", flat=True):
            counts.update(_unmapped(subjects, index))
        for evidence in ImportJob.objects.values_list("evidence", flat=True):
            counts.update(_unmapped(genres.embedded_subjects(evidence), index))
        self.stdout.write(f"Most frequent unmapped tags (top {limit}):")
        for tag, count in counts.most_common(limit):
            self.stdout.write(f"{count:6d}  {tag}")


def _accepted_candidate_subjects(job: ImportJob) -> list[Any] | None:
    """Provider subjects of the candidate the job was linked through.

    ``None`` means the book was linked by merging into an existing book, which never
    assigns genres automatically.
    """
    task = (
        ReviewTask.objects.filter(import_job=job, state=ReviewTask.State.RESOLVED)
        .select_related("selected_candidate")
        .order_by("-decided_at")
        .first()
    )
    if task is not None:
        if task.decision == ReviewTask.Decision.MERGED_BOOK:
            return None
        if task.selected_candidate is not None:
            return list(task.selected_candidate.subjects or [])
        return []  # manual entry: only the file's own tags apply
    candidate = (
        MetadataCandidate.objects.filter(
            import_job=job, state=MetadataCandidate.CandidateState.MATCH
        )
        .order_by("-score")
        .first()
    )
    return list(candidate.subjects or []) if candidate is not None else []


def _unmapped(subjects: Any, index: dict[str, Any]) -> list[str]:
    if not isinstance(subjects, list):
        return []
    return [match.tag for match in genres.resolve_tags(subjects, index) if match.genre is None]
