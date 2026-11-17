"""Contribution recognition: ledger emitters, settlement, scorecards (#27).

Design: docs/design/27-contribution-recognition.md; operator notes: docs/contributions.md.

Rules this module enforces:

- Points only for *verified outcomes*: a resolved review and a scan job finished into
  the library. Uploads, claims, time spent, conversions and downloads earn nothing,
  and nothing here is called from those paths.
- Emitters only append a PENDING ``ContributionEvent`` with a snapshot of the outcome.
  They never compute points, so no request does point arithmetic.
- The settlement job (``settle``) compares each event's snapshot with the source rows.
  A reversed outcome voids a PENDING event, or adds a negative CLAWBACK for a SETTLED
  one. An outcome that still holds after the hold period settles; only then are points
  computed, from the weight version recorded when the event happened.
- Everything is a no-op while ``BOOK_SCANNER_CONTRIBUTIONS_ENABLED`` is false.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import structlog
from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.db import IntegrityError, transaction
from django.db.models import Count, Max, Q, Sum
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import (
    AppUserProfile,
    ContributionEvent,
    ContributionKind,
    ContributionMonthlyTotal,
    ContributionWeight,
    ContributorPreference,
    FileVariant,
    ReviewDecisionCorrection,
    ReviewTask,
    ScanJob,
    ScanPageVersion,
)

logger = structlog.get_logger(__name__)

REVIEW_KINDS = frozenset({ContributionKind.REVIEW_RESOLVED, ContributionKind.REVIEW_UNIDENTIFIED})
OUTCOME_KINDS = frozenset({*REVIEW_KINDS, ContributionKind.SCAN_COMPLETED})
# Decisions that identify a book; MARK_UNIDENTIFIED is scored separately and the
# search/edit decisions are not outcomes at all.
_IDENTIFYING_DECISIONS = frozenset(
    {
        ReviewTask.Decision.ACCEPT_CANDIDATE,
        ReviewTask.Decision.MANUAL_ENTRY,
        ReviewTask.Decision.MERGED_BOOK,
    },
)
SOFT_CAP_MULTIPLIER = Decimal("0.5")
COLLECTIVE_STATS_CACHE_KEY = "contributions:collective-stats:v1"
COLLECTIVE_STATS_CACHE_SECONDS = 300
_CENT = Decimal("0.01")


# --- Configuration ---------------------------------------------------------------------


def enabled() -> bool:
    return bool(getattr(settings, "BOOK_SCANNER_CONTRIBUTIONS_ENABLED", False))


def leaderboard_enabled() -> bool:
    return enabled() and bool(getattr(settings, "BOOK_SCANNER_LEADERBOARD_ENABLED", False))


def hold_period() -> timedelta:
    return timedelta(
        days=max(0, int(getattr(settings, "BOOK_SCANNER_CONTRIBUTIONS_HOLD_DAYS", 14)))
    )


def daily_soft_cap() -> Decimal:
    return Decimal(str(getattr(settings, "BOOK_SCANNER_CONTRIBUTIONS_DAILY_SOFT_CAP", 50)))


def retention_months() -> int:
    return max(0, int(getattr(settings, "BOOK_SCANNER_CONTRIBUTIONS_RETENTION_MONTHS", 24)))


def season_kind() -> str:
    value = str(getattr(settings, "BOOK_SCANNER_CONTRIBUTIONS_SEASON", "month"))
    return value if value in {"month", "quarter"} else "month"


@dataclass(frozen=True)
class WeightSpec:
    base_points: Decimal
    per_unit_points: Decimal = Decimal("0")
    cap: Decimal | None = None
    own_upload_multiplier: Decimal = Decimal("1")


# Starting points from the design doc; admins override them with versioned rows.
DEFAULT_WEIGHTS: dict[str, WeightSpec] = {
    ContributionKind.REVIEW_RESOLVED: WeightSpec(
        Decimal("3"), own_upload_multiplier=Decimal("0.5")
    ),
    ContributionKind.REVIEW_UNIDENTIFIED: WeightSpec(
        Decimal("1"),
        own_upload_multiplier=Decimal("0.5"),
    ),
    ContributionKind.SCAN_COMPLETED: WeightSpec(
        Decimal("5"),
        per_unit_points=Decimal("0.05"),
        cap=Decimal("25"),
    ),
}


def current_weight_version() -> int:
    return int(ContributionWeight.objects.aggregate(v=Max("version"))["v"] or 0)


def weights_for_version(version: int) -> dict[str, WeightSpec]:
    weights = dict(DEFAULT_WEIGHTS)
    if version <= 0:
        return weights
    for row in ContributionWeight.objects.filter(version=version):
        weights[row.kind] = WeightSpec(
            base_points=row.base_points,
            per_unit_points=row.per_unit_points,
            cap=row.cap,
            own_upload_multiplier=row.own_upload_multiplier,
        )
    return weights


def save_weights(specs: dict[str, WeightSpec], *, actor: User | None) -> int:
    """Store ``specs`` as a new weight version; existing events keep their version."""
    with transaction.atomic():
        version = current_weight_version() + 1
        merged = {**weights_for_version(version - 1), **specs}
        for kind, spec in merged.items():
            ContributionWeight.objects.create(
                version=version,
                kind=kind,
                base_points=spec.base_points,
                per_unit_points=spec.per_unit_points,
                cap=spec.cap,
                own_upload_multiplier=spec.own_upload_multiplier,
                created_by=actor,
            )
    logger.info("contribution_weights_saved", version=version)
    return version


# --- Emitters (called next to existing state transitions) ------------------------------


def _create_event(**fields: Any) -> ContributionEvent | None:
    """Insert one PENDING event; an existing (user, kind, outcome_key) row wins."""
    lookup = {key: fields.pop(key) for key in ("user_id", "kind", "outcome_key")}
    try:
        with transaction.atomic():
            event, _created = ContributionEvent.objects.get_or_create(**lookup, defaults=fields)
    except IntegrityError:  # a concurrent emitter inserted the same outcome
        return ContributionEvent.objects.filter(**lookup).first()
    return event


def _review_owner_id(task: ReviewTask) -> int | None:
    if task.import_job_id is not None:
        import_job = task.import_job
        return import_job.uploader_id if import_job is not None else task.uploader_id
    if task.scan_job_id is not None:
        scan_job = task.scan_job
        return scan_job.operator_id if scan_job is not None else None
    return task.uploader_id


def record_review_resolution(task: ReviewTask) -> ContributionEvent | None:
    """Emit a pending event for a review a member just resolved.

    Automatic resolutions (no reviewer) earn nothing. Never raises: recognition must
    not break the review workflow.
    """
    if not enabled():
        return None
    try:
        if task.state != ReviewTask.State.RESOLVED or task.reviewer_id is None:
            return None
        if task.decided_at is None:
            return None
        if task.decision == ReviewTask.Decision.MARK_UNIDENTIFIED:
            kind = ContributionKind.REVIEW_UNIDENTIFIED
            book_id = None
        elif task.decision in _IDENTIFYING_DECISIONS:
            kind = ContributionKind.REVIEW_RESOLVED
            book_id = task.resolved_book_id
        else:
            return None
        decided_at = task.decided_at
        return _create_event(
            user_id=task.reviewer_id,
            kind=kind,
            outcome_key=f"review:{task.pk}:{decided_at.isoformat()}",
            outcome={
                "decision": str(task.decision),
                "decided_at": decided_at.isoformat(),
                "book_id": book_id,
            },
            own_upload=_review_owner_id(task) == task.reviewer_id,
            weight_version=current_weight_version(),
            review_task_id=task.pk,
            book_id=book_id,
            occurred_at=decided_at,
            settle_after=decided_at + hold_period(),
        )
    except Exception:
        logger.exception("contribution_review_emit_failed", review_task_id=task.pk)
        return None


def record_review_reversal(
    task: ReviewTask,
    *,
    source: str,
    found_by: User | None = None,
    note: str = "",
) -> ReviewDecisionCorrection | None:
    """Record that a RESOLVED review's decision is being undone.

    Call it *before* the code that reopens or re-decides the task. Tasks that are not
    resolved have nothing to reverse. Settlement voids or claws back the points.
    """
    if not enabled():
        return None
    try:
        if task.state != ReviewTask.State.RESOLVED:
            return None
        with transaction.atomic():  # savepoint: a failure must not poison the caller's
            return ReviewDecisionCorrection.objects.create(
                review_task=task,
                found_by=found_by,
                source=source,
                note=note[:2000],
            )
    except Exception:
        logger.exception("contribution_reversal_record_failed", review_task_id=task.pk)
        return None


def record_book_deleted(book_id: int) -> int:
    """Record corrections for every resolved review that chose ``book_id``."""
    if not enabled():
        return 0
    count = 0
    for task in ReviewTask.objects.filter(
        resolved_book_id=book_id,
        state=ReviewTask.State.RESOLVED,
    ):
        if record_review_reversal(
            task,
            source=ReviewDecisionCorrection.Source.BOOK_DELETED,
            note=f"book {book_id} deleted",
        ):
            count += 1
    return count


def _active_page_owners(scan_job: ScanJob) -> dict[int, int]:
    """Active (final) page count per member; pages without an uploader go to the operator."""
    owners: dict[int, int] = defaultdict(int)
    rows = (
        ScanPageVersion.objects.filter(scan_page__scan_job=scan_job, is_active=True)
        .values("uploader_id")
        .annotate(n=Count("id"))
    )
    for row in rows:
        user_id = row["uploader_id"] or scan_job.operator_id
        if user_id is not None:
            owners[int(user_id)] += int(row["n"])
    return owners


def record_scan_completion(scan_job: ScanJob) -> list[ContributionEvent]:
    """Emit pending events for each member whose pages are in the finished job."""
    if not enabled():
        return []
    try:
        if scan_job.state != ScanJob.State.COMPLETE or scan_job.finished_at is None:
            return []
        user_ids = set(_active_page_owners(scan_job))
        if not user_ids and scan_job.operator_id is not None:
            user_ids = {scan_job.operator_id}
        finished_at = scan_job.finished_at
        version = current_weight_version()
        events: list[ContributionEvent] = []
        for user_id in sorted(user_ids):
            event = _create_event(
                user_id=user_id,
                kind=ContributionKind.SCAN_COMPLETED,
                outcome_key=f"scan:{scan_job.pk}:{finished_at.isoformat()}",
                outcome={"finished_at": finished_at.isoformat(), "book_id": scan_job.book_id},
                weight_version=version,
                scan_job_id=scan_job.pk,
                book_id=scan_job.book_id,
                occurred_at=finished_at,
                settle_after=finished_at + hold_period(),
            )
            if event is not None:
                events.append(event)
        return events
    except Exception:
        logger.exception("contribution_scan_emit_failed", scan_job_id=str(scan_job.pk))
        return []


# --- Outcome verification --------------------------------------------------------------


def _same_instant(raw: object, value: datetime | None) -> bool:
    if value is None or not isinstance(raw, str):
        return False
    parsed = parse_datetime(raw)
    return parsed is not None and parsed == value


def _review_outcome_holds(event: ContributionEvent) -> tuple[bool, str]:
    task = event.review_task
    if task is None:
        return False, "review task deleted"
    correction = (
        ReviewDecisionCorrection.objects.filter(
            review_task=task,
            created_at__gt=event.occurred_at,
        )
        .order_by("created_at")
        .first()
    )
    if correction is not None:
        return False, f"decision corrected: {correction.get_source_display()}"
    snapshot = event.outcome if isinstance(event.outcome, dict) else {}
    if task.state != ReviewTask.State.RESOLVED:
        return False, "review reopened"
    if task.reviewer_id != event.user_id or not _same_instant(
        snapshot.get("decided_at"),
        task.decided_at,
    ):
        return False, "review decided again"
    if task.decision != snapshot.get("decision"):
        return False, "decision changed"
    book_id = snapshot.get("book_id")
    if book_id is not None and task.resolved_book_id != book_id:
        return False, "resolved book deleted or changed"
    return True, ""


def _scan_has_library_variant(scan_job: ScanJob) -> bool:
    return FileVariant.objects.filter(
        book_id=scan_job.book_id,
        source_type=FileVariant.SourceType.CLEANED,
        processing_state=FileVariant.ProcessingState.COMPLETE,
        converter_options__scan_job_id=str(scan_job.pk),
    ).exists()


def _scan_outcome_holds(event: ContributionEvent) -> tuple[bool, str]:
    scan_job = event.scan_job
    if scan_job is None:
        return False, "scan job deleted"
    snapshot = event.outcome if isinstance(event.outcome, dict) else {}
    if scan_job.state != ScanJob.State.COMPLETE or not _same_instant(
        snapshot.get("finished_at"),
        scan_job.finished_at,
    ):
        return False, "scan job no longer complete"
    if scan_job.book_id is None or scan_job.book_id != snapshot.get("book_id"):
        return False, "book deleted or changed"
    if not _scan_has_library_variant(scan_job):
        return False, "no library variant from this scan"
    return True, ""


def outcome_holds(event: ContributionEvent) -> tuple[bool, str]:
    if event.kind in REVIEW_KINDS:
        return _review_outcome_holds(event)
    if event.kind == ContributionKind.SCAN_COMPLETED:
        return _scan_outcome_holds(event)
    return True, ""


# --- Point computation (settlement only) -----------------------------------------------


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _day_bounds(moment: datetime) -> tuple[datetime, datetime]:
    local = timezone.localtime(moment)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start, start + timedelta(days=1)


def _settled_review_points_on_day(user_id: int, moment: datetime) -> Decimal:
    start, end = _day_bounds(moment)
    total = ContributionEvent.objects.filter(
        user_id=user_id,
        kind__in=REVIEW_KINDS,
        state=ContributionEvent.State.SETTLED,
        occurred_at__gte=start,
        occurred_at__lt=end,
    ).aggregate(total=Sum("points"))["total"]
    return Decimal(total or 0)


def compute_points(event: ContributionEvent) -> tuple[Decimal, list[str]]:
    """Points for a verified outcome, with the notes explaining each multiplier."""
    spec = weights_for_version(event.weight_version).get(event.kind)
    if spec is None:
        return Decimal("0"), ["no weight for this kind"]
    notes: list[str] = []
    if event.kind in REVIEW_KINDS:
        points = spec.base_points
        if event.own_upload and spec.own_upload_multiplier != 1:
            points *= spec.own_upload_multiplier
            notes.append(f"own upload x{spec.own_upload_multiplier}")
        cap = daily_soft_cap()
        if cap > 0 and _settled_review_points_on_day(event.user_id, event.occurred_at) >= cap:
            points *= SOFT_CAP_MULTIPLIER
            notes.append(f"daily soft cap x{SOFT_CAP_MULTIPLIER}")
        return _quantize(points), notes
    if event.kind == ContributionKind.SCAN_COMPLETED and event.scan_job is not None:
        owners = _active_page_owners(event.scan_job)
        total_pages = sum(owners.values())
        job_points = spec.base_points + spec.per_unit_points * total_pages
        if spec.cap is not None and job_points > spec.cap:
            job_points = spec.cap
            notes.append(f"capped at {spec.cap}")
        if total_pages == 0:
            share = Decimal("1") if event.user_id == event.scan_job.operator_id else Decimal("0")
        else:
            share = Decimal(owners.get(event.user_id, 0)) / Decimal(total_pages)
            notes.append(f"{owners.get(event.user_id, 0)}/{total_pages} final pages")
        return _quantize(job_points * share), notes
    return Decimal("0"), []


# --- Settlement --------------------------------------------------------------------------


@dataclass
class SettlementReport:
    skipped: bool = False
    settled: int = 0
    voided: int = 0
    clawed_back: int = 0
    pruned: int = 0
    still_pending: int = 0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _void(event: ContributionEvent, reason: str, *, now: datetime, actor: User | None) -> None:
    event.state = ContributionEvent.State.VOIDED
    event.reason = reason[:255]
    event.voided_at = now
    if actor is not None:
        event.actor = actor
    event.save(update_fields=["state", "reason", "voided_at", "actor"])


def _clawback(
    event: ContributionEvent,
    reason: str,
    *,
    now: datetime,
    actor: User | None,
) -> ContributionEvent | None:
    if event.state != ContributionEvent.State.SETTLED or event.points is None:
        return None
    try:
        with transaction.atomic():
            clawback, created = ContributionEvent.objects.get_or_create(
                user_id=event.user_id,
                kind=ContributionKind.CLAWBACK,
                outcome_key=f"clawback:{event.pk}",
                defaults={
                    "state": ContributionEvent.State.SETTLED,
                    "points": -event.points,
                    "weight_version": event.weight_version,
                    "reverses": event,
                    "reason": reason[:255],
                    "actor": actor,
                    "review_task_id": event.review_task_id,
                    "scan_job_id": event.scan_job_id,
                    "book_id": event.book_id,
                    "occurred_at": now,
                    "settle_after": now,
                    "settled_at": now,
                },
            )
    except IntegrityError:
        return None
    return clawback if created else None


def settle(*, now: datetime | None = None) -> SettlementReport:
    """Void, settle and claw back events; prune detail past retention. Idempotent."""
    report = SettlementReport()
    if not enabled():
        report.skipped = True
        return report
    now = now or timezone.now()

    pending_ids = list(
        ContributionEvent.objects.filter(state=ContributionEvent.State.PENDING)
        .order_by("occurred_at", "created_at")
        .values_list("pk", flat=True),
    )
    for event_id in pending_ids:
        try:
            with transaction.atomic():
                event = (
                    ContributionEvent.objects.select_for_update()
                    .select_related("review_task", "scan_job")
                    .get(pk=event_id)
                )
                if event.state != ContributionEvent.State.PENDING:
                    continue
                holds, why = outcome_holds(event)
                if not holds:
                    _void(event, why, now=now, actor=None)
                    report.voided += 1
                elif event.settle_after <= now:
                    points, notes = compute_points(event)
                    event.points = points
                    event.state = ContributionEvent.State.SETTLED
                    event.settled_at = now
                    event.reason = "; ".join(notes)[:255]
                    event.save(update_fields=["points", "state", "settled_at", "reason"])
                    report.settled += 1
                else:
                    report.still_pending += 1
        except Exception as exc:  # one bad row must not stop the run
            logger.exception("contribution_settle_failed", event_id=str(event_id))
            report.errors.append(f"{event_id}: {exc}")

    clawed = ContributionEvent.objects.filter(
        kind=ContributionKind.CLAWBACK,
        reverses__isnull=False,
    ).values("reverses_id")
    settled_outcomes = (
        ContributionEvent.objects.filter(
            state=ContributionEvent.State.SETTLED,
            kind__in=OUTCOME_KINDS,
        )
        .exclude(pk__in=clawed)
        .select_related("review_task", "scan_job")
        .order_by("occurred_at")
    )
    for event in settled_outcomes:
        try:
            holds, why = outcome_holds(event)
            if not holds and _clawback(event, why, now=now, actor=None) is not None:
                report.clawed_back += 1
        except Exception as exc:
            logger.exception("contribution_clawback_failed", event_id=str(event.pk))
            report.errors.append(f"{event.pk}: {exc}")

    report.pruned = prune_expired(now=now)
    cache.delete(COLLECTIVE_STATS_CACHE_KEY)
    logger.info("contribution_settlement_finished", **report.as_dict())
    return report


def settle_job() -> dict[str, Any]:
    """RQ entry point for the periodic settlement run."""
    return settle().as_dict()


# --- Retention ---------------------------------------------------------------------------


def _subtract_months(moment: datetime, months: int) -> datetime:
    index = moment.year * 12 + (moment.month - 1) - months
    year, month = divmod(index, 12)
    month += 1
    day = min(moment.day, 28)
    return moment.replace(year=year, month=month, day=day)


def prune_expired(*, now: datetime | None = None) -> int:
    """Fold finished events older than the retention period into monthly totals."""
    months = retention_months()
    if months <= 0:
        return 0
    now = now or timezone.now()
    cutoff = _subtract_months(now, months)
    expired = ContributionEvent.objects.filter(
        occurred_at__lt=cutoff,
        state__in=[ContributionEvent.State.SETTLED, ContributionEvent.State.VOIDED],
    )
    with transaction.atomic():
        totals: dict[tuple[int, date, str], list[Decimal]] = defaultdict(
            lambda: [Decimal("0"), Decimal("0")],
        )
        for user_id, kind, points, settled_at in expired.filter(
            state=ContributionEvent.State.SETTLED,
        ).values_list("user_id", "kind", "points", "settled_at"):
            moment = timezone.localtime(settled_at or now)
            key = (user_id, moment.date().replace(day=1), kind)
            totals[key][0] += Decimal(points or 0)
            totals[key][1] += 1
        for (user_id, month, kind), (points, count) in totals.items():
            row, _ = ContributionMonthlyTotal.objects.select_for_update().get_or_create(
                user_id=user_id,
                month=month,
                kind=kind,
            )
            row.points += points
            row.event_count += int(count)
            row.save(update_fields=["points", "event_count"])
        deleted, _ = expired.delete()
    return deleted


# --- Admin operations --------------------------------------------------------------------


class ContributionAdminError(ValueError):
    pass


def admin_adjust(*, user: User, points: Decimal, reason: str, actor: User) -> ContributionEvent:
    reason = reason.strip()
    if not reason:
        raise ContributionAdminError("a reason is required")
    now = timezone.now()
    event = ContributionEvent.objects.create(
        user=user,
        kind=ContributionKind.ADMIN_ADJUSTMENT,
        state=ContributionEvent.State.SETTLED,
        points=_quantize(points),
        outcome_key=f"adjustment:{uuid.uuid4()}",
        reason=reason[:255],
        actor=actor,
        occurred_at=now,
        settle_after=now,
        settled_at=now,
    )
    logger.info("contribution_admin_adjustment", event_id=str(event.pk), user_id=user.pk)
    return event


def admin_void(*, event: ContributionEvent, reason: str, actor: User) -> ContributionEvent:
    """Void a pending event, or claw back a settled one. Never a silent edit."""
    reason = reason.strip()
    if not reason:
        raise ContributionAdminError("a reason is required")
    now = timezone.now()
    with transaction.atomic():
        locked = ContributionEvent.objects.select_for_update().get(pk=event.pk)
        if locked.kind == ContributionKind.CLAWBACK:
            raise ContributionAdminError("a clawback cannot be voided; adjust instead")
        if locked.state == ContributionEvent.State.PENDING:
            _void(locked, f"admin: {reason}", now=now, actor=actor)
            return locked
        if locked.state == ContributionEvent.State.SETTLED:
            clawback = _clawback(locked, f"admin: {reason}", now=now, actor=actor)
            if clawback is None:
                raise ContributionAdminError("already clawed back")
            return clawback
    raise ContributionAdminError("event is already voided")


def admin_flag_review_wrong(
    *, task: ReviewTask, note: str, actor: User
) -> ReviewDecisionCorrection:
    if task.state != ReviewTask.State.RESOLVED:
        raise ContributionAdminError("only resolved reviews can be flagged")
    return ReviewDecisionCorrection.objects.create(
        review_task=task,
        found_by=actor,
        source=ReviewDecisionCorrection.Source.ADMIN,
        note=note[:2000],
    )


def set_leaderboard_exclusion(*, user: User, excluded: bool, reason: str = "") -> None:
    preference, _ = ContributorPreference.objects.get_or_create(user=user)
    preference.excluded_from_leaderboard = excluded
    preference.excluded_reason = reason[:255] if excluded else ""
    preference.save(update_fields=["excluded_from_leaderboard", "excluded_reason", "updated_at"])


# --- Leaderboard preference ------------------------------------------------------------

HANDLE_MIN_LENGTH = 2
HANDLE_MAX_LENGTH = 40


class PreferenceError(ValueError):
    pass


def _validate_handle(handle: str) -> str:
    handle = " ".join(handle.split())
    if not (HANDLE_MIN_LENGTH <= len(handle) <= HANDLE_MAX_LENGTH):
        raise PreferenceError(
            f"the display handle must be {HANDLE_MIN_LENGTH}-{HANDLE_MAX_LENGTH} characters",
        )
    if not all(ch.isalnum() or ch in " ._-" for ch in handle):
        raise PreferenceError("use letters, digits, spaces, '.', '_' or '-' only")
    return handle


def set_leaderboard_opt_in(*, user: User, opt_in: bool, handle: str = "") -> ContributorPreference:
    """Join (with a unique handle) or leave the leaderboard; leaving is immediate."""
    preference, _ = ContributorPreference.objects.get_or_create(user=user)
    if not opt_in:
        preference.leaderboard_opt_in = False
        preference.opted_in_at = None
        preference.save(update_fields=["leaderboard_opt_in", "opted_in_at", "updated_at"])
        return preference
    clean = _validate_handle(handle)
    clash = (
        ContributorPreference.objects.filter(
            leaderboard_opt_in=True,
            display_handle__iexact=clean,
        )
        .exclude(user=user)
        .exists()
    )
    if clash:
        raise PreferenceError("that display handle is taken")
    preference.leaderboard_opt_in = True
    preference.display_handle = clean
    if preference.opted_in_at is None:
        preference.opted_in_at = timezone.now()
    try:
        with transaction.atomic():
            preference.save()
    except IntegrityError as exc:
        raise PreferenceError("that display handle is taken") from exc
    return preference


# --- Read models -------------------------------------------------------------------------


def season_start(now: datetime | None = None) -> datetime:
    local = timezone.localtime(now or timezone.now())
    month = local.month
    if season_kind() == "quarter":
        month = 3 * ((month - 1) // 3) + 1
    return local.replace(month=month, day=1, hour=0, minute=0, second=0, microsecond=0)


def _accuracy(user_id: int) -> dict[str, Any]:
    """Share of the member's finished review outcomes that held up."""
    finished = ContributionEvent.objects.filter(
        user_id=user_id,
        kind__in=REVIEW_KINDS,
        state__in=[ContributionEvent.State.SETTLED, ContributionEvent.State.VOIDED],
    )
    total = finished.count()
    clawed = ContributionEvent.objects.filter(
        user_id=user_id,
        kind=ContributionKind.CLAWBACK,
        reverses__kind__in=REVIEW_KINDS,
    ).count()
    voided = finished.filter(state=ContributionEvent.State.VOIDED).count()
    held = total - voided - clawed
    return {
        "decided": total,
        "held_up": max(held, 0),
        "percent": round(100 * max(held, 0) / total) if total else None,
    }


