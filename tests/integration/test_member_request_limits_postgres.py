"""Cross-worker member limits using independent PostgreSQL connections."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from django.db import connection, connections
from django.test import override_settings

from ebook_server.core import request_limits
from ebook_server.core.models import UploadReservation, get_or_create_profile

pytestmark = pytest.mark.infrastructure


@pytest.mark.django_db(transaction=True)
def test_postgres_workers_compete_for_one_upload_slot() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("row-lock concurrency requires PostgreSQL")
    profile = get_or_create_profile("two-workers")
    barrier = Barrier(2)

    def reserve() -> bool:
        try:
            barrier.wait(timeout=10)
            request_limits.reserve_upload(profile, 10)
            return True
        except request_limits.RequestLimited:
            return False
        finally:
            connections.close_all()

    with override_settings(BOOK_SCANNER_MEMBER_UPLOADS_PENDING=1), ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(reserve) for _ in range(2)]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
    assert UploadReservation.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_postgres_workers_share_inflight_login_budget() -> None:
    if connection.vendor != "postgresql":
        pytest.skip("row-lock concurrency requires PostgreSQL")
    barrier = Barrier(2)

    def reserve(address: str) -> bool:
        try:
            barrier.wait(timeout=10)
            request_limits.reserve_login("reader", address)
            return True
        except request_limits.RequestLimited:
            return False
        finally:
            connections.close_all()

    with override_settings(BOOK_SCANNER_LOGIN_ACCOUNT_BURST=1), ThreadPoolExecutor(2) as pool:
        futures = [pool.submit(reserve, address) for address in ("192.0.2.10", "192.0.2.11")]
        assert sorted(future.result(timeout=10) for future in futures) == [False, True]
