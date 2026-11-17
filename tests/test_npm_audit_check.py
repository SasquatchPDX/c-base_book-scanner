"""The npm audit allowlist wrapper used by CI."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from datetime import date
from pathlib import Path
from types import ModuleType
from typing import Any

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
        "auditReportVersion": 2,
        "metadata": {
            "vulnerabilities": {
                **dict.fromkeys(("info", "low", "moderate", "high", "critical"), 0),
                severity: 2,
                "total": 2,
            }
        },
        "vulnerabilities": {
            "picomatch": {
                "severity": severity,
                "via": [
                    {
                        "source": 1,
                        "title": "ReDoS",
                        "url": f"https://github.com/advisories/{advisory}",
                        "severity": severity,
                        "range": "<=4.0.7",
                    }
                ],
            },
            "vite": {"severity": severity, "via": ["picomatch"]},
        },
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


def _clean_report() -> dict[str, Any]:
    return {
        "auditReportVersion": 2,
        "vulnerabilities": {},
        "metadata": {
            "vulnerabilities": dict.fromkeys(
                ("info", "low", "moderate", "high", "critical", "total"), 0
            )
        },
    }


def _main(
    monkeypatch: pytest.MonkeyPatch,
    payload: object,
    returncode: int,
    allowlist: dict[str, dict[str, str]] | None = None,
) -> int:
    module = _load()
    completed = subprocess.CompletedProcess(
        ["npm", "audit", "--json"], returncode, json.dumps(payload)
    )
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: completed)
    monkeypatch.setattr(module, "load_allowlist", lambda: allowlist or {})
    return int(module.main())


def test_main_accepts_successful_clean_report(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(monkeypatch, _clean_report(), 0) == 0


@pytest.mark.parametrize("returncode", [0, 1])
def test_main_blocks_unlisted_advisories_even_if_npm_exits_zero(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    assert _main(monkeypatch, _audit(), returncode) == 1


def test_main_accepts_valid_vulnerable_report_under_existing_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # npm exits 1 for a completed vulnerable report; this is not an operational failure.
    allowlist = {"GHSA-aaaa-bbbb-cccc": {"reason": "test-only exception", "expires": "9999-12-31"}}
    assert _main(monkeypatch, _audit(), 1, allowlist) == 0
    assert _main(monkeypatch, _audit("moderate"), 1) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"error": {"code": "E503", "summary": "registry unavailable"}},
        {"error": {}, **_clean_report()},
        {},
        [],
        None,
        {"auditReportVersion": 2, "vulnerabilities": {}},
        {"auditReportVersion": 2, "metadata": _clean_report()["metadata"]},
        {**_clean_report(), "auditReportVersion": 1},
        {**_clean_report(), "metadata": {"vulnerabilities": {"total": 0}}},
        {**_clean_report(), "vulnerabilities": {"hidden": {"severity": "high", "via": []}}},
    ],
)
def test_main_rejects_errors_nonreports_and_incomplete_reports(
    monkeypatch: pytest.MonkeyPatch, payload: object, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _main(monkeypatch, payload, 1) == 2
    assert "no unaccepted" not in capsys.readouterr().out


@pytest.mark.parametrize("returncode", [2, 127, -9])
def test_main_rejects_operational_exit_even_with_valid_json(
    monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    assert _main(monkeypatch, _clean_report(), returncode) == 2


def test_main_rejects_failure_with_zero_vulnerabilities(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _main(monkeypatch, _clean_report(), 1) == 2


@pytest.mark.parametrize(
    "via",
    [[], ["missing"], ["picomatch"], [{"severity": "low", "url": "https://example.test/advisory"}]],
)
def test_main_rejects_missing_or_cyclic_high_advisory_details(
    monkeypatch: pytest.MonkeyPatch, via: list[object]
) -> None:
    payload = _audit()
    vulnerabilities = payload["vulnerabilities"]
    assert isinstance(vulnerabilities, dict)
    vulnerabilities["picomatch"]["via"] = via
    assert _main(monkeypatch, payload, 1) == 2


@pytest.mark.parametrize("stdout", ["", "registry unavailable", "{truncated"])
def test_main_rejects_non_json(monkeypatch: pytest.MonkeyPatch, stdout: str) -> None:
    module = _load()
    completed = subprocess.CompletedProcess(["npm"], 1, stdout)
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: completed)
    assert module.main() == 2


def test_main_handles_missing_npm(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _load()

    def missing(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError("npm unavailable")

    monkeypatch.setattr(module.subprocess, "run", missing)
    assert module.main() == 2