def scorecard(user: User, *, now: datetime | None = None) -> dict[str, Any]:
    events = ContributionEvent.objects.filter(user=user)
    settled = events.filter(state=ContributionEvent.State.SETTLED)
    archived = ContributionMonthlyTotal.objects.filter(user=user).aggregate(p=Sum("points"))["p"]
    all_time = Decimal(settled.aggregate(p=Sum("points"))["p"] or 0) + Decimal(archived or 0)
    start = season_start(now)
    season_points = settled.filter(settled_at__gte=start).aggregate(p=Sum("points"))["p"]
    pending = {
        row["kind"]: row["n"]
        for row in events.filter(state=ContributionEvent.State.PENDING)
        .values("kind")
        .annotate(n=Count("pk"))
    }
    preference = ContributorPreference.objects.filter(user=user).first()
    return {
        "all_time_points": _quantize(all_time),
        "season_points": _quantize(Decimal(season_points or 0)),
        "season_start": start,
        "pending_counts": pending,
        "pending_total": sum(pending.values()),
        "accuracy": _accuracy(user.pk),
        "recent": list(
            events.select_related("review_task", "scan_job").order_by("-occurred_at")[:50],
        ),
        "preference": preference,
    }


def open_work_for(profile: AppUserProfile) -> int:
    """Open review tasks this member is allowed to resolve (what they could pick up)."""
    tasks = ReviewTask.objects.filter(
        state__in=[ReviewTask.State.OPEN, ReviewTask.State.CLAIMED],
    )
    if not profile.is_admin():
        tasks = tasks.filter(
            Q(import_job__uploader=profile.user) | Q(scan_job__operator=profile.user),
        )
    return tasks.count()


