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

A part that is byte-identical to a file already in the library (attached to another
Book) is not short-circuited onto that Book: it is archived for the group like any
other part, shown on the review as a *duplicate* part, and blocks confirmation until
the reviewer acknowledges it. On confirmation it becomes a part FileVariant of the
group's Book backed by its own archived copy, with the identical variant recorded as
provenance; the other Book and its variant are never touched.
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

# Where one detected member of a group stands (``group_membership``).
MEMBER_ARCHIVED = "archived"  # archived and verified, waiting for the group decision
# Archived for this group, but byte-identical to a file already in the library (on
# another Book). Needs an acknowledgement; confirming makes it a part of this Book too.
MEMBER_DUPLICATE = "duplicate"
# Resolved onto another Book without an archive copy of its own (it was not processed
# as part of this pending group). It cannot become a part, so it counts as absent.
MEMBER_IN_LIBRARY = "in_library"
MEMBER_PENDING = "pending"  # being processed, or present but not imported yet
MEMBER_MISSING = "missing"  # import failed, or the file disappeared before import
MEMBER_SEPARATE = "separate"  # imported on its own, outside this group

# Review-task payload key recording which absent and duplicate parts a reviewer
# acknowledged (absent parts are left out, duplicate parts are included).
INCOMPLETE_ACK_KEY = "multipart_incomplete_acknowledged"

# ``converter_options`` key on a part variant whose bytes equal another library file.
IDENTICAL_TO_KEY = "multipart_identical_to"


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


def _detection_evidence(detected: DetectedGroup, folder: Path) -> dict[str, Any]:
    """Detection evidence plus the expected membership: each detected file's source path.

    Persisted on the group when it is detected, so settlement and confirmation can
    tell a complete group from one whose remaining files were never imported.
    """
    evidence = detected.evidence()
    for part in evidence["parts"]:
        part["source_path"] = str(folder / part["filename"])
    return evidence


def _expected_parts(group: ImportGroup) -> list[dict[str, Any]]:
    detection = group.detection if isinstance(group.detection, dict) else {}
    parts = detection.get("parts")
    if not isinstance(parts, list):
        return []
    return [
        part for part in parts if isinstance(part, dict) and isinstance(part.get("filename"), str)
    ]


def _decided_group_listing(
    source_location: SourceLocation, folder: Path, key: str, filename: str
) -> ImportGroup | None:
    decided = (
        ImportGroup.objects.filter(
            source_location=source_location, folder=str(folder), group_key=key
        )
        .exclude(state=ImportGroup.State.PENDING)
        .order_by("-decided_at")
    )
    for group in decided:
        if any(part["filename"] == filename for part in _expected_parts(group)):
            return group
    return None


