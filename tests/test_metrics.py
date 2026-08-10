from __future__ import annotations

from datetime import timedelta

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from ebook_server.core.models import (
    ImportJob,
    MetadataCandidate,
    ProcessingJob,
    get_or_create_profile,
)


@pytest.mark.django_db(transaction=True)
@override_settings(
    BOOK_SCANNER_METRICS_ENABLED=True,
    BOOK_SCANNER_METRICS_BEARER_TOKEN="metrics-secret",
    BOOK_SCANNER_QUEUE_URL="inline://",
)
def test_metrics_require_bearer_token_and_expose_persistent_job_state() -> None:
    profile = get_or_create_profile("metrics-uploader")
    import_job = ImportJob.objects.create(
        source_type=ImportJob.SourceType.UPLOAD,
        state=ImportJob.State.LOW_CONFIDENCE,
        uploader=profile.user,
        source_path="/staging/metrics.epub",
        original_filename="metrics.epub",
        normalized_filename="metrics.epub",
        detected_format="epub",
        mime_type="application/epub+zip",
    )
    MetadataCandidate.objects.create(
        import_job=import_job,
        provider_id="metrics-provider",
        provider_ref="candidate-1",
        title="Metrics Candidate",
        score=0.72,
    )
    ImportJob.objects.create(
        source_type=ImportJob.SourceType.NETWORK_SOURCE,
        state=ImportJob.State.FAILED,
        uploader=profile.user,
        source_path="/staging/failed.epub",
        original_filename="failed.epub",
        normalized_filename="failed.epub",
        detected_format="epub",
        mime_type="application/epub+zip",
    )
    started_at = timezone.now() - timedelta(seconds=12)
    ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.CONVERSION,
        state=ProcessingJob.JobState.SUCCESS,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=8),
    )
    ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.OCR,
        state=ProcessingJob.JobState.QUEUED,
        scheduled_at=timezone.now() - timedelta(minutes=12),
    )
    ProcessingJob.objects.create(
        job_type=ProcessingJob.JobType.OCR,
        state=ProcessingJob.JobState.FAILED,
        started_at=started_at,
        finished_at=timezone.now() - timedelta(minutes=2),
    )
    client = Client()

    missing = client.get("/metrics/")
    wrong = client.get("/metrics/", HTTP_AUTHORIZATION="Bearer wrong")
    response = client.get(
        "/metrics/",
        HTTP_AUTHORIZATION="Bearer metrics-secret",
    )

    assert missing.status_code == 403
    assert missing["WWW-Authenticate"] == "Bearer"
    assert wrong.status_code == 403
    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/plain")
    body = response.content.decode()
    assert 'ebook_server_processing_jobs{job_type="conversion",state="success"} 1.0' in body
    assert 'ebook_server_import_jobs{state="low_confidence"} 1.0' in body
    assert (
        'ebook_server_metadata_confidence_bucket_population{le="0.86",provider="metrics-provider"} '
        "1.0"
    ) in body
    assert 'ebook_server_queue{measure="up",queue="ebook-server"} 1.0' in body
    assert 'ebook_server_processing_job_oldest_age_seconds{job_type="ocr",state="queued"} ' in body
    assert 'ebook_server_processing_job_failures_recent{job_type="ocr",window="1h"} 1.0' in body
    assert (
        'ebook_server_import_failures_recent{source_type="network_source",window="1h"} 1.0' in body
    )
    assert "# HELP ebook_server_scantailor_failures_recent" in body


@override_settings(BOOK_SCANNER_METRICS_ENABLED=False)
def test_metrics_can_be_disabled() -> None:
    assert Client().get("/metrics/").status_code == 404