def collective_stats(*, now: datetime | None = None) -> dict[str, Any]:
    """Anonymous totals only: no names, no per-member numbers. Cached briefly."""
    if now is None:
        cached = cache.get(COLLECTIVE_STATS_CACHE_KEY)
        if cached is not None:
            return dict(cached)
    moment = timezone.localtime(now or timezone.now())
    month_start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    week_start = (moment - timedelta(days=moment.weekday())).replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0,
    )
    clawed = ContributionEvent.objects.filter(
        kind=ContributionKind.CLAWBACK,
        reverses__isnull=False,
    ).values("reverses_id")
    standing = ContributionEvent.objects.exclude(state=ContributionEvent.State.VOIDED).exclude(
        pk__in=clawed,
    )
    stats = {
        "reviews_this_month": standing.filter(
            kind__in=REVIEW_KINDS,
            occurred_at__gte=month_start,
        )
        .values("review_task_id")
        .distinct()
        .count(),
        "books_scanned_this_week": standing.filter(
            kind=ContributionKind.SCAN_COMPLETED,
            occurred_at__gte=week_start,
        )
        .values("scan_job_id")
        .distinct()
        .count(),
        "books_scanned_this_month": standing.filter(
            kind=ContributionKind.SCAN_COMPLETED,
            occurred_at__gte=month_start,
        )
        .values("scan_job_id")
        .distinct()
        .count(),
        "open_reviews": ReviewTask.objects.filter(
            state__in=[ReviewTask.State.OPEN, ReviewTask.State.CLAIMED],
        ).count(),
        "month_start": month_start.date().isoformat(),
        "week_start": week_start.date().isoformat(),
    }
    if now is None:
        cache.set(COLLECTIVE_STATS_CACHE_KEY, stats, COLLECTIVE_STATS_CACHE_SECONDS)
    return stats