def assign_discovered_job(
    job: ImportJob,
    source_location: SourceLocation,
    *,
    folder_groups: dict[Path, list[DetectedGroup]],
) -> ImportGroup | None:
    """Attach a newly discovered source job to its folder's pending multi-part group.

    ``folder_groups`` caches detection per folder for one discovery pass. Groups that
    were already confirmed or rejected are never reused: a later file that such a
    group expected is an ordinary independent import (identified and reviewed alone).
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
        decided = _decided_group_listing(source_location, folder, detected.key, path.name)
        if decided is not None:
            logger.info(
                "multipart_part_after_group_decision",
                import_group_id=str(decided.id),
                import_job_id=str(job.id),
                group_state=decided.state,
            )
            return None
        evidence = _detection_evidence(detected, folder)
        group, created = ImportGroup.objects.get_or_create(
            source_location=source_location,
            folder=str(folder),
            group_key=detected.key,
            state=ImportGroup.State.PENDING,
            defaults={"kind": ImportGroup.Kind.MULTIPART_WORK, "detection": evidence},
        )
        if not created and group.detection != evidence:
            group.detection = evidence
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
    """The job's group if it is still PENDING, read fresh from the database.

    Never trust a group object loaded earlier: a reviewer may have rejected the
    grouping while this part was being hashed.
    """
    if job.import_group_id is None:
        return None
    return ImportGroup.objects.filter(
        pk=job.import_group_id, state=ImportGroup.State.PENDING
    ).first()


def group_jobs(group: ImportGroup) -> list[ImportJob]:
    return list(group.import_jobs.order_by("part_index", "original_filename"))


@dataclass(frozen=True)
class GroupMember:
    """One expected file of a group and where it stands."""

    filename: str
    source_path: str
    part_index: int | None
    part_label: str
    status: str
    job: ImportJob | None = None
    detail: str = ""
    # For MEMBER_DUPLICATE / MEMBER_IN_LIBRARY: the library file with the same bytes.
    identical_variant: FileVariant | None = None

    def as_evidence(self) -> dict[str, Any]:
        evidence: dict[str, Any] = {
            "import_job_id": str(self.job.id) if self.job is not None else None,
            "filename": self.filename,
            "part_index": self.part_index,
            "part_label": self.part_label,
            "state": self.job.state if self.job is not None else None,
            "status": self.status,
            "detail": self.detail,
        }
        if self.identical_variant is not None:
            evidence["identical_file_variant_id"] = self.identical_variant.id
            evidence["identical_book_id"] = self.identical_variant.book_id
            evidence["identical_book_title"] = self.identical_variant.book.title
        return evidence


@dataclass(frozen=True)
class GroupMembership:
    members: tuple[GroupMember, ...]

    def _with(self, *statuses: str) -> list[GroupMember]:
        return [member for member in self.members if member.status in statuses]

    @property
    def pending(self) -> list[GroupMember]:
        return self._with(MEMBER_PENDING)

    @property
    def archived(self) -> list[GroupMember]:
        return self._with(MEMBER_ARCHIVED)

    @property
    def duplicates(self) -> list[GroupMember]:
        """Archived parts whose bytes are already in the library on another Book."""
        return self._with(MEMBER_DUPLICATE)

    @property
    def includable(self) -> list[GroupMember]:
        """Parts a confirmation turns into part variants, in reading order."""
        return self._with(MEMBER_ARCHIVED, MEMBER_DUPLICATE)

    @property
    def absent(self) -> list[GroupMember]:
        """Expected parts that will not be part of a confirmation."""
        return self._with(MEMBER_MISSING, MEMBER_SEPARATE, MEMBER_IN_LIBRARY)

    @property
    def needs_acknowledgement(self) -> list[GroupMember]:
        """Parts a reviewer must explicitly accept before the group can be confirmed."""
        return self._with(MEMBER_MISSING, MEMBER_SEPARATE, MEMBER_IN_LIBRARY, MEMBER_DUPLICATE)

    @property
    def settled(self) -> bool:
        return not self.pending


def _preferred_job(jobs: list[ImportJob]) -> ImportJob | None:
    """A rediscovered file may have an old FAILED job next to its current one."""
    if not jobs:
        return None
    return max(
        jobs,
        key=lambda job: (job.state != ImportJob.State.FAILED, job.created_at, str(job.id)),
    )


def identical_library_variant(job: ImportJob) -> FileVariant | None:
    """A library file with the same bytes as ``job``'s archived copy, if any.

    The job's own archive location does not count. Evaluated on every membership
    check, so a file imported elsewhere after this part was archived is still caught
    before confirmation.
    """
    if not job.sha256:
        return None
    identical = FileVariant.objects.select_related("book").filter(sha256=job.sha256)
    if job.storage_identifier:
        identical = identical.exclude(
            storage_location_id=job.storage_location_id,
            storage_identifier=job.storage_identifier,
        )
    return identical.order_by("created_at", "id").first()


def _identical_detail(variant: FileVariant) -> str:
    return (
        f'byte-identical to "{variant.original_filename}" of the book '
        f'"{variant.book.title}" (book {variant.book_id})'
    )


def _member_status(
    group: ImportGroup, source_path: str, job: ImportJob | None
) -> tuple[str, str, FileVariant | None]:
    if job is not None:
        if job.state == ImportJob.State.LOW_CONFIDENCE:
            identical = identical_library_variant(job)
            if identical is not None:
                return MEMBER_DUPLICATE, _identical_detail(identical), identical
            return MEMBER_ARCHIVED, "", None
        if job.state == ImportJob.State.COMPLETE:
            if group.book_id is not None and job.book_id == group.book_id:
                return MEMBER_ARCHIVED, "", None
            # Attached to another Book, without an archive copy of its own.
            other = job.file_variant
            if other is not None:
                detail = (
                    f"already in the library, {_identical_detail(other)}; "
                    "it will not be a part of this book"
                )
            else:
                detail = "already in the library on another book; it will not be a part"
            return MEMBER_IN_LIBRARY, detail, other
        if job.state == ImportJob.State.FAILED:
            return MEMBER_MISSING, job.last_error or "import failed", None
        if job.state == ImportJob.State.CANCELLED:
            return MEMBER_PENDING, "import cancelled; re-imported when its source resumes", None
        return MEMBER_PENDING, "being processed", None
    outside = ImportJob.objects.filter(
        source_path=source_path, source_location_id=group.source_location_id
    ).exclude(import_group=group)
    if outside.exists():
        return MEMBER_SEPARATE, "imported on its own, outside this group", None
    try:
        present = Path(source_path).is_file()
    except OSError:
        present = False
    if present:
        return MEMBER_PENDING, "not imported yet", None
    return MEMBER_MISSING, "source file no longer present", None


def group_membership(group: ImportGroup) -> GroupMembership:
    """Every expected (detected) member of ``group`` plus any other job attached to it.

    A group is only settled when no member is pending: files the detector saw but
    discovery has not turned into jobs yet (e.g. a byte budget stopped it) count as
    pending until they are imported or disappear.
    """
    by_name: dict[str, list[ImportJob]] = {}
    for attached in group_jobs(group):
        by_name.setdefault(Path(attached.source_path).name, []).append(attached)
    folder = Path(group.folder)
    members: list[GroupMember] = []
    for part in _expected_parts(group):
        filename = part["filename"]
        job = _preferred_job(by_name.pop(filename, []))
        raw_path = part.get("source_path")
        source_path = raw_path if isinstance(raw_path, str) else str(folder / filename)
        if job is not None:
            source_path = job.source_path
        status, detail, identical = _member_status(group, source_path, job)
        raw_index = part.get("index")
        members.append(
            GroupMember(
                filename=filename,
                source_path=source_path,
                part_index=job.part_index
                if job is not None
                else (raw_index if isinstance(raw_index, int) else None),
                part_label=job.part_label if job is not None else str(part.get("label") or ""),
                status=status,
                job=job,
                detail=detail,
                identical_variant=identical,
            )
        )
    for filename, jobs in by_name.items():  # attached, but no longer in the detection
        extra = _preferred_job(jobs)
        assert extra is not None
        status, detail, identical = _member_status(group, extra.source_path, extra)
        members.append(
            GroupMember(
                filename=filename,
                source_path=extra.source_path,
                part_index=extra.part_index,
                part_label=extra.part_label,
                status=status,
                job=extra,
                detail=detail,
                identical_variant=identical,
            )
        )
    members.sort(key=lambda m: (m.part_index is None, m.part_index or 0, m.filename))
    return GroupMembership(tuple(members))


def _names(members: list[GroupMember], limit: int = 5) -> str:
    names = [member.filename for member in members]
    shown = ", ".join(names[:limit])
    return f"{shown}, ..." if len(names) > limit else shown


def _acknowledged_absent(task: ReviewTask) -> set[str]:
    payload = task.decision_payload if isinstance(task.decision_payload, dict) else {}
    ack = payload.get(INCOMPLETE_ACK_KEY)
    names = ack.get("filenames") if isinstance(ack, dict) else None
    return {name for name in names if isinstance(name, str)} if isinstance(names, list) else set()


def _membership_blocker(membership: GroupMembership, task: ReviewTask) -> str | None:
    pending = membership.pending
    if pending:
        return (
            f"{len(pending)} detected part(s) are not archived yet ({_names(pending)}); "
            "try again once they have been imported"
        )
    if not membership.includable:
        return "no archived parts are waiting for review"
    acknowledged = _acknowledged_absent(task)
    absent = [m for m in membership.absent if m.filename not in acknowledged]
    duplicates = [m for m in membership.duplicates if m.filename not in acknowledged]
    reasons = []
    if absent:
        reasons.append(f"{len(absent)} detected part(s) are missing ({_names(absent)})")
    if duplicates:
        reasons.append(
            f"{len(duplicates)} part(s) are byte-identical to files already attached to "
            f"another book ({_names(duplicates)})"
        )
    if reasons:
        return "; ".join(reasons) + "; acknowledge these parts before confirming one book"
    return None


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


def maybe_open_review(group: ImportGroup) -> ReviewTask | None:
    """Open (or refresh) the group's review task once every expected part has settled.

    "Every expected part" is the detected membership, not just the jobs created so
    far: a part discovery has not imported yet keeps the group waiting, and a part
    that failed or disappeared is recorded as missing in the task's evidence.
    Identification runs outside the lock; two workers finishing together may both
    search, but only one task is created.
    """
    from . import identification, intake

    membership = group_membership(group)
    if not membership.settled or not membership.includable:
        return None
    reviewable = [member.job for member in membership.includable if member.job is not None]
    signals = _group_signals(group, reviewable)
    candidates, outcomes = identification.resolve_candidates_with_report(signals)

    with transaction.atomic():
        locked = ImportGroup.objects.select_for_update().get(pk=group.pk)
        if locked.state != ImportGroup.State.PENDING:
            return None
        membership = group_membership(locked)
        if not membership.settled or not membership.includable:
            return None
        reviewable = [member.job for member in membership.includable if member.job is not None]
        lead = reviewable[0]
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
        intake._save_metadata_candidates(
            lead, candidates, signals=signals, outcomes=outcomes, allow_match=False
        )
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
        absent = membership.absent
        duplicates = membership.duplicates
        evidence = {
            "multipart": locked.detection,
            "folder": folder_name,
            "expected_part_count": len(membership.members),
            "parts": [member.as_evidence() for member in membership.members],
            "missing_parts": [member.as_evidence() for member in absent],
            "duplicate_parts": [member.as_evidence() for member in duplicates],
        }
        count = (
            f"{len(reviewable)} of {len(membership.members)} parts"
            if absent
            else f"{len(reviewable)} parts"
        )
        previous_payload = (
            task.decision_payload
            if task is not None and isinstance(task.decision_payload, dict)
            else {}
        )
        values = {
            "inferred_title": intake._fit(ReviewTask, "inferred_title", title),
            "inferred_authors": signals.author_hints,
            "embedded_metadata": evidence,
            "provider_snapshot": snapshot,
            "source_filename": intake._fit(
                ReviewTask, "source_filename", f"{folder_name} ({count})"
            ),
            "source_path_hint": intake._fit(ReviewTask, "source_path_hint", locked.folder),
            "decision_payload": {
                **previous_payload,
                "candidate_count": len(candidates),
                "multipart": True,
                "missing_part_count": len(absent),
                "duplicate_part_count": len(duplicates),
            },
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
        missing_part_count=len(absent),
        duplicate_part_count=len(duplicates),
        candidate_count=len(candidates),
    )
    return task


def task_group(task: ReviewTask) -> ImportGroup | None:
    """The task's group, read fresh (its state may have changed since the task loaded)."""
    if task.task_type != ReviewTask.TaskType.MULTIPART_WORK or task.import_job is None:
        return None
    group_id = task.import_job.import_group_id
    if group_id is None:
        return None
    return ImportGroup.objects.filter(pk=group_id).first()


