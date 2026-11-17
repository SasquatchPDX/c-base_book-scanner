"""Detect and route books split across several per-chapter files in one folder (#60).

Detection works on filenames only: no file content is read. A folder qualifies when
at least ``MIN_NUMBERED_PARTS`` files of one format share a common prefix followed by
a chapter/part/section token and distinct numbers (``ch01_noPW.pdf``,
``Chapter 2.txt``, ``Part III.epub``). Appendix files (``AppA.pdf``,
``Appendix1_noPW.pdf``) and front/back matter (``FM.pdf``, ``Index.pdf``,
``glossary.pdf``) join such a group but never form one on their own. A folder whose
*name* contains a word like "CHAPTER" proves nothing (#60's false alarm: a complete
novel in a folder called ``CHAPTER ONE``); only the file names count.

Volume/book/band numbers are deliberately not part tokens: ``Series Book 1``,
``Series Book 2`` are separate works, not fragments of one.

A detected group is only a suggestion. Its files are hashed, validated and archived as
usual, but instead of N independent identifications a single ``MULTIPART_WORK``
review task is opened once every part has settled. The reviewer either confirms the
set as one Book (each file becomes an ordered part FileVariant) or rejects the
grouping, after which every file is identified as an independent import.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog
from django.conf import settings as django_settings
from django.db import transaction
from django.utils import timezone

from .models import (
    Book,
    FileVariant,
    ImportGroup,
    ImportJob,
    ReviewTask,
    SourceLocation,
)

logger = structlog.get_logger(__name__)

MIN_NUMBERED_PARTS = 3
MAX_PART_NUMBER = 999

# Keyword families that number fragments of one work. Not "vol", "book", "band":
# those usually number separate works in a series.
_MAIN_KEYWORDS = {
    "ch": "chapter",
    "chap": "chapter",
    "chapter": "chapter",
    "chpt": "chapter",
    "kap": "chapter",
    "kapitel": "chapter",
    "part": "part",
    "pt": "part",
    "teil": "part",
    "sec": "section",
    "section": "section",
    "lesson": "lesson",
    "lektion": "lesson",
}
_APPENDIX_KEYWORDS = ("appendix", "anhang", "app")
_FRONT_MATTER = {
    "fm": "Front matter",
    "front matter": "Front matter",
    "frontmatter": "Front matter",
    "title page": "Title page",
    "toc": "Contents",
    "contents": "Contents",
    "preface": "Preface",
    "foreword": "Foreword",
    "introduction": "Introduction",
    "intro": "Introduction",
}
_BACK_MATTER = {
    "glossary": "Glossary",
    "bibliography": "Bibliography",
    "references": "References",
    "index": "Index",
    "bm": "Back matter",
    "back matter": "Back matter",
    "backmatter": "Back matter",
}

_SEP = r"[\s._-]*"
_NOT_WORD_BEFORE = r"(?<![a-z])"
_NOT_WORD_AFTER = r"(?![a-z0-9])"
_MAIN_TOKEN = re.compile(
    _NOT_WORD_BEFORE
    + r"(?P<kw>"
    + "|".join(sorted(_MAIN_KEYWORDS, key=len, reverse=True))
    + r")(?:"
    + _SEP
    + r"(?P<num>\d{1,3})|[\s._-]+(?P<roman>[ivxlc]{1,7}))"
    + _NOT_WORD_AFTER,
    re.IGNORECASE,
)
_APPENDIX_TOKEN = re.compile(
    _NOT_WORD_BEFORE
    + r"(?P<kw>"
    + "|".join(_APPENDIX_KEYWORDS)
    + r")"
    + _SEP
    + r"(?P<id>\d{1,2}|[a-z])"
    + _NOT_WORD_AFTER,
    re.IGNORECASE,
)
_ROMAN = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100}

RANK_FRONT = 0
RANK_MAIN = 1
RANK_APPENDIX = 2
RANK_BACK = 3


def _roman_to_int(raw: str) -> int | None:
    total = 0
    previous = 0
    for char in reversed(raw.lower()):
        value = _ROMAN.get(char)
        if value is None:
            return None
        if value < previous:
            total -= value
        else:
            total += value
            previous = value
    return total if 0 < total <= MAX_PART_NUMBER else None


def _normalize_affix(value: str) -> str:
    return " ".join(re.sub(r"[\s._-]+", " ", value).split()).casefold()


@dataclass(frozen=True)
class DetectedPart:
    """One file of a detected multi-part work, in reading order."""

    filename: str
    index: int
    label: str
    kind: str


@dataclass(frozen=True)
class DetectedGroup:
    """Files in one folder that look like ordered parts of a single work."""

    key: str
    format_name: str
    prefix: str
    keyword: str
    parts: tuple[DetectedPart, ...]
    missing_numbers: tuple[int, ...] = ()

    def evidence(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "format": self.format_name,
            "prefix": self.prefix,
            "keyword": self.keyword,
            "part_count": len(self.parts),
            "missing_numbers": list(self.missing_numbers),
            "parts": [
                {"filename": part.filename, "index": part.index, "label": part.label}
                for part in self.parts
            ],
        }


@dataclass
class _Candidate:
    filename: str
    rank: int
    sort_key: tuple[int, ...]
    label: str
    kind: str
    numbers: tuple[int, ...] = ()


@dataclass
class _Bucket:
    prefix: str
    keyword: str
    format_name: str
    main: list[_Candidate] = field(default_factory=list)
    suffixes: set[str] = field(default_factory=set)


def _main_tokens(stem: str) -> list[re.Match[str]]:
    return list(_MAIN_TOKEN.finditer(stem))


def _token_number(match: re.Match[str]) -> int | None:
    if match.group("num") is not None:
        value = int(match.group("num"))
        return value if value <= MAX_PART_NUMBER else None
    roman = match.group("roman")
    return _roman_to_int(roman) if roman else None


def _format_for(filename: str) -> str | None:
    from .intake import ValidationError, detect_format

    try:
        return detect_format(filename)[0]
    except ValidationError:
        return None


def detect_multipart_groups(filenames: Iterable[str]) -> list[DetectedGroup]:
    """Group the supported files of ONE folder into likely multi-part works.

    Pure function of the names: callers pass every intake-supported filename in the
    folder (not only new ones) so the result does not depend on discovery order.
    """
    names = sorted({name for name in filenames if name})
    buckets: dict[tuple[str, str, str], _Bucket] = {}
    leftovers: list[tuple[str, str, str]] = []  # (filename, stem, format)
    for name in names:
        format_name = _format_for(name)
        if format_name is None:
            continue
        stem = Path(name).stem
        tokens = _main_tokens(stem)
        numbers = tuple(n for n in (_token_number(token) for token in tokens) if n is not None)
        if not tokens or len(numbers) != len(tokens):
            leftovers.append((name, stem, format_name))
            continue
        first = tokens[0]
        family = _MAIN_KEYWORDS[first.group("kw").lower()]
        prefix = _normalize_affix(stem[: first.start()])
        key = (prefix, family, format_name)
        bucket = buckets.setdefault(
            key, _Bucket(prefix=prefix, keyword=family, format_name=format_name)
        )
        bucket.main.append(
            _Candidate(
                filename=name,
                rank=RANK_MAIN,
                sort_key=numbers,
                label=f"{family.capitalize()} {numbers[0]}",
                kind=family,
                numbers=numbers,
            ),
        )
        bucket.suffixes.add(_normalize_affix(stem[tokens[-1].end() :]))

    groups: list[DetectedGroup] = []
    claimed: set[str] = set()
    for (prefix, family, format_name), bucket in sorted(buckets.items()):
        distinct = {candidate.numbers for candidate in bucket.main}
        if len(bucket.main) < MIN_NUMBERED_PARTS or len(distinct) != len(bucket.main):
            continue
        members = list(bucket.main)
        common_suffix = next(iter(bucket.suffixes)) if len(bucket.suffixes) == 1 else ""
        for name, stem, leftover_format in leftovers:
            if leftover_format != format_name or name in claimed:
                continue
            extra = _auxiliary_candidate(name, stem, prefix=prefix, suffix=common_suffix)
            if extra is not None:
                members.append(extra)
                claimed.add(name)
        members.sort(key=lambda candidate: (candidate.rank, candidate.sort_key, candidate.filename))
        first_numbers = sorted({candidate.numbers[0] for candidate in bucket.main})
        missing = tuple(
            number
            for number in range(first_numbers[0], first_numbers[-1] + 1)
            if number not in first_numbers
        )
        parts = tuple(
            DetectedPart(
                filename=candidate.filename,
                index=position,
                label=candidate.label,
                kind=candidate.kind,
            )
            for position, candidate in enumerate(members, start=1)
        )
        groups.append(
            DetectedGroup(
                key=f"{format_name}|{family}|{prefix}",
                format_name=format_name,
                prefix=prefix,
                keyword=family,
                parts=parts,
                missing_numbers=missing,
            ),
        )
    return groups


def _auxiliary_candidate(name: str, stem: str, *, prefix: str, suffix: str) -> _Candidate | None:
    """Appendix or front/back-matter file that belongs next to a numbered group."""
    normalized = _normalize_affix(stem)
    if prefix:
        if not normalized.startswith(prefix):
            return None
        normalized = normalized[len(prefix) :].strip()
    if suffix and normalized.endswith(suffix):
        normalized = normalized[: -len(suffix)].strip()
    if normalized in _FRONT_MATTER:
        order = list(_FRONT_MATTER).index(normalized)
        return _Candidate(name, RANK_FRONT, (order,), _FRONT_MATTER[normalized], "front_matter")
    if normalized in _BACK_MATTER:
        order = list(_BACK_MATTER).index(normalized)
        return _Candidate(name, RANK_BACK, (order,), _BACK_MATTER[normalized], "back_matter")
    match = _APPENDIX_TOKEN.fullmatch(normalized.replace(" ", ""))
    if match is None:
        return None
    raw_id = match.group("id")
    if raw_id.isdigit():
        number = int(raw_id)
        label = f"Appendix {number}"
    else:
        number = ord(raw_id.lower()) - ord("a") + 1
        label = f"Appendix {raw_id.upper()}"
    return _Candidate(name, RANK_APPENDIX, (number,), label, "appendix")


# --- intake orchestration -------------------------------------------------------------

SETTLED_PART_STATES = (
    ImportJob.State.LOW_CONFIDENCE,
    ImportJob.State.COMPLETE,
    ImportJob.State.FAILED,
)


class MultipartError(RuntimeError):
    """Raised when a multi-part review action cannot be applied."""


def detection_enabled() -> bool:
    raw = getattr(django_settings, "BOOK_SCANNER_MULTIPART_DETECTION", True)
    if isinstance(raw, str):
        return raw.strip().lower() not in {"0", "false", "no", "off"}
    return bool(raw)


def _folder_supported_names(folder: Path) -> list[str]:
    from .intake import _is_supported_file

    try:
        return [
            child.name
            for child in folder.iterdir()
            if not child.name.startswith((".", "~"))
            and child.is_file()
            and _is_supported_file(child)
        ]
    except OSError:
        return []


def groups_for_folder(folder: Path) -> list[DetectedGroup]:
    return detect_multipart_groups(_folder_supported_names(folder))


def assign_discovered_job(
    job: ImportJob,
    source_location: SourceLocation,
    *,
    folder_groups: dict[Path, list[DetectedGroup]],
) -> ImportGroup | None:
    """Attach a newly discovered source job to its folder's pending multi-part group.

    ``folder_groups`` caches detection per folder for one discovery pass. Groups that
    were already confirmed or rejected are never reused: a later file in such a
    folder is an ordinary independent import.
    """
    if not detection_enabled():
        return None
    path = Path(job.source_path)
    folder = path.parent
    if folder not in folder_groups:
        folder_groups[folder] = groups_for_folder(folder)
    for detected in folder_groups[folder]:
        part = next((p for p in detected.parts if p.filename == path.name), None)
        if part is None:
            continue
        group, created = ImportGroup.objects.get_or_create(
            source_location=source_location,
            folder=str(folder),
            group_key=detected.key,
            state=ImportGroup.State.PENDING,
            defaults={"kind": ImportGroup.Kind.MULTIPART_WORK, "detection": detected.evidence()},
        )
        if not created and group.detection != detected.evidence():
            group.detection = detected.evidence()
            group.save(update_fields=["detection", "updated_at"])
        job.import_group = group
        job.part_index = part.index
        job.part_label = part.label
        job.save(update_fields=["import_group", "part_index", "part_label", "updated_at"])
        if created:
            logger.info(
                "multipart_group_detected",
                import_group_id=str(group.id),
                source_location_id=source_location.pk,
                part_count=len(detected.parts),
                format=detected.format_name,
            )
        return group
    return None


def pending_group_for(job: ImportJob) -> ImportGroup | None:
    if job.import_group_id is None:
        return None
    group = job.import_group
    if group is None or group.state != ImportGroup.State.PENDING:
        return None
    return group


def _group_signals(group: ImportGroup, jobs: list[ImportJob]) -> Any:
    """Identification input for the whole work: the folder name, not chapter titles."""
    from . import identification

    folder_name = Path(group.folder).name
    title_hints, author_hints = identification.parse_filename_signals(f"{folder_name}.x")
    isbns: list[str] = []
    language = publisher = None
    for job in jobs:
        evidence = job.evidence if isinstance(job.evidence, dict) else {}
        for name in identification.evidence_author_names(evidence):
            if name not in author_hints:
                author_hints.append(name)
        # Folder-layout authors (#35) describe the folder, so they hold for the whole
        # work; a layout *title* may come from the chapter file name and is skipped.
        layout = evidence.get(identification.PATH_LAYOUT_EVIDENCE_KEY)
        layout_authors = layout.get("authors") if isinstance(layout, dict) else None
        for name in layout_authors if isinstance(layout_authors, list) else []:
            if isinstance(name, str) and name.strip() and name.strip() not in author_hints:
                author_hints.append(name.strip())
        for field_name in ("isbn", "isbn10", "isbn13", "identifier"):
            value = evidence.get(field_name)
            if isinstance(value, str):
                for isbn in identification.normalize_isbns(value):
                    if isbn not in isbns:
                        isbns.append(isbn)
        language = language or identification.normalize_text(evidence.get("language")) or None
        publisher = publisher or identification.normalize_text(evidence.get("publisher")) or None
    parent_name = Path(group.folder).parent.name
    return identification.IdentificationInput(
        title_hints=title_hints,
        author_hints=author_hints,
        isbns=isbns,
        folder_hints=[name for name in (folder_name, parent_name) if name],
        language=language,
        publisher=publisher,
        description=None,
    )


def group_jobs(group: ImportGroup) -> list[ImportJob]:
    return list(group.import_jobs.order_by("part_index", "original_filename"))


def maybe_open_review(group: ImportGroup) -> ReviewTask | None:
    """Open (or refresh) the group's review task once every part has settled.

    Called by each part job after archiving. Identification runs outside the lock;
    two workers finishing together may both search, but only one task is created.
    """
    from . import identification, intake

    jobs = group_jobs(group)
    if any(job.state not in SETTLED_PART_STATES for job in jobs):
        return None
    reviewable = [job for job in jobs if job.state == ImportJob.State.LOW_CONFIDENCE]
    if not reviewable:
        return None
    lead = reviewable[0]
    signals = _group_signals(group, reviewable)
    candidates = identification.resolve_candidates(signals)

    with transaction.atomic():
        locked = ImportGroup.objects.select_for_update().get(pk=group.pk)
        if locked.state != ImportGroup.State.PENDING:
            return None
        task = (
            ReviewTask.objects.filter(
                task_type=ReviewTask.TaskType.MULTIPART_WORK,
                import_job__import_group=locked,
            )
            .order_by("-created_at")
            .first()
        )
        if task is not None and task.state not in {
            ReviewTask.State.OPEN,
            ReviewTask.State.CLAIMED,
        }:
            return None
        if task is not None and task.import_job_id != lead.pk:
            # The lead part changed (a late earlier-numbered part arrived): move the
            # task and its candidates to the new lead so actions apply consistently.
            task.import_job = lead
        intake._save_metadata_candidates(lead, candidates, allow_match=False)
        folder_name = Path(locked.folder).name
        title = signals.best_title() or folder_name
        snapshot = [
            {
                "provider_id": candidate.provider_id,
                "provider_ref": candidate.provider_ref,
                "score": float(candidate.score),
            }
            for candidate in candidates
        ]
        evidence = {
            "multipart": locked.detection,
            "folder": folder_name,
            "parts": [
                {
                    "import_job_id": str(job.id),
                    "filename": job.original_filename,
                    "part_index": job.part_index,
                    "part_label": job.part_label,
                    "state": job.state,
                }
                for job in jobs
            ],
        }
        values = {
            "inferred_title": intake._fit(ReviewTask, "inferred_title", title),
            "inferred_authors": signals.author_hints,
            "embedded_metadata": evidence,
            "provider_snapshot": snapshot,
            "source_filename": intake._fit(
                ReviewTask, "source_filename", f"{folder_name} ({len(reviewable)} parts)"
            ),
            "source_path_hint": intake._fit(ReviewTask, "source_path_hint", locked.folder),
            "decision_payload": {"candidate_count": len(candidates), "multipart": True},
        }
        if task is None:
            task = ReviewTask.objects.create(
                import_job=lead,
                task_type=ReviewTask.TaskType.MULTIPART_WORK,
                created_by=lead.uploader,
                uploader=lead.uploader,
                state=ReviewTask.State.OPEN,
                **values,
            )
        else:
            for name, value in values.items():
                setattr(task, name, value)
            task.save()
    logger.info(
        "multipart_review_opened",
        import_group_id=str(group.id),
        review_task_id=task.id,
        part_count=len(reviewable),
        candidate_count=len(candidates),
    )
    return task


def task_group(task: ReviewTask) -> ImportGroup | None:
    if task.task_type != ReviewTask.TaskType.MULTIPART_WORK or task.import_job is None:
        return None
    return task.import_job.import_group


def confirmation_blocker(task: ReviewTask) -> str | None:
    """Why the parts of a multi-part review cannot be confirmed right now, if anything.

    Checked before a review action creates a Book, so a refused confirmation leaves
    no orphan record behind.
    """
    if task.task_type != ReviewTask.TaskType.MULTIPART_WORK:
        return None
    group = task_group(task)
    if group is None or group.state != ImportGroup.State.PENDING:
        return "multi-part group was already decided"
    jobs = group_jobs(group)
    if any(job.state not in SETTLED_PART_STATES for job in jobs):
        return "some parts are still being processed; try again shortly"
    if not any(job.state == ImportJob.State.LOW_CONFIDENCE for job in jobs):
        return "no archived parts are waiting for review"
    return None


def update_part_order(task: ReviewTask, order: dict[str, tuple[int, str]]) -> None:
    """Apply reviewer-edited ``{import_job_id: (part_index, part_label)}`` values."""
    group = task_group(task)
    if group is None or group.state != ImportGroup.State.PENDING:
        raise MultipartError("review task has no pending multi-part group")
    jobs = {str(job.id): job for job in group_jobs(group)}
    indexes = [index for index, _label in order.values()]
    if len(set(indexes)) != len(indexes) or any(index < 1 for index in indexes):
        raise MultipartError("part positions must be distinct positive numbers")
    with transaction.atomic():
        for job_id, (index, label) in order.items():
            job = jobs.get(job_id)
            if job is None:
                raise MultipartError("unknown part")
            job.part_index = index
            job.part_label = label.strip()[:128]
            job.save(update_fields=["part_index", "part_label", "updated_at"])


def confirm_parts(
    *,
    lead_job: ImportJob,
    review_task: ReviewTask,
    book: Book,
) -> list[FileVariant]:
    """Resolve every settled part of the task's group as an ordered part of ``book``.

    Each archived part becomes its own FileVariant carrying ``part_index`` and
    ``part_label``; nothing is merged, rewritten or deleted. Parts that turned out to
    be byte-identical to an existing variant keep that variant untouched.
    """
    from . import intake

    if lead_job.import_group_id is None:
        raise MultipartError("review task has no pending multi-part group")
    variants: list[FileVariant] = []
    first_part: tuple[FileVariant, ImportJob] | None = None
    with transaction.atomic():
        group = ImportGroup.objects.select_for_update().get(pk=lead_job.import_group_id)
        if group.state != ImportGroup.State.PENDING:
            raise MultipartError("multi-part group was already decided")
        jobs = group_jobs(group)
        if any(job.state not in SETTLED_PART_STATES for job in jobs):
            raise MultipartError("some parts are still being processed; try again shortly")
        for job in jobs:
            if job.state != ImportJob.State.LOW_CONFIDENCE:
                continue
            # Already archived and verified: this only records the variant row.
            variant = intake._create_or_reuse_file_variant(
                job,
                book,
                part_index=job.part_index,
                part_label=job.part_label,
            )
            if first_part is None:
                first_part = (variant, job)
            job.book = book
            job.file_variant = variant
            job.storage_location = variant.storage_location
            job.storage_identifier = variant.storage_identifier
            job.state = ImportJob.State.COMPLETE
            job.save(
                update_fields=[
                    "state",
                    "book_id",
                    "file_variant_id",
                    "storage_location_id",
                    "storage_identifier",
                    "updated_at",
                ],
            )
            variants.append(variant)
        if not variants:
            raise MultipartError("no archived parts are waiting for review")
        group.state = ImportGroup.State.CONFIRMED
        group.book = book
        group.decided_at = timezone.now()
        group.save(update_fields=["state", "book", "decided_at", "updated_at"])
    if first_part is not None:
        intake._try_extract_front_cover(*first_part)
    logger.info(
        "multipart_group_confirmed",
        import_group_id=str(group.id),
        review_task_id=review_task.id,
        book_id=book.id,
        part_count=len(variants),
    )
    return variants


def reject_grouping(*, review_task: ReviewTask, reviewer: Any, notes: str = "") -> list[ImportJob]:
    """The files are not parts of one work: identify each as an independent import."""
    from ebook_server.jobs import enqueue

    group = task_group(review_task)
    if group is None or group.state != ImportGroup.State.PENDING:
        raise MultipartError("review task has no pending multi-part group")
    with transaction.atomic():
        locked = ImportGroup.objects.select_for_update().get(pk=group.pk)
        if locked.state != ImportGroup.State.PENDING:
            raise MultipartError("multi-part group was already decided")
        jobs = group_jobs(locked)
        locked.state = ImportGroup.State.REJECTED
        locked.decided_at = timezone.now()
        locked.save(update_fields=["state", "decided_at", "updated_at"])
        review_task.state = ReviewTask.State.RESOLVED
        review_task.decision = ReviewTask.Decision.SPLIT_PARTS
        review_task.reviewer = reviewer
        review_task.decision_notes = notes
        review_task.decided_at = timezone.now()
        review_task.decision_payload = {
            **(
                review_task.decision_payload
                if isinstance(review_task.decision_payload, dict)
                else {}
            ),
            "split_import_job_ids": [str(job.id) for job in jobs],
        }
        review_task.save(
            update_fields=[
                "state",
                "decision",
                "reviewer_id",
                "decision_notes",
                "decided_at",
                "decision_payload",
                "updated_at",
            ],
        )
    # The task's candidates were searched for the whole work, not the lead file.
    from .models import MetadataCandidate

    MetadataCandidate.objects.filter(import_job=review_task.import_job).delete()
    split: list[ImportJob] = []
    for job in jobs:
        if job.state != ImportJob.State.LOW_CONFIDENCE:
            continue
        split.append(job)
        enqueue(identify_split_part, str(job.id))
    logger.info(
        "multipart_group_rejected",
        import_group_id=str(group.id),
        review_task_id=review_task.id,
        part_count=len(split),
    )
    return split


def identify_split_part(job_id: str) -> None:
    """Background job: identify one archived file of a rejected group on its own.

    Same decision rules as ``process_import_job``: auto-accept only a confident,
    author-verified candidate, otherwise open an ordinary review task. The file is
    already archived and verified, so nothing touches the source again.
    """
    from . import identification, intake

    job = ImportJob.objects.select_related("uploader", "import_group").get(pk=job_id)
    if job.state != ImportJob.State.LOW_CONFIDENCE or job.book_id is not None:
        return
    if job.import_group is not None and job.import_group.state == ImportGroup.State.PENDING:
        return
    metadata = dict(job.evidence) if isinstance(job.evidence, dict) else {}
    signals = identification.build_identification_input(
        source_path=job.source_path,
        original_filename=job.original_filename,
        extracted_metadata=metadata,
    )
    candidates = identification.resolve_candidates(signals)
    file_authors = identification.evidence_author_names(metadata)
    decision = intake.decide_auto_accept(candidates, file_authors)
    if decision.blocked_reason is not None:
        metadata[intake.AUTO_ACCEPT_BLOCKED_KEY] = decision.blocked_reason
        metadata[intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY] = decision.details
        metadata[intake.SUGGESTED_AUTHORS_KEY] = file_authors
        job.evidence = metadata
        job.save(update_fields=["evidence", "updated_at"])
    intake._save_metadata_candidates(job, candidates, allow_match=decision.blocked_reason is None)
    if decision.candidate is None:
        intake._create_review_task(job=job, candidates=candidates)
        return
    book = intake._provision_book_from_metadata(
        candidate=decision.candidate,
        fallback_title=decision.candidate.title,
        extra_metadata=metadata,
    )
    variant = intake._create_or_reuse_file_variant(job, book)
    intake._try_extract_front_cover(variant, job)
    job.book = book
    job.file_variant = variant
    job.state = ImportJob.State.COMPLETE
    job.save(update_fields=["state", "book_id", "file_variant_id", "updated_at"])


def group_for_task(task: ReviewTask) -> dict[str, Any] | None:
    """Review-page context: the group's parts in their current order."""
    group = task_group(task)
    if group is None:
        return None
    detection = group.detection if isinstance(group.detection, dict) else {}
    return {
        "id": str(group.id),
        "state": group.state,
        "folder": group.folder,
        "missing_numbers": detection.get("missing_numbers", []),
        "parts": [
            {
                "import_job_id": str(job.id),
                "filename": job.original_filename,
                "part_index": job.part_index,
                "part_label": job.part_label,
                "state": job.state,
                "size_bytes": job.size_bytes,
            }
            for job in group_jobs(group)
        ],
    }