def leaderboard(*, period: str = "season", now: datetime | None = None) -> list[dict[str, Any]]:
    """Opted-in, non-excluded, enabled members only, ranked by settled points."""
    visible = ContributorPreference.objects.filter(
        leaderboard_opt_in=True,
        excluded_from_leaderboard=False,
        user__is_active=True,
    ).exclude(display_handle="")
    visible = visible.exclude(user__library_profile__is_disabled=True)
    handles = dict(visible.values_list("user_id", "display_handle"))
    if not handles:
        return []
    settled = ContributionEvent.objects.filter(
        user_id__in=handles,
        state=ContributionEvent.State.SETTLED,
    )
    if period == "season":
        settled = settled.filter(settled_at__gte=season_start(now))
    totals: dict[int, Decimal] = defaultdict(Decimal)
    for row in settled.values("user_id").annotate(p=Sum("points")):
        totals[row["user_id"]] += Decimal(row["p"] or 0)
    if period != "season":
        for row in (
            ContributionMonthlyTotal.objects.filter(user_id__in=handles)
            .values("user_id")
            .annotate(p=Sum("points"))
        ):
            totals[row["user_id"]] += Decimal(row["p"] or 0)
    ranked = sorted(
        ((user_id, points) for user_id, points in totals.items() if points > 0),
        key=lambda item: (-item[1], handles[item[0]].casefold()),
    )
    return [
        {
            "rank": index,
            "handle": handles[user_id],
            "points": _quantize(points),
            "accuracy_percent": _accuracy(user_id)["percent"],
        }
        for index, (user_id, points) in enumerate(ranked, start=1)
    ]