def confirmation_blocker(task: ReviewTask) -> str | None:
    """Why the parts of a multi-part review cannot be confirmed right now, if anything.

    Checked before a review action creates a Book, so a refused confirmation leaves
    no orphan record behind. Checks the full detected membership: parts still to be
    imported block confirmation, and missing parts need an explicit acknowledgement.
    """
    if task.task_type != ReviewTask.TaskType.MULTIPART_WORK:
        return None
    group = task_group(task)
    if group is None or group.state != ImportGroup.State.PENDING:
        return "multi-part group was already decided"
    fresh = ReviewTask.objects.filter(pk=task.pk).first() or task
    return _membership_blocker(group_membership(group), fresh)


def acknowledge_incomplete(
    *, review_task: ReviewTask, reviewer: Any
) -> tuple[list[str], list[str]]:
    """Record the reviewer's decision on the parts that block a plain confirmation.

    Absent parts (missing, failed, imported elsewhere) are left out of the Book.
    Duplicate parts (byte-identical to a file of another Book) are included as parts
    of this Book, backed by their own archived copy. The acknowledgement names the
    files; a part that becomes absent or duplicate later needs acknowledging again.
    Returns ``(absent, duplicates)`` file names.
    """
    with transaction.atomic():
        task = ReviewTask.objects.select_for_update().get(pk=review_task.pk)
        group = task_group(task)
        if group is None:
            raise MultipartError("review task has no pending multi-part group")
        group = ImportGroup.objects.select_for_update().get(pk=group.pk)
        if group.state != ImportGroup.State.PENDING:
            raise MultipartError("multi-part group was already decided")
        membership = group_membership(group)
        if membership.pending:
            raise MultipartError(
                f"{len(membership.pending)} detected part(s) are not archived yet; "
                "try again once they have been imported"
            )
        absent = sorted(member.filename for member in membership.absent)
        duplicates = sorted(member.filename for member in membership.duplicates)
        if not absent and not duplicates:
            raise MultipartError("no detected parts are missing or duplicated")
        payload = dict(task.decision_payload) if isinstance(task.decision_payload, dict) else {}
        payload[INCOMPLETE_ACK_KEY] = {
            "filenames": sorted(absent + duplicates),
            "missing": absent,
            "duplicates": [member.as_evidence() for member in membership.duplicates],
            "by": getattr(reviewer, "username", None),
            "at": timezone.now().isoformat(),
        }
        task.decision_payload = payload
        task.save(update_fields=["decision_payload", "updated_at"])
    review_task.decision_payload = payload
    logger.info(
        "multipart_incomplete_acknowledged",
        import_group_id=str(group.id),
        review_task_id=task.id,
        missing_part_count=len(absent),
        duplicate_part_count=len(duplicates),
    )
    return absent, duplicates


