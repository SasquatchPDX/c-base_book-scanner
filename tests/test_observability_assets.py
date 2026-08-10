from __future__ import annotations

import json
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_prometheus_assets_have_complete_alert_metadata_and_safe_auth() -> None:
    alerts = yaml.safe_load((PROJECT_ROOT / "deploy/prometheus/alerts.yml").read_text())
    scrape = yaml.safe_load((PROJECT_ROOT / "deploy/prometheus/scrape.yml").read_text())

    rules = [rule for group in alerts["groups"] for rule in group["rules"]]
    assert len(rules) >= 8
    assert len({rule["alert"] for rule in rules}) == len(rules)
    for rule in rules:
        assert rule["expr"]
        assert rule["for"]
        assert rule["labels"]["severity"] in {"warning", "critical"}
        assert rule["annotations"]["summary"]
        assert rule["annotations"]["description"]
        assert rule["annotations"]["runbook_url"].startswith(
            "https://github.com/SasquatchPDX/book_scanner/",
        )

    target = scrape["scrape_configs"][0]
    assert target["job_name"] == "book-scanner"
    assert target["scheme"] == "https"
    assert target["authorization"]["credentials_file"]
    assert "credentials" not in target["authorization"]
    assert scrape["rule_files"]


def test_grafana_dashboard_tracks_every_major_workflow() -> None:
    dashboard = json.loads(
        (PROJECT_ROOT / "deploy/grafana/dashboards/book-scanner-dashboard.json").read_text(),
    )
    expressions = "\n".join(
        target["expr"] for panel in dashboard["panels"] for target in panel.get("targets", [])
    )

    assert dashboard["uid"] == "book-scanner-ops"
    assert dashboard["refresh"] == "30s"
    assert len({panel["id"] for panel in dashboard["panels"]}) == len(dashboard["panels"])
    for metric in {
        "ebook_server_queue",
        "ebook_server_processing_jobs",
        "ebook_server_processing_job_oldest_age_seconds",
        "ebook_server_import_jobs",
        "ebook_server_review_tasks",
        "ebook_server_scan_jobs",
        "ebook_server_scantailor_stages",
        "ebook_server_metadata_confidence_bucket_population",
    }:
        assert metric in expressions
