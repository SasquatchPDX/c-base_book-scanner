"""Received-inventory and manifest comparison for scanner jobs (#127, contract §6.4/§6.9).

``GET jobs/{id}`` reports what serif holds for a job, and ``capture-complete`` compares
the scanner's full local manifest with it. Both are built from *manifest sources*: one
per kind of item (pages, covers, ``metadata.opf``), each registered under the field name
it uses in both payloads.

Only pages exist so far (``ScanPage``/``ScanPageVersion``). The tickets that store covers
(S4) and the OPF (S6) register their own source with :func:`register_manifest_source`;
the stored ``scan.jsonl`` events (S5) register the event high-water mark with
:func:`register_event_high_water_mark`. Until then:

* a field with no registered source is omitted from ``GET jobs/{id}`` and ignored in a
  ``capture-complete`` request, so nothing is expected for it;
* the event high-water mark is ``0``, and ``capture-complete`` compares the request's
  ``sans_last_line_no`` with that ``0``.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Final, Protocol

from .models import ScanJob, ScanPageVersion

_SHA256_RE: Final = re.compile(r"[0-9a-f]{64}")
EVENTS_ITEM_KEY: Final = "events"


class ManifestError(ValueError):
    """The request manifest is malformed; ``field`` names the offending part."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name


@dataclass(frozen=True)
class ReceivedItem:
    sha256: str
    version: int


class ManifestSource(Protocol):
    """One kind of job item, as reported by ``GET jobs/{id}`` and checked by
    ``capture-complete``. Item keys (``page:12``, ``cover:front``, ``metadata_opf``)
    appear in ``details.missing``/``details.mismatched``."""

    #: Field name in both the status payload and the capture-complete request.
    name: str

    def received(self, job: ScanJob) -> dict[str, ReceivedItem]:
        """Active item per item key."""
        ...

    def status(self, job: ScanJob) -> Any:
        """JSON value of ``name`` in ``GET jobs/{id}``."""
        ...

    def expected(self, value: Any) -> dict[str, str]:
        """Item key -> SHA-256 from the request's ``name`` field (``None`` if absent).

        Raises :class:`ManifestError` for a malformed value.
        """
        ...


def _sha256(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise ManifestError(field_name, f"{field_name} must be a lowercase hex SHA-256.")
    return value


class PageManifestSource:
    """Pages (§4.2): one item per ``sequence_number`` with its active version."""

    name = "pages"

    @staticmethod
    def item_key(sequence_number: int) -> str:
        return f"page:{sequence_number}"

    def _active_versions(self, job: ScanJob) -> list[ScanPageVersion]:
        return list(
            ScanPageVersion.objects.filter(scan_page__scan_job=job, is_active=True)
            .select_related("scan_page")
            .order_by("scan_page__logical_sequence_number", "scan_page__side")
        )

    def received(self, job: ScanJob) -> dict[str, ReceivedItem]:
        return {
            self.item_key(version.scan_page.logical_sequence_number): ReceivedItem(
                version.checksum_sha256, version.version
            )
            for version in self._active_versions(job)
        }

    def status(self, job: ScanJob) -> list[dict[str, Any]]:
        return [
            {
                "sequence_number": version.scan_page.logical_sequence_number,
                "sha256": version.checksum_sha256,
                "version": version.version,
            }
            for version in self._active_versions(job)
        ]

    def expected(self, value: Any) -> dict[str, str]:
        if value is None:
            return {}
        if not isinstance(value, list):
            raise ManifestError("pages", "pages must be a list.")
        expected: dict[str, str] = {}
        for entry in value:
            if not isinstance(entry, dict):
                raise ManifestError("pages", "Each page must be an object.")
            number = entry.get("sequence_number")
            if not isinstance(number, int) or isinstance(number, bool) or number < 0:
                raise ManifestError("pages", "sequence_number must be a non-negative integer.")
            key = self.item_key(number)
            if key in expected:
                raise ManifestError("pages", f"sequence_number {number} is listed twice.")
            expected[key] = _sha256(entry.get("sha256"), "pages.sha256")
        return expected


EventHighWaterMark = Callable[[ScanJob], int]

_sources: dict[str, ManifestSource] = {}
_event_high_water_mark: EventHighWaterMark | None = None


def register_manifest_source(source: ManifestSource) -> None:
    """Add (or replace) the source for ``source.name``. Called at import/app-ready time."""
    _sources[source.name] = source


def unregister_manifest_source(name: str) -> None:
    _sources.pop(name, None)


def manifest_sources() -> list[ManifestSource]:
    return list(_sources.values())


def register_event_high_water_mark(provider: EventHighWaterMark | None) -> None:
    """Install the stored-events high-water mark (S5); ``None`` restores the default 0."""
    global _event_high_water_mark
    _event_high_water_mark = provider


def event_high_water_mark(job: ScanJob) -> int:
    """Highest stored ``sans`` event ``line_no`` of ``job``; ``0`` with no event store."""
    if _event_high_water_mark is None:
        return 0
    return int(_event_high_water_mark(job))


register_manifest_source(PageManifestSource())


@dataclass
class ManifestCheck:
    missing: list[str] = field(default_factory=list)
    mismatched: list[str] = field(default_factory=list)
    sans_last_line_no: int = 0

    @property
    def verified(self) -> bool:
        return not (self.missing or self.mismatched)


def parse_last_line_no(payload: Mapping[str, Any]) -> int:
    value = payload.get("sans_last_line_no", 0)
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ManifestError(
            "sans_last_line_no", "sans_last_line_no must be a non-negative integer."
        )
    return value


def expected_items(payload: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """Parse every registered field of the request; fields with no source are ignored."""
    return {source.name: source.expected(payload.get(source.name)) for source in _sources.values()}


def check_manifest(job: ScanJob, payload: Mapping[str, Any]) -> ManifestCheck:
    """Compare a capture-complete manifest with what serif holds for ``job``.

    Validate the whole request first (raises :class:`ManifestError`), then report every
    listed item that is absent (``missing``) or whose active checksum differs
    (``mismatched``). Items serif holds but the manifest does not list are not checked.
    The ``events`` key is mismatched when the high-water marks differ.
    """
    expected_last_line_no = parse_last_line_no(payload)
    expected = expected_items(payload)
    check = ManifestCheck(sans_last_line_no=event_high_water_mark(job))
    for name, items in expected.items():
        received = _sources[name].received(job)
        for key, sha256 in items.items():
            held = received.get(key)
            if held is None:
                check.missing.append(key)
            elif held.sha256 != sha256:
                check.mismatched.append(key)
    if expected_last_line_no != check.sans_last_line_no:
        check.mismatched.append(EVENTS_ITEM_KEY)
    return check


def status_sections(job: ScanJob) -> dict[str, Any]:
    """The registered sections of ``GET jobs/{id}`` plus the event high-water mark."""
    sections: dict[str, Any] = {"events": {"sans_last_line_no": event_high_water_mark(job)}}
    for source in _sources.values():
        sections[source.name] = source.status(job)
    return sections