def update_part_order(task: ReviewTask, order: dict[str, tuple[int, str]]) -> None:
    """Apply reviewer-edited ``{import_job_id: (part_index, part_label)}`` values.

    Positions are validated against the whole group, not just the submitted rows:
    moving one part onto a position another (unsubmitted) part holds is refused.
    """
    group = task_group(task)
    if group is None or group.state != ImportGroup.State.PENDING:
        raise MultipartError("review task has no pending multi-part group")
    with transaction.atomic():
        locked = ImportGroup.objects.select_for_update().get(pk=group.pk)
        if locked.state != ImportGroup.State.PENDING:
            raise MultipartError("multi-part group was already decided")
        jobs = {
            str(member.job.id): member.job
            for member in group_membership(locked).members
            if member.job is not None
        }
        if any(job_id not in jobs for job_id in order):
            raise MultipartError("unknown part")
        final = {
            job_id: order[job_id][0] if job_id in order else job.part_index
            for job_id, job in jobs.items()
        }
        indexes = [index for index in final.values() if index is not None]
        if (
            len(indexes) != len(final)
            or len(set(indexes)) != len(indexes)
            or any(index < 1 for index in indexes)
        ):
            raise MultipartError("part positions must be distinct positive numbers")
        for job_id, (index, label) in order.items():
            job = jobs[job_id]
            job.part_index = index
            job.part_label = label.strip()[:128]
            job.save(update_fields=["part_index", "part_label", "updated_at"])


