"""The read-only recorder for the physical scanner acceptance run (#169)."""

from __future__ import annotations

import functools
import hashlib
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from scripts import record_scanner_site_run as site_run

JOB_ID = "20261125T101500Z"
REFINISH = UUID(int=7)
PAGE_ID = "5f0c4f3e-1111-4c4c-8888-000000000001"
FIRST, REPLACEMENT = "a" * 64, "b" * 64
PDF = b"%PDF-1.7 re-finished"
KEY = "device-key-never-printed"


def _site() -> dict[str, Any]:
    return {
        "scan": {
            "id": JOB_ID,
            "state": "complete",
            "book_id": 12,
            "pages": [
                {
                    "id": PAGE_ID,
                    "logical_sequence_number": 0,
                    "active_version": {
                        "version": 2,
                        "checksum_sha256": REPLACEMENT,
                        "created_at": "2026-11-25T10:30:00+00:00",
                    },
                    "available_versions": [
                        {"version": 2, "checksum_sha256": REPLACEMENT},
                        {"version": 1, "checksum_sha256": FIRST},
                    ],
                },
                {
                    "id": "5f0c4f3e-1111-4c4c-8888-000000000002",
                    "logical_sequence_number": 1,
                    "active_version": {"version": 1, "checksum_sha256": "e" * 64},
                    "available_versions": [{"version": 1, "checksum_sha256": "e" * 64}],
                },
            ],
        },
        "todos": [
            {
                "id": 3,
                "todo_type": "rescan",
                "scan_page_id": PAGE_ID,
                "state": "completed",
                "claimed_by_id": 8,
            }
        ],
        "finish": {
            "state": "success",
            "scan_job_id": JOB_ID,
            "cleaned_variant_id": 40,
            "started_at": "2026-11-25T10:31:00+00:00",
        },
        "book": {
            "variants": [
                {"id": 40, "format": "pdf", "source_type": "cleaned", "size_bytes": len(PDF)}
            ]
        },
        "device": {
            "job_id": JOB_ID,
            "state": "complete",
            "events": {"sans_last_line_no": 9},
            "pages": [{"sequence_number": 0, "sha256": REPLACEMENT, "version": 2}],
            "covers": [{"position": "front", "sha256": "c" * 64, "version": 1}],
            "metadata_opf": {"sha256": "d" * 64, "version": 1},
            "open_rescans": 0,
        },
    }


def _transport(site: dict[str, Any]) -> httpx.MockTransport:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.startswith("/api/scanner/v1/"):
            if request.headers.get("Authorization") != "Bearer " + KEY:
                return httpx.Response(401)
            return httpx.Response(200, json=site["device"])
        if "sessionid=member" not in request.headers.get("Cookie", ""):
            return httpx.Response(403)
        if path == f"/scan/jobs/{JOB_ID}/":
            return httpx.Response(200, json=site["scan"])
        if path == "/scan/todos/":
            assert request.url.params["job_id"] == JOB_ID
            return httpx.Response(200, json={"items": site["todos"]})
        if path == f"/scan-finalizations/{REFINISH}/":
            return httpx.Response(200, json=site["finish"])
        if path == "/library/books/12/":
            return httpx.Response(200, json=site["book"])
        if path == "/library/books/12/download/":
            assert request.url.params["variant_id"] == "40"
            return httpx.Response(200, content=PDF)
        return httpx.Response(404)

    return httpx.MockTransport(handle)


def _member(site: dict[str, Any]) -> dict[str, Any]:
    with httpx.Client(
        base_url="https://stage.example",
        cookies={"sessionid": "member"},
        transport=_transport(site),
    ) as client:
        return site_run.check_member_view(client, JOB_ID, 0, REFINISH)


def test_member_view_records_identity_both_checksums_and_the_download() -> None:
    record = _member(_site())

    assert record["page"] == {
        "page_id": PAGE_ID,
        "sequence_number": 0,
        "versions": [{"version": 1, "sha256": FIRST}, {"version": 2, "sha256": REPLACEMENT}],
    }
    assert record["rescan_todo_ids"] == [3]
    assert record["download"] == {
        "variant_id": 40,
        "bytes": len(PDF),
        "sha256": hashlib.sha256(PDF).hexdigest(),
    }


