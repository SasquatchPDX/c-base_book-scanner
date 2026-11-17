"""Fail CI on high/critical npm advisories, except explicitly allowlisted ones.

`npm audit --audit-level=high` has no way to accept a specific advisory, so an
unfixable advisory in build-only tooling would block every PR. This wrapper runs
`npm audit --json`, and fails when any high or critical advisory is present that
is not listed in `.npm-audit-allowlist.json`, or whose allowlist entry has
expired. Every entry must carry a reason and an expiry date, so exceptions are
re-examined instead of living forever.
"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = ROOT / ".npm-audit-allowlist.json"
BLOCKING_SEVERITIES = {"high", "critical"}
SEVERITIES = {"info", "low", "moderate", "high", "critical"}


def validate_report(payload: Any, returncode: int) -> dict[str, Any]:
    """Accept npm's completed v2 reports, never operational errors or partial JSON."""
    if returncode not in (0, 1):
        raise ValueError(f"npm audit exited with status {returncode}")
    if not isinstance(payload, dict) or "error" in payload:
        raise ValueError("npm audit returned an error or a non-report payload")
    if payload.get("auditReportVersion") != 2:
        raise ValueError("npm audit did not return a v2 audit report")
    vulnerabilities = payload.get("vulnerabilities")
    metadata = payload.get("metadata")
    if not isinstance(vulnerabilities, dict) or not isinstance(metadata, dict):
        raise ValueError("npm audit report is incomplete")
    counts = metadata.get("vulnerabilities")
    if not isinstance(counts, dict) or any(
        type(counts.get(key)) is not int or counts[key] < 0 for key in SEVERITIES | {"total"}
    ):
        raise ValueError("npm audit report has invalid vulnerability counts")
    actual = dict.fromkeys(SEVERITIES, 0)
    for package, info in vulnerabilities.items():
        if (
            not isinstance(info, dict)
            or not isinstance(info.get("severity"), str)
            or info["severity"] not in SEVERITIES
        ):
            raise ValueError(f"npm audit report has an invalid package: {package}")
        actual[info["severity"]] += 1
        via = info.get("via")
        if not isinstance(via, list) or not via:
            raise ValueError(f"npm audit report has no advisory details: {package}")
        for advisory in via:
            if isinstance(advisory, str) and advisory in vulnerabilities:
                continue
            if (
                isinstance(advisory, dict)
                and isinstance(advisory.get("severity"), str)
                and advisory.get("severity") in SEVERITIES
                and isinstance(advisory.get("url"), str)
                and advisory["url"]
            ):
                continue
            raise ValueError(f"npm audit report has invalid advisory details: {package}")
    # A transitive pointer must ultimately reach a real advisory, not just another
    # pointer (or a cycle). Otherwise an incomplete report can hide a high finding.
    covered = {
        package
        for package, info in vulnerabilities.items()
        if any(
            isinstance(via, dict) and via["severity"] in BLOCKING_SEVERITIES for via in info["via"]
        )
    }
    while True:
        expanded = covered | {
            package
            for package, info in vulnerabilities.items()
            if any(isinstance(via, str) and via in covered for via in info["via"])
        }
        if expanded == covered:
            break
        covered = expanded
    if any(
        info["severity"] in BLOCKING_SEVERITIES and package not in covered
        for package, info in vulnerabilities.items()
    ):
        raise ValueError("npm audit report is missing high/critical advisory details")
    if any(counts[key] != actual[key] for key in SEVERITIES) or counts["total"] != len(
        vulnerabilities
    ):
        raise ValueError("npm audit vulnerability counts do not match its package report")
    if returncode == 1 and not vulnerabilities:
        raise ValueError("npm audit failed without reporting vulnerabilities")
    return payload


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    entries = json.loads(path.read_text(encoding="utf-8"))
    allowlist: dict[str, dict[str, str]] = {}
    for entry in entries:
        advisory = entry["advisory"]
        if not entry.get("reason") or not entry.get("expires"):
            raise SystemExit(f"allowlist entry {advisory} needs a reason and an expires date")
        allowlist[advisory] = entry
    return allowlist


def blocking_advisories(audit: dict[str, object]) -> dict[str, str]:
    """Return {advisory-id: summary} for every high/critical advisory in the report."""
    found: dict[str, str] = {}
    vulnerabilities = audit.get("vulnerabilities", {})
    assert isinstance(vulnerabilities, dict)
    for package, info in vulnerabilities.items():
        for via in info.get("via", []):
            if not isinstance(via, dict):
                continue  # transitive pointer to another package's entry
            if via.get("severity") not in BLOCKING_SEVERITIES:
                continue
            url = str(via.get("url", ""))
            advisory = url.rsplit("/", 1)[-1] or f"{package}:{via.get('source')}"
            found[advisory] = f"{package} {via.get('range', '')}: {via.get('title', '')}"
    return found


def evaluate(
    audit: dict[str, object],
    allowlist: dict[str, dict[str, str]],
    today: date,
) -> list[str]:
    problems: list[str] = []
    for advisory, summary in sorted(blocking_advisories(audit).items()):
        entry = allowlist.get(advisory)
        if entry is None:
            problems.append(f"{advisory} ({summary}) is not allowlisted")
        elif date.fromisoformat(entry["expires"]) < today:
            problems.append(f"{advisory} allowlist entry expired on {entry['expires']}")
    return problems


def main() -> int:
    try:
        completed = subprocess.run(
            ["npm", "audit", "--json"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            shell=sys.platform == "win32",
        )
        audit = validate_report(json.loads(completed.stdout), completed.returncode)
    except (OSError, UnicodeError, ValueError) as exc:
        print(f"npm audit could not complete: {exc}", file=sys.stderr)
        return 2
    allowlist = load_allowlist()
    problems = evaluate(audit, allowlist, date.today())
    for advisory in sorted(set(blocking_advisories(audit)) & set(allowlist)):
        entry = allowlist[advisory]
        print(f"allowlisted until {entry['expires']}: {advisory} - {entry['reason']}")
    if problems:
        for problem in problems:
            print(f"npm audit: {problem}", file=sys.stderr)
        return 1
    print("npm audit: no unaccepted high/critical advisories")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