@dataclass(frozen=True)
class ConfirmedParts:
    variants: list[FileVariant]
    absent: list[str]  # acknowledged and left out
    duplicates: list[dict[str, Any]]  # acknowledged and included; names the identical file


def _part_variant(job: ImportJob, book: Book, identical: FileVariant | None) -> FileVariant:
    """The part FileVariant of ``book`` for one archived group member.

    Never reuses another Book's variant (Book != FileVariant): a part identical to a
    file of another Book gets its own variant on ``book``, backed by the part's own
    verified archive copy, with the identical variant recorded in
    ``converter_options``. Only a variant with the same bytes already on ``book``
    itself (merging the group into the Book that holds that file) is reused, so one
    Book never holds two copies of the same bytes.
    """
    from . import intake

    same_book = (
        FileVariant.objects.filter(book=book, sha256=job.sha256)
        .order_by("created_at", "id")
        .first()
    )
    if same_book is not None:
        if same_book.part_index is None and not same_book.part_label:
            same_book.part_index = job.part_index
            same_book.part_label = job.part_label
            same_book.save(update_fields=["part_index", "part_label", "updated_at"])
        intake.discard_redundant_archive_copy(job, keep=same_book)
        return same_book
    provenance = (
        {
            IDENTICAL_TO_KEY: {
                "file_variant_id": identical.id,
                "book_id": identical.book_id,
                "import_job_id": str(job.id),
            }
        }
        if identical is not None
        else None
    )
    return intake._create_or_reuse_file_variant(
        job,
        book,
        part_index=job.part_index,
        part_label=job.part_label,
        reuse_identical=False,
        converter_options=provenance,
    )