# --- GDPR: export and erase ----------------------------------------------------------------


def _event_export(event: ContributionEvent) -> dict[str, Any]:
    return {
        "id": str(event.pk),
        "kind": event.kind,
        "state": event.state,
        "points": str(event.points) if event.points is not None else None,
        "own_upload": event.own_upload,
        "outcome": event.outcome,
        "review_task_id": event.review_task_id,
        "scan_job_id": str(event.scan_job_id) if event.scan_job_id else None,
        "book_id": event.book_id,
        "reverses": str(event.reverses_id) if event.reverses_id else None,
        "reason": event.reason,
        "occurred_at": event.occurred_at.isoformat(),
        "settle_after": event.settle_after.isoformat(),
        "settled_at": event.settled_at.isoformat() if event.settled_at else None,
        "voided_at": event.voided_at.isoformat() if event.voided_at else None,
    }


def export_user_data(user: User) -> dict[str, Any]:
    preference = ContributorPreference.objects.filter(user=user).first()
    return {
        "user": user.get_username(),
        "exported_at": timezone.now().isoformat(),
        "purpose": "Recognition of library contributions; never used to evaluate members.",
        "events": [
            _event_export(event)
            for event in ContributionEvent.objects.filter(user=user).order_by("occurred_at")
        ],
        "monthly_totals": [
            {
                "month": row.month.isoformat(),
                "kind": row.kind,
                "points": str(row.points),
                "event_count": row.event_count,
            }
            for row in ContributionMonthlyTotal.objects.filter(user=user).order_by("month")
        ],
        "preference": (
            {
                "leaderboard_opt_in": preference.leaderboard_opt_in,
                "display_handle": preference.display_handle,
                "opted_in_at": (
                    preference.opted_in_at.isoformat() if preference.opted_in_at else None
                ),
                "excluded_from_leaderboard": preference.excluded_from_leaderboard,
            }
            if preference is not None
            else None
        ),
        "corrections_reported": [
            {
                "review_task_id": row.review_task_id,
                "source": row.source,
                "created_at": row.created_at.isoformat(),
            }
            for row in ReviewDecisionCorrection.objects.filter(found_by=user)
        ],
    }


