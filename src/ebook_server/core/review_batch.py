"""Rules for bulk review work: queue paging and batch acceptance of top candidates.

Batch acceptance is a human decision applied to many tasks at once, so it is
deliberately conservative: a task is only eligible when its best candidate meets a
configurable minimum confidence *and* is clearly ahead of the runner-up. Anything
else stays in the queue for individual review. These rules never change the
intake auto-accept threshold.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from django.conf import settings

DEFAULT_QUEUE_PAGE_SIZE: Final = 50
MAX_QUEUE_PAGE_SIZE: Final = 200
DEFAULT_BATCH_MIN_SCORE: Final = 0.75
DEFAULT_BATCH_AMBIGUITY_MARGIN: Final = 0.05
DEFAULT_BATCH_MAX_TASKS: Final = 100

ELIGIBLE: Final = "eligible"
NO_CANDIDATES: Final = "no_candidates"
BELOW_MIN_SCORE: Final = "below_min_score"
AMBIGUOUS: Final = "ambiguous"


def _float_setting(name: str, default: float) -> float:
    value = getattr(settings, name, None)
    try:
        return default if value is None else float(value)
    except (TypeError, ValueError):
        return default


def _int_setting(name: str, default: int) -> int:
    value = getattr(settings, name, None)
    try:
        return default if value is None else int(value)
    except (TypeError, ValueError):
        return default


def queue_page_size() -> int:
    size = _int_setting("BOOK_SCANNER_REVIEW_QUEUE_PAGE_SIZE", DEFAULT_QUEUE_PAGE_SIZE)
    return min(max(size, 1), MAX_QUEUE_PAGE_SIZE)


def batch_min_score() -> float:
    """Lowest top-candidate score a batch accept may resolve (0..1)."""
    score = _float_setting("BOOK_SCANNER_REVIEW_BATCH_MIN_SCORE", DEFAULT_BATCH_MIN_SCORE)
    return min(max(score, 0.0), 1.0)


def batch_ambiguity_margin() -> float:
    """Runner-ups within this distance of the top score make a task ambiguous."""
    margin = _float_setting(
        "BOOK_SCANNER_REVIEW_BATCH_AMBIGUITY_MARGIN",
        DEFAULT_BATCH_AMBIGUITY_MARGIN,
    )
    return max(margin, 0.0)


def batch_max_tasks() -> int:
    return max(_int_setting("BOOK_SCANNER_REVIEW_BATCH_MAX_TASKS", DEFAULT_BATCH_MAX_TASKS), 1)


def effective_min_score(requested: float | None) -> float:
    """A request may only make the batch threshold stricter, never looser."""
    configured = batch_min_score()
    if requested is None:
        return configured
    return min(max(configured, requested), 1.0)


@dataclass(frozen=True)
class BatchEligibility:
    reason: str
    top_score: float | None = None
    runner_up_score: float | None = None

    @property
    def eligible(self) -> bool:
        return self.reason == ELIGIBLE


def evaluate_top_candidates(
    scores: Sequence[Decimal | float],
    *,
    min_score: float,
    ambiguity_margin: float,
) -> BatchEligibility:
    """Decide whether the best of ``scores`` may be accepted without individual review."""
    ordered = sorted((float(score) for score in scores), reverse=True)
    if not ordered:
        return BatchEligibility(NO_CANDIDATES)
    top = ordered[0]
    runner_up = ordered[1] if len(ordered) > 1 else None
    if top < min_score:
        return BatchEligibility(BELOW_MIN_SCORE, top, runner_up)
    # Round to the stored precision (2 decimals) so float noise cannot hide a tie.
    if runner_up is not None and round(top - runner_up, 6) <= round(ambiguity_margin, 6):
        return BatchEligibility(AMBIGUOUS, top, runner_up)
    return BatchEligibility(ELIGIBLE, top, runner_up)