def confirm_parts(
    *,
    lead_job: ImportJob,
    review_task: ReviewTask,
    book: Book,
    inline_cover_fallback: bool = True,
) -> ConfirmedParts:
    """Resolve every archived part of the task's group as an ordered part of ``book``.

    Each archived part (acknowledged duplicate parts included) becomes its own
    FileVariant of ``book`` carrying ``part_index`` and ``part_label``; nothing is
    merged, rewritten or deleted, and no other Book's variant is touched. Returns the
    variants plus the acknowledged absent and duplicate parts.
    """
    if lead_job.import_group_id is None:
        raise MultipartError("review task has no pending multi-part group")
    from . import intake

    variants: list[FileVariant] = []
    duplicates: list[dict[str, Any]] = []
    first_part: tuple[FileVariant, ImportJob] | None = None
    with transaction.atomic():
        group = ImportGroup.objects.select_for_update().get(pk=lead_job.import_group_id)
        if group.state != ImportGroup.State.PENDING:
            raise MultipartError("multi-part group was already decided")
        membership = group_membership(group)
        fresh_task = ReviewTask.objects.filter(pk=review_task.pk).first() or review_task
        blocker = _membership_blocker(membership, fresh_task)
        if blocker is not None:
            raise MultipartError(blocker)
        for member in membership.includable:
            job = member.job
            assert job is not None
            # Already archived and verified: this only records the variant row.
            variant = _part_variant(job, book, member.identical_variant)
            if member.status == MEMBER_DUPLICATE:
                duplicates.append({**member.as_evidence(), "file_variant_id": variant.id})
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
        absent = sorted(member.filename for member in membership.absent)
        group.state = ImportGroup.State.CONFIRMED
        group.book = book
        group.decided_at = timezone.now()
        group.save(update_fields=["state", "book", "decided_at", "updated_at"])
    if first_part is not None:
        intake.schedule_front_cover_extraction(*first_part, inline_fallback=inline_cover_fallback)
    logger.info(
        "multipart_group_confirmed",
        import_group_id=str(group.id),
        review_task_id=review_task.id,
        book_id=book.id,
        part_count=len(variants),
        missing_part_count=len(absent),
        duplicate_part_count=len(duplicates),
    )
    return ConfirmedParts(variants=variants, absent=absent, duplicates=duplicates)


