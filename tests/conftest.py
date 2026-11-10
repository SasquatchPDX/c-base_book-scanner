from __future__ import annotations

from collections.abc import Iterator

import pytest
from django.conf import settings as django_settings


@pytest.fixture(autouse=True)
def _relaxed_storage_root_and_source_age() -> Iterator[None]:
    """Tests point APP_DATA_DIR at not-yet-created temp dirs and freshly written sources.

    Production guards (existing storage root, minimum source age) are exercised explicitly
    in tests that opt back in.
    """
    original = (
        getattr(django_settings, "STORAGE_REQUIRE_EXISTING_ROOT", False),
        getattr(django_settings, "STORAGE_SENTINEL_FILE", ""),
        getattr(django_settings, "BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS", 60),
    )
    django_settings.STORAGE_REQUIRE_EXISTING_ROOT = False
    django_settings.STORAGE_SENTINEL_FILE = ""
    django_settings.BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS = 0
    yield
    (
        django_settings.STORAGE_REQUIRE_EXISTING_ROOT,
        django_settings.STORAGE_SENTINEL_FILE,
        django_settings.BOOK_SCANNER_SOURCE_MIN_AGE_SECONDS,
    ) = original
