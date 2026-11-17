"""The npm audit allowlist wrapper used by CI."""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from types import ModuleType

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "npm_audit_check.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("npm_audit_check", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _audit(severity: str = "high", advisory: str = "GHSA-aaaa-bbbb-cccc") -> dict[str, object]:
    return {
        "vulnerabilities": {
            "picomatch": {
                "via": [
                    {
                        "source": 1,
                        "title": "ReDoS",
                        "url": f"https://github.com/advisories/{advisory}",
                        "severity": severity,
                        "range": "<=4.0.7",
                    }
                ]
            },
            "vite": {"via": ["picomatch"]},
        }
    }


def test_unlisted_high_advisory_fails() -> None:
    module = _load()
    problems = module.evaluate(_audit(), {}, date(2026, 11, 17))
    assert problems and "not allowlisted" in problems[0]


def test_allowlisted_advisory_passes_until_expiry() -> None:
    module = _load()
    allowlist = {"GHSA-aaaa-bbbb-cccc": {"reason": "build-only", "expires": "2026-12-31"}}
    assert module.evaluate(_audit(), allowlist, date(2026, 12, 31)) == []
    expired = module.evaluate(_audit(), allowlist, date(2027, 1, 1))
    assert expired and "expired" in expired[0]


def test_moderate_advisories_do_not_block() -> None:
    module = _load()
    assert module.evaluate(_audit(severity="moderate"), {}, date(2026, 11, 17)) == []


def test_allowlist_entries_require_reason_and_expiry(tmp_path: Path) -> None:
    module = _load()
    path = tmp_path / "allowlist.json"
    path.write_text('[{"advisory": "GHSA-x", "reason": ""}]', encoding="utf-8")
    with pytest.raises(SystemExit):
        module.load_allowlist(path)


def test_committed_allowlist_is_valid() -> None:
    module = _load()
    entries = module.load_allowlist()
    for entry in entries.values():
        date.fromisoformat(entry["expires"])
