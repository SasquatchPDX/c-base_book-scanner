"""Check and record the physical-scanner acceptance run of #169 on a staging deployment.

The run itself happens on the scanner and in the portal (``docs/operations.md``,
"Recording the physical scanner acceptance run"). This tool only reads: it checks
what serif holds for the finished, rescanned job, downloads the member-visible
re-finished PDF and prints one JSON record for ``docs/measurements/``.

Exit status: 0 passed, 1 failed, 77 unavailable (no origin or session, or no
connection).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx

DEVICE_JOB_ID = re.compile(r"[0-9]{8}T[0-9]{6}Z(?:-[1-9][0-9]{0,3})?")
SHA256 = re.compile(r"[0-9a-f]{64}")


class RunFailed(ValueError):
    """The site run does not meet an acceptance check."""


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise RunFailed(message)


def _timestamp(value: Any) -> datetime:
    _check(isinstance(value, str) and bool(value), "A timestamp is missing.")
    return datetime.fromisoformat(str(value))


def check_completion(response: dict[str, Any], job_id: str) -> dict[str, Any]:
    """The capture-complete body the scanner received before it deleted its copy."""
    _check(response.get("verified") is True, "The completion response is not verified.")
    _check(response.get("state") == "captured", "The completion did not report a raw scan.")
    _check(
        str(response.get("portal_url", "")).rstrip("/").endswith("/j/" + job_id),
        "The completion response names another job.",
    )
    return {"verified": True, "state": response["state"], "portal_url": response["portal_url"]}


def check_member_view(
    client: httpx.Client, job_id: str, sequence: int, refinish: UUID
) -> dict[str, Any]:
    def get(path: str, **params: str) -> dict[str, Any]:
        return dict(client.get(path, params=params).raise_for_status().json())

    scan = get(f"/scan/jobs/{job_id}/")
    _check(scan.get("id") == job_id, "serif reports another job ID.")
    _check(scan.get("state") == "complete", "The job is not complete after the re-finish.")
    pages = [p for p in scan["pages"] if p["logical_sequence_number"] == sequence]
    _check(len(pages) == 1, "The rescanned sequence number is not exactly one logical page.")
    page = pages[0]
    versions = sorted(page["available_versions"], key=lambda v: v["version"])
    _check(len(versions) >= 2, "The rescanned page keeps fewer than two versions.")
    active = page["active_version"]
    _check(
        active is not None and active["version"] == versions[-1]["version"],
        "The replacement is not the active version.",
    )
    checksums = [v["checksum_sha256"] for v in versions]
    _check(all(SHA256.fullmatch(c or "") for c in checksums), "A version has no checksum.")
    _check(checksums[0] != checksums[-1], "The replacement has the first capture's checksum.")
    # The interrupted and retried uploads must not have added versions anywhere else.
    _check(
        all(len(p["available_versions"]) == 1 for p in scan["pages"] if p is not page),
        "A page that was not rescanned holds more than one version.",
    )

    todos = get("/scan/todos/", job_id=job_id)["items"]
    _check(
        not [t for t in todos if t["state"] in ("open", "claimed")],
        "The job still has an outstanding to-do.",
    )
    fulfilled = [
        t
        for t in todos
        if t["todo_type"] == "rescan"
        and t["scan_page_id"] == page["id"]
        and t["state"] == "completed"
    ]
    _check(bool(fulfilled), "No completed rescan to-do exists for this page.")
    _check(
        all(t.get("claimed_by_id") is not None for t in fulfilled),
        "The rescan to-do was fulfilled without being claimed.",
    )

    finish = get(f"/scan-finalizations/{refinish}/")
    _check(finish.get("state") == "success", "The re-finish did not succeed.")
    _check(finish.get("scan_job_id") == job_id, "The re-finish belongs to another job.")
    _check(
        _timestamp(finish.get("started_at")) >= _timestamp(active["created_at"]),
        "The finish ran before the replacement arrived.",
    )
    variant_id = finish.get("cleaned_variant_id")
    _check(variant_id is not None, "The re-finish produced no cleaned variant.")

    book = get(f"/library/books/{scan['book_id']}/")
    variant = next((v for v in book["variants"] if v["id"] == variant_id), None)
    if variant is None:
        raise RunFailed("The re-finished PDF is not a current member download.")
    _check(
        variant["source_type"] == "cleaned" and str(variant["format"]).lower() == "pdf",
        "The re-finished variant is not the cleaned PDF.",
    )
    download = client.get(
        f"/library/books/{scan['book_id']}/download/", params={"variant_id": str(variant_id)}
    ).raise_for_status()
    _check(len(download.content) == variant["size_bytes"], "Incomplete PDF download.")
    _check(download.content.startswith(b"%PDF-"), "The download is not a PDF.")
    return {
        "job_state": scan["state"],
        "book_id": scan["book_id"],
        "page": {
            "page_id": page["id"],
            "sequence_number": sequence,
            "versions": [
                {"version": v["version"], "sha256": v["checksum_sha256"]} for v in versions
            ],
        },
        "rescan_todo_ids": [t["id"] for t in fulfilled],
        "refinish": {"finalization_id": str(refinish), "cleaned_variant_id": variant_id},
        "download": {
            "variant_id": variant_id,
            "bytes": len(download.content),
            "sha256": hashlib.sha256(download.content).hexdigest(),
        },
    }


def check_device_view(
    client: httpx.Client, job_id: str, sequence: int, active: str
) -> dict[str, Any]:
    """What the scanner itself sees for the job afterwards (read-only ``GET``)."""
    status = dict(client.get(f"/api/scanner/v1/jobs/{job_id}").raise_for_status().json())
    _check(status.get("state") == "complete", "The device API does not report the job complete.")
    _check(status.get("open_rescans") == 0, "The device API still lists open rescans.")
    _check("note" not in status, "The device API still asks for a re-finish.")
    held = [p for p in status["pages"] if p["sequence_number"] == sequence]
    _check(
        len(held) == 1 and held[0]["sha256"] == active,
        "The device API holds another checksum for the rescanned page.",
    )
    return {
        "state": status["state"],
        "sans_last_line_no": status.get("events", {}).get("sans_last_line_no"),
        "pages": len(status["pages"]),
        "covers": [c["position"] for c in status.get("covers", [])],
        "metadata_opf": status.get("metadata_opf"),
    }


def _origin(value: str) -> httpx.URL:
    url = httpx.URL(value)
    if url.scheme != "https" or not url.host or url.userinfo or url.path != "/" or url.query:
        raise ValueError("Specify an HTTPS staging origin such as https://serif.lan.example/.")
    return url


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--job-id", required=True, help="the scanner-assigned job ID")
    parser.add_argument("--sequence", type=int, required=True, help="the rescanned page")
    parser.add_argument("--refinish", type=UUID, required=True, help="re-finish job UUID")
    parser.add_argument(
        "--completion-response",
        type=Path,
        required=True,
        help="JSON file with the capture-complete body the scanner received",
    )
    parser.add_argument("--scanner-firmware", required=True)
    parser.add_argument("--server-commit", required=True)
    parser.add_argument("--scanner-key-file", type=Path, help="adds the device API check")
    parser.add_argument("--ca-file", type=Path, help="CA bundle for the staging certificate")
    args = parser.parse_args(argv)

    origin, session = os.getenv("SCANNER_SITE_RUN_ORIGIN"), os.getenv("SCANNER_SITE_RUN_SESSION")
    if not origin or not session:
        print(
            json.dumps(
                {"status": "unavailable", "reason": "staging origin or member session absent"}
            )
        )
        return 77
    verify: str | bool = str(args.ca_file) if args.ca_file else True
    try:
        if not DEVICE_JOB_ID.fullmatch(args.job_id):
            raise RunFailed("The job ID is not a scanner-assigned ID.")
        if not re.fullmatch(r"[0-9a-f]{7,40}", args.server_commit):
            raise ValueError("Give the deployed server commit as a git hash.")
        url = _origin(origin)
        completion = check_completion(
            json.loads(args.completion_response.read_text("utf-8")), args.job_id
        )
        with httpx.Client(
            base_url=url, cookies={"sessionid": session}, timeout=60, verify=verify
        ) as client:
            member = check_member_view(client, args.job_id, args.sequence, args.refinish)
        device = None
        if args.scanner_key_file:
            key = args.scanner_key_file.read_text("utf-8").strip()
            headers = {"Authorization": "Bearer " + key}
            with httpx.Client(base_url=url, headers=headers, timeout=30, verify=verify) as client:
                active = member["page"]["versions"][-1]["sha256"]
                device = check_device_view(client, args.job_id, args.sequence, active)
    except httpx.TransportError as exc:
        print(json.dumps({"status": "unavailable", "reason": type(exc).__name__}))
        return 77
    except (KeyError, OSError, ValueError, httpx.HTTPStatusError) as exc:
        print(json.dumps({"status": "failed", "job_id": args.job_id, "reason": str(exc)}))
        return 1
    record = {
        "status": "passed",
        "job_id": args.job_id,
        "scanner_firmware": args.scanner_firmware,
        "server_commit": args.server_commit,
        "completion": completion,
        "member": member,
        "device": device,
    }
    print(json.dumps(record, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