def _broken(change: str) -> dict[str, Any]:
    site = _site()
    page = site["scan"]["pages"][0]
    if change == "one version":
        page["available_versions"] = page["available_versions"][:1]
    elif change == "old version active":
        page["active_version"]["version"] = 1
    elif change == "same checksum":
        page["available_versions"][1]["checksum_sha256"] = REPLACEMENT
    elif change == "open todo":
        site["todos"].append(
            {"id": 4, "todo_type": "rescan", "scan_page_id": PAGE_ID, "state": "claimed"}
        )
    elif change == "no rescan":
        site["todos"] = []
    elif change == "finished before the replacement":
        site["finish"]["started_at"] = "2026-11-25T10:00:00+00:00"
    elif change == "job not complete":
        site["scan"]["state"] = "captured"
    elif change == "old PDF":
        site["book"]["variants"][0]["id"] = 39
    elif change == "short download":
        site["book"]["variants"][0]["size_bytes"] = len(PDF) + 1
    elif change == "never claimed":
        site["todos"][0]["claimed_by_id"] = None
    elif change == "retry added a version":
        other = site["scan"]["pages"][1]
        other["available_versions"].append({"version": 2, "checksum_sha256": "e" * 64})
    return site


@pytest.mark.parametrize(
    "change",
    [
        "one version",
        "old version active",
        "same checksum",
        "open todo",
        "no rescan",
        "finished before the replacement",
        "job not complete",
        "old PDF",
        "short download",
        "retry added a version",
        "never claimed",
    ],
)
def test_member_view_fails_an_unmet_check(change: str) -> None:
    with pytest.raises(site_run.RunFailed):
        _member(_broken(change))


@pytest.mark.parametrize(
    "response",
    [
        {"verified": False, "state": "captured", "portal_url": f"https://s/j/{JOB_ID}"},
        {"state": "captured", "portal_url": f"https://s/j/{JOB_ID}"},
        {"verified": True, "state": "captured", "portal_url": "https://s/j/20261125T101500Z-2"},
    ],
)
def test_only_a_verified_completion_for_this_job_is_accepted(response: dict[str, Any]) -> None:
    with pytest.raises(site_run.RunFailed):
        site_run.check_completion(response, JOB_ID)


@pytest.mark.parametrize(
    ("field", "value"),
    [("note", "re-finish needed"), ("open_rescans", 1), ("pages", [])],
)
def test_device_view_fails_while_the_scanner_would_still_act(field: str, value: Any) -> None:
    site = _site()
    site["device"][field] = value
    headers = {"Authorization": "Bearer " + KEY}
    with httpx.Client(
        base_url="https://stage.example", headers=headers, transport=_transport(site)
    ) as client:
        with pytest.raises(site_run.RunFailed):
            site_run.check_device_view(client, JOB_ID, 0, REPLACEMENT)


def _args(tmp_path: Path, completion: dict[str, Any]) -> list[str]:
    response = tmp_path / "completion.json"
    response.write_text(json.dumps(completion), encoding="utf-8")
    key = tmp_path / "key"
    key.write_text(KEY + "\n", encoding="utf-8")
    return [
        "--job-id", JOB_ID, "--sequence", "0", "--refinish", str(REFINISH),
        "--completion-response", str(response), "--scanner-firmware", "sans 0.9.1",
        "--server-commit", "d2afd0e", "--scanner-key-file", str(key),
    ]  # fmt: skip


def test_main_prints_a_record_without_the_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    site = _site()
    monkeypatch.setattr(
        httpx, "Client", functools.partial(httpx.Client, transport=_transport(site))
    )
    monkeypatch.setenv("SCANNER_SITE_RUN_ORIGIN", "https://stage.example/")
    monkeypatch.setenv("SCANNER_SITE_RUN_SESSION", "member")
    completion = {"verified": True, "state": "captured", "portal_url": f"https://s/j/{JOB_ID}"}

    assert site_run.main(_args(tmp_path, completion)) == 0
    out = capsys.readouterr().out
    assert KEY not in out
    record = json.loads(out)
    assert record["status"] == "passed"
    assert record["server_commit"] == "d2afd0e"
    assert record["device"]["covers"] == ["front"]

    site["device"]["note"] = "re-finish needed"
    assert site_run.main(_args(tmp_path, completion)) == 1
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert site_run.main(_args(tmp_path, {**completion, "verified": False})) == 1


def test_main_is_unavailable_without_a_staging_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("SCANNER_SITE_RUN_ORIGIN", raising=False)
    monkeypatch.delenv("SCANNER_SITE_RUN_SESSION", raising=False)
    assert site_run.main(_args(tmp_path, {})) == 77
    assert json.loads(capsys.readouterr().out)["status"] == "unavailable"


def test_main_refuses_plain_http_and_web_job_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("SCANNER_SITE_RUN_ORIGIN", "http://stage.example/")
    monkeypatch.setenv("SCANNER_SITE_RUN_SESSION", "member")
    completion = {"verified": True, "state": "captured", "portal_url": f"https://s/j/{JOB_ID}"}
    assert site_run.main(_args(tmp_path, completion)) == 1
    args = _args(tmp_path, completion)
    args[1] = "6a1f0b4e-2a39-4d8e-9a51-1b7d3c0f9e22"
    monkeypatch.setenv("SCANNER_SITE_RUN_ORIGIN", "https://stage.example/")
    assert site_run.main(args) == 1
    capsys.readouterr()
