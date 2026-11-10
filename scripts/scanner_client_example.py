#!/usr/bin/env python3
"""Smoke-test client for the scanner capture API.

Logs in like a non-browser client, creates a scan job, uploads every image in a
directory in filename order (with SHA-256 checksums and retries), and optionally
finishes the job.

Example:
    uv run python scripts/scanner_client_example.py \\
        --base-url http://127.0.0.1:8000 --username scanner --password secret \\
        --directory ./pages --title "Test book" --finish --allow-raw-pages

See docs/scanner-protocol.md ("Client recipe") for the protocol details.
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp"}


class ScannerClient:
    def __init__(self, base_url: str, username: str, password: str, retries: int = 4) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.retries = retries
        self.http = httpx.Client(base_url=self.base_url, timeout=120.0, follow_redirects=False)

    # -- auth -----------------------------------------------------------------
    def login(self) -> None:
        """GET the login page for a csrftoken cookie, then POST the credentials."""
        self.http.cookies.clear()
        self.http.get("/accounts/login/").raise_for_status()
        token = self.http.cookies.get("csrftoken", "")
        response = self.http.post(
            "/accounts/login/",
            data={
                "username": self.username,
                "password": self.password,
                "csrfmiddlewaretoken": token,
            },
            headers={"Referer": f"{self.base_url}/accounts/login/"},
        )
        if response.status_code != 302 or "sessionid" not in self.http.cookies:
            raise SystemExit(f"login failed (HTTP {response.status_code})")

    def _headers(self, unsafe: bool) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if unsafe:
            # Rotated on login, so read the cookie fresh each time.
            headers["X-CSRFToken"] = self.http.cookies.get("csrftoken", "")
            headers["Referer"] = f"{self.base_url}/"
        return headers

    def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        """Send a request; retry transient failures and re-login once on 401."""
        relogged = False
        delay = 1.0
        attempt = 0
        while True:
            attempt += 1
            try:
                response = self.http.request(
                    method,
                    path,
                    headers=self._headers(method.upper() not in {"GET", "HEAD"}),
                    **kwargs,
                )
            except httpx.TransportError as exc:
                if attempt > self.retries:
                    raise
                print(f"  network error ({exc}); retrying in {delay:.0f}s")
            else:
                if response.status_code == 401 and not relogged:
                    print("  session expired; logging in again")
                    self.login()
                    relogged = True
                    continue
                # CSRF failures (403) are not transient; only retry server errors.
                if response.status_code < 500 or attempt > self.retries:
                    return response
                print(f"  HTTP {response.status_code}; retrying in {delay:.0f}s")
            time.sleep(delay)
            delay = min(delay * 2, 30.0)

    # -- scan API -------------------------------------------------------------
    def create_job(self, title: str, scanner_id: str, expected: int | None) -> str:
        data = {"provisional_title": title, "scanner_id": scanner_id}
        if expected:
            data["expected_page_count"] = str(expected)
        response = self.request("POST", "/scan/jobs/", data=data)
        response.raise_for_status()
        return str(response.json()["id"])

    def upload_page(self, job_id: str, path: Path, sequence: int, side: str) -> dict[str, Any]:
        payload = path.read_bytes()
        checksum = hashlib.sha256(payload).hexdigest()
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        response = self.request(
            "POST",
            f"/scan/jobs/{job_id}/pages/",
            data={
                "logical_sequence_number": str(sequence),
                "side": side,
                "checksum_sha256": checksum,
                "capture_timestamp": str(int(path.stat().st_mtime)),
                "scanner_metadata": '{"source": "scanner_client_example"}',
            },
            files={"file": (path.name, payload, mime)},
        )
        if response.status_code not in {200, 201}:
            raise SystemExit(
                f"upload of {path.name} failed: {response.status_code} {response.text}"
            )
        body: dict[str, Any] = response.json()
        body["http_status"] = response.status_code
        return body

    def finish(self, job_id: str, allow_raw_pages: bool) -> httpx.Response:
        return self.request(
            "POST",
            f"/scan/jobs/{job_id}/finish/",
            json={"allow_raw_pages": allow_raw_pages},
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--username", default=os.environ.get("SCANNER_USERNAME", ""))
    parser.add_argument("--password", default=os.environ.get("SCANNER_PASSWORD", ""))
    parser.add_argument("--directory", type=Path, required=True, help="directory of page images")
    parser.add_argument("--title", default="Scanner smoke test")
    parser.add_argument("--scanner-id", default="smoke-test")
    parser.add_argument(
        "--spreads",
        action="store_true",
        help="treat images as left/right pairs sharing a sequence number",
    )
    parser.add_argument("--finish", action="store_true", help="finish the job afterwards")
    parser.add_argument("--allow-raw-pages", action="store_true", help="finish with raw pages")
    args = parser.parse_args()

    if not args.username or not args.password:
        parser.error("--username and --password (or SCANNER_USERNAME/SCANNER_PASSWORD) required")
    images = sorted(
        path
        for path in args.directory.iterdir()
        if path.is_file() and path.suffix.lower() in IMAGE_SUFFIXES
    )
    if not images:
        parser.error(f"no images found in {args.directory}")

    client = ScannerClient(args.base_url, args.username, args.password)
    client.login()
    print("logged in")
    expected = (len(images) + 1) // 2 if args.spreads else len(images)
    job_id = client.create_job(args.title, args.scanner_id, expected)
    print(f"created job {job_id}")

    for index, image in enumerate(images):
        if args.spreads:
            sequence, side = index // 2 + 1, ("left" if index % 2 == 0 else "right")
        else:
            sequence, side = index + 1, "single"
        result = client.upload_page(job_id, image, sequence, side)
        state = "stored" if result["http_status"] == 201 else "already present"
        print(f"  {image.name}: sequence {sequence} {side} -> {state}")

    if args.finish:
        response = client.finish(job_id, args.allow_raw_pages)
        print(f"finish -> HTTP {response.status_code}: {response.text[:400]}")
        if response.status_code >= 400:
            return 1
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