def reject_grouping(*, review_task: ReviewTask, reviewer: Any, notes: str = "") -> list[ImportJob]:
    """The files are not parts of one work: identify each as an independent import.

    Archived parts are identified now. A part still being processed finds the group
    REJECTED when it settles (``settle_part``) and is identified on its own then.
    """
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
        in_flight_count=sum(1 for job in jobs if job.state not in SETTLED_PART_STATES),
    )
    return split


def _has_open_individual_review(job_id: Any) -> bool:
    return (
        ReviewTask.objects.filter(
            import_job_id=job_id,
            state__in=(ReviewTask.State.OPEN, ReviewTask.State.CLAIMED),
        )
        .exclude(task_type=ReviewTask.TaskType.MULTIPART_WORK)
        .exists()
    )


def settle_part(job: ImportJob) -> None:
    """Exit hook for every terminal path of a grouped part's intake job.

    Re-reads the group under its row lock. While the group is PENDING, opens or
    refreshes its review once every expected part has settled. Once the group was
    decided, an archived part that is still waiting (it was in flight when the
    reviewer rejected the grouping) is identified as an independent import, so it
    never ends up LOW_CONFIDENCE without a review.
    """
    from ebook_server.jobs import enqueue

    if job.import_group_id is None:
        return
    with transaction.atomic():
        group = ImportGroup.objects.select_for_update().filter(pk=job.import_group_id).first()
        if group is None:
            return
        row = ImportJob.objects.filter(pk=job.pk).values_list("state", "book_id").first()
    if group.state == ImportGroup.State.PENDING:
        maybe_open_review(group)
        return
    if row is None:
        return
    state, book_id = row
    if (
        state == ImportJob.State.LOW_CONFIDENCE
        and book_id is None
        and not _has_open_individual_review(job.pk)
    ):
        logger.info(
            "multipart_part_settled_after_decision",
            import_group_id=str(group.id),
            import_job_id=str(job.id),
            group_state=group.state,
        )
        enqueue(identify_split_part, str(job.id))


def _resolve_split_part_as_duplicate(job_id: str) -> bool:
    """A split-off part identical to a library file resolves like any intake duplicate.

    Outside the group the ordinary exact-duplicate rule applies: the job points at the
    existing variant (whose Book is not changed) and the part's own, now redundant,
    archive copy is discarded. Returns True when the job needs no identification.
    """
    from . import intake

    with transaction.atomic():
        job = ImportJob.objects.select_for_update().get(pk=job_id)
        if job.state != ImportJob.State.LOW_CONFIDENCE or job.book_id is not None:
            return True
        identical = identical_library_variant(job)
        if identical is None:
            return False
        intake.discard_redundant_archive_copy(job, keep=identical)
        job.book = identical.book
        job.file_variant = identical
        job.storage_location = identical.storage_location
        job.storage_identifier = identical.storage_identifier
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
    logger.info(
        "multipart_split_part_duplicate",
        import_job_id=str(job_id),
        file_variant_id=identical.id,
        book_id=identical.book_id,
    )
    return True