def erase_user_data(user: User) -> dict[str, int]:
    """Erase the member's contribution history (GDPR Art. 17).

    The library records themselves (reviews, scans) are untouched; only the
    recognition data is removed. An admin exclusion is kept so that erasing cannot be
    used to get back onto a board an admin removed the member from.
    """
    with transaction.atomic():
        ContributionEvent.objects.filter(actor=user).exclude(user=user).update(actor=None)
        ContributionEvent.objects.filter(reverses__user=user).exclude(user=user).update(
            reverses=None,
        )
        events, _ = ContributionEvent.objects.filter(user=user).delete()
        totals, _ = ContributionMonthlyTotal.objects.filter(user=user).delete()
        corrections = ReviewDecisionCorrection.objects.filter(found_by=user).update(found_by=None)
        preference = ContributorPreference.objects.filter(user=user).first()
        preferences = 0
        if preference is not None:
            if preference.excluded_from_leaderboard:
                preference.leaderboard_opt_in = False
                preference.display_handle = ""
                preference.opted_in_at = None
                preference.save()
            else:
                preference.delete()
                preferences = 1
    logger.info("contribution_data_erased", user_id=user.pk)
    return {
        "events": events,
        "monthly_totals": totals,
        "preferences": preferences,
        "corrections_unlinked": corrections,
    }


def iter_kinds() -> Iterable[tuple[str, str]]:
    return [(kind, label) for kind, label in ContributionKind.choices if kind in DEFAULT_WEIGHTS]
