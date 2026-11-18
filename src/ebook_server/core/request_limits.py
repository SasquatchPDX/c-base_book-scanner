"""Shared admission limits for LDAP binds and incoming member uploads."""

from __future__ import annotations

import math
from datetime import datetime, timedelta

import structlog
from django.conf import settings
from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.utils import timezone
from django.utils.crypto import salted_hmac

from .models import (
    AppUserProfile,
    ImportJob,
    LoginAttemptWindow,
    RequestLimitCount,
    UploadReservation,
)

logger = structlog.get_logger(__name__)
LOGIN_WINDOW = timedelta(minutes=15)
UPLOAD_LEASE = timedelta(minutes=15)
UPLOAD_WINDOW = timedelta(hours=1)
LoginTicket = list[tuple[str, datetime, int]]


class RequestLimited(Exception):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__("Please try again later.")


def record_rejection(kind: str) -> None:
    RequestLimitCount.objects.get_or_create(kind=kind)
    RequestLimitCount.objects.filter(kind=kind).update(total=F("total") + 1)
    logger.info("member_request_throttled", kind=kind)


def _enforce_backoff(windows: list[LoginAttemptWindow], now: datetime) -> None:
    delay = max(
        ((row.blocked_until - now).total_seconds() for row in windows if row.blocked_until),
        default=0,
    )
    if delay > 0:
        raise RequestLimited(math.ceil(delay))


def reserve_login(username: str, address: str) -> LoginTicket:
    limits = {
        salted_hmac(
            "login-account", username.strip().casefold(), algorithm="sha256"
        ).hexdigest(): settings.BOOK_SCANNER_LOGIN_ACCOUNT_BURST,
        salted_hmac(
            "login-address", address, algorithm="sha256"
        ).hexdigest(): settings.BOOK_SCANNER_LOGIN_ADDRESS_BURST,
    }
    now = timezone.now()
    # A blocked address submitting many different names needs no new database rows.
    _enforce_backoff(list(LoginAttemptWindow.objects.filter(pk__in=limits)), now)
    # Retire a bounded batch. Recheck the expiry when deleting in case a concurrent
    # request has already started a new window for that digest.
    expired = list(
        LoginAttemptWindow.objects.filter(expires_at__lte=now)
        .order_by("pk")
        .values_list("pk", flat=True)[:32]
    )
    if expired:
        LoginAttemptWindow.objects.filter(pk__in=expired, expires_at__lte=now).delete()
    ticket: LoginTicket = []
    with transaction.atomic():
        rows = [
            LoginAttemptWindow.objects.select_for_update().get_or_create(
                pk=key, defaults={"expires_at": now + LOGIN_WINDOW}
            )[0]
            for key in sorted(limits)
        ]
        _enforce_backoff(rows, now)
        for row in rows:
            if row.expires_at <= now:
                row.attempts, row.expires_at, row.blocked_until = 0, now + LOGIN_WINDOW, None
            row.attempts += 1
            burst = limits[row.key]
            if row.attempts >= burst:
                seconds = min(300, 2 ** min(row.attempts - burst, 9))
                row.blocked_until = min(row.expires_at, now + timedelta(seconds=seconds))
            row.save(update_fields=["attempts", "expires_at", "blocked_until"])
            ticket.append((row.key, row.expires_at, burst))
    return ticket


@transaction.atomic
def release_successful_login(ticket: LoginTicket) -> None:
    # A successful bind refunds its own reservation, including behind a shared proxy.
    for key, expiry, burst in ticket:
        row = (
            LoginAttemptWindow.objects.select_for_update().filter(pk=key, expires_at=expiry).first()
        )
        if row and row.attempts:
            row.attempts -= 1
            if row.attempts < burst:
                row.blocked_until = None
            row.save(update_fields=["attempts", "blocked_until"])


@transaction.atomic
def reserve_upload(profile: AppUserProfile, size: int) -> UploadReservation:
    AppUserProfile.objects.select_for_update().get(pk=profile.pk)
    now = timezone.now()
    reservations = UploadReservation.objects.filter(member_id=profile.user_id)
    reservations.filter(expires_at__lte=now).delete()
    held = reservations.aggregate(count=Count("pk"), size=Sum("size_bytes", default=0))
    jobs = ImportJob.objects.filter(
        uploader_id=profile.user_id, source_type=ImportJob.SourceType.UPLOAD
    )
    pending = ~Q(
        state__in=[
            ImportJob.State.COMPLETE,
            ImportJob.State.LOW_CONFIDENCE,
            ImportJob.State.FAILED,
            ImportJob.State.CANCELLED,
        ]
    ) | Q(state=ImportJob.State.FAILED, next_retry_at__isnull=False)
    recent = Q(created_at__gt=now - UPLOAD_WINDOW)
    counts = jobs.filter(pending | recent).aggregate(
        pending=Count("pk", filter=pending),
        recent=Count("pk", filter=recent),
        size=Sum("size_bytes", filter=recent, default=0),
    )
    if held["count"] + counts["pending"] >= settings.BOOK_SCANNER_MEMBER_UPLOADS_PENDING:
        raise RequestLimited(30)
    if (
        held["count"] + counts["recent"] >= settings.BOOK_SCANNER_MEMBER_UPLOADS_PER_HOUR
        or held["size"] + counts["size"] + size > settings.BOOK_SCANNER_MEMBER_UPLOAD_BYTES_PER_HOUR
    ):
        raise RequestLimited(3600)
    return reservations.create(
        member_id=profile.user_id, size_bytes=size, expires_at=now + UPLOAD_LEASE
    )


def finish_reservation(reservation: UploadReservation, actual_size: int) -> None:
    """Called in the same transaction as job insertion. A late writer must reacquire admission."""
    # Serialize this handoff with the two aggregate queries in reserve_upload.
    AppUserProfile.objects.select_for_update().get(user_id=reservation.member_id)
    if actual_size != reservation.size_bytes:
        raise ValueError("Upload size changed during staging.")
    removed, _ = UploadReservation.objects.filter(
        pk=reservation.pk, expires_at__gt=timezone.now()
    ).delete()
    if not removed:
        raise RequestLimited(1)
