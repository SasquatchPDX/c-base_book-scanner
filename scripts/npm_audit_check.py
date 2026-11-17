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

ROOT = Path(__file__).resolve().parent.parent
ALLOWLIST_PATH = ROOT / ".npm-audit-allowlist.json"
BLOCKING_SEVERITIES = {"high", "critical"}


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
    completed = subprocess.run(
        ["npm", "audit", "--json"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        shell=sys.platform == "win32",
    )
    try:
        audit = json.loads(completed.stdout)
    except json.JSONDecodeError:
        print(completed.stdout, completed.stderr, sep="\n")
        print("npm audit did not return JSON", file=sys.stderr)
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