def identify_split_part(job_id: str) -> None:
    """Background job: identify one archived file of a decided group on its own.

    Same decision rules as ``process_import_job``: auto-accept only a confident,
    author-verified candidate, otherwise open an ordinary review task. The file is
    already archived and verified, so nothing touches the source again. Idempotent:
    rejection and the part's own settlement may both enqueue it.
    """
    from . import identification, intake

    job = ImportJob.objects.select_related("uploader", "import_group").get(pk=job_id)
    if job.state != ImportJob.State.LOW_CONFIDENCE or job.book_id is not None:
        return
    if pending_group_for(job) is not None:
        return
    if _has_open_individual_review(job.pk):
        return
    if _resolve_split_part_as_duplicate(job_id):
        return
    metadata = dict(job.evidence) if isinstance(job.evidence, dict) else {}
    signals = identification.build_identification_input(
        source_path=job.source_path,
        original_filename=job.original_filename,
        extracted_metadata=metadata,
    )
    candidates, outcomes = identification.resolve_candidates_with_report(signals)
    file_authors = identification.evidence_author_names(metadata)
    decision = intake.decide_auto_accept(candidates, file_authors)
    with transaction.atomic():
        job = ImportJob.objects.select_for_update().get(pk=job_id)
        if (
            job.state != ImportJob.State.LOW_CONFIDENCE
            or job.book_id is not None
            or _has_open_individual_review(job.pk)
        ):
            return
        if decision.blocked_reason is not None:
            metadata[intake.AUTO_ACCEPT_BLOCKED_KEY] = decision.blocked_reason
            metadata[intake.AUTO_ACCEPT_BLOCK_DETAILS_KEY] = decision.details
            metadata[intake.SUGGESTED_AUTHORS_KEY] = file_authors
            job.evidence = metadata
            job.save(update_fields=["evidence", "updated_at"])
        intake._save_metadata_candidates(
            job,
            candidates,
            signals=signals,
            outcomes=outcomes,
            allow_match=decision.blocked_reason is None,
        )
        if decision.candidate is None:
            intake._create_review_task(job=job, candidates=candidates)
            return
        book = intake._provision_book_from_metadata(
            candidate=decision.candidate,
            fallback_title=decision.candidate.title,
            extra_metadata=metadata,
        )
        variant = intake._create_or_reuse_file_variant(job, book)
        job.book = book
        job.file_variant = variant
        job.state = ImportJob.State.COMPLETE
        job.save(update_fields=["state", "book_id", "file_variant_id", "updated_at"])
    intake._try_extract_front_cover(variant, job)


def group_for_task(task: ReviewTask) -> dict[str, Any] | None:
    """Review-page context: the group's parts in their current order, and what is absent."""
    group = task_group(task)
    if group is None:
        return None
    detection = group.detection if isinstance(group.detection, dict) else {}
    membership = group_membership(group)
    absent = membership.absent
    flagged = membership.needs_acknowledgement
    acknowledged = _acknowledged_absent(task)
    return {
        "id": str(group.id),
        "state": group.state,
        "folder": group.folder,
        "missing_numbers": detection.get("missing_numbers", []),
        "expected_count": len(membership.members),
        "parts": [
            {
                "import_job_id": str(member.job.id),
                "filename": member.filename,
                "part_index": member.part_index,
                "part_label": member.part_label,
                "state": member.job.state,
                "status": member.status,
                "size_bytes": member.job.size_bytes,
            }
            for member in membership.members
            if member.job is not None
        ],
        "pending_parts": [member.as_evidence() for member in membership.pending],
        "missing_parts": [member.as_evidence() for member in absent],
        "duplicate_parts": [member.as_evidence() for member in membership.duplicates],
        "incomplete_acknowledged": bool(flagged)
        and not ({member.filename for member in flagged} - acknowledged),
        "confirmation_blocker": (
            confirmation_blocker(task) if group.state == ImportGroup.State.PENDING else None
        ),
    }
