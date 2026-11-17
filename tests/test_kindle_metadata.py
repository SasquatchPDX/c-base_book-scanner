"""MOBI/Kindle-family metadata extraction through Calibre's ``ebook-meta`` (mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from defusedxml.common import DefusedXmlException

from ebook_server.core import conversion, intake

KINDLE_OPF = b"""<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uuid_id" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Mobi Book</dc:title>
    <dc:creator opf:role="aut" opf:file-as="Mobi, Megan">Megan Mobi</dc:creator>
    <dc:creator opf:role="trl">Tom Translator</dc:creator>
    <dc:identifier id="uuid_id" opf:scheme="uuid">0c9d6f2e-uuid</dc:identifier>
    <dc:identifier opf:scheme="ISBN">9780306406157</dc:identifier>
    <dc:language>eng</dc:language>
    <dc:publisher>Kindle Press</dc:publisher>
    <dc:description>A short summary.</dc:description>
    <dc:subject>Science Fiction</dc:subject>
  </metadata>
</package>
"""

ENTITY_OPF = b"""<?xml version="1.0"?>
<!DOCTYPE package [<!ENTITY boom "boom">]>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>&boom;</dc:title></metadata>
</package>
"""


class FakeEbookMeta:
    """Stand-in for the conversion runner that writes a canned OPF like ebook-meta."""

    def __init__(self, payload: bytes | None, *, returncode: int = 0) -> None:
        self.payload = payload
        self.returncode = returncode
        self.calls: list[tuple[list[str], dict[str, Any]]] = []

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        self.calls.append((command, kwargs))
        if self.payload is not None:
            Path(command[command.index("--to-opf") + 1]).write_bytes(self.payload)
        return subprocess.CompletedProcess(command, self.returncode, None, "")


@pytest.fixture
def ebook_meta_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "ebook_server.core.intake.shutil.which",
        lambda name: f"/usr/bin/{name}",
    )


def _kindle_file(tmp_path: Path, name: str = "Some Book.mobi") -> Path:
    path = tmp_path / name
    path.write_bytes(b"irrelevant: ebook-meta is mocked")
    return path


@pytest.mark.parametrize("detected_format", sorted(intake.EBOOK_META_FORMATS))
@pytest.mark.usefixtures("ebook_meta_installed")
def test_kindle_metadata_is_read_from_ebook_meta_opf(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    detected_format: str,
) -> None:
    fake = FakeEbookMeta(KINDLE_OPF)
    monkeypatch.setattr(conversion, "_run_conversion_command", fake)
    source = _kindle_file(tmp_path, f"Some Book.{detected_format}")

    metadata = intake.extract_metadata(
        str(source),
        detected_format,
        original_filename=source.name,
    )

    assert metadata["title"] == "Mobi Book"
    assert metadata["authors"] == ["Megan Mobi"]  # translator role excluded
    assert metadata["identifier"] == "9780306406157"  # ISBN beats the UUID listed first
    assert metadata["language"] == "eng"
    assert metadata["publisher"] == "Kindle Press"
    assert metadata["description"] == "A short summary."
    assert metadata["subjects"] == ["Science Fiction"]
    assert metadata["container_type"] == detected_format
    assert metadata["raw_metadata"] == {"metadata_tool": "ebook-meta"}

    command, kwargs = fake.calls[0]
    assert command[0] == "/usr/bin/ebook-meta"
    assert command[1] == str(source.resolve())
    # The bounded runner kills the whole process group when this expires.
    assert kwargs["timeout"] == intake.EBOOK_META_TIMEOUT_SECONDS


def test_kindle_metadata_falls_back_when_ebook_meta_is_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ebook_server.core.intake.shutil.which", lambda name: None)
    fake = FakeEbookMeta(KINDLE_OPF)
    monkeypatch.setattr(conversion, "_run_conversion_command", fake)
    source = _kindle_file(tmp_path, "Untooled Book.mobi")

    metadata = intake.extract_metadata(str(source), "mobi", original_filename=source.name)

    assert metadata == {"title": "Untooled Book", "raw_metadata": {}, "container_type": "mobi"}
    assert fake.calls == []


@pytest.mark.usefixtures("ebook_meta_installed")
def test_kindle_metadata_falls_back_when_ebook_meta_times_out(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def timed_out(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise conversion.ConversionError("ebook-meta timed out after 30s")

    monkeypatch.setattr(conversion, "_run_conversion_command", timed_out)
    source = _kindle_file(tmp_path, "Slow Book.mobi")

    metadata = intake.extract_metadata(str(source), "mobi", original_filename=source.name)

    assert metadata["title"] == "Slow Book"
    assert metadata["container_type"] == "mobi"


@pytest.mark.usefixtures("ebook_meta_installed")
@pytest.mark.parametrize(
    ("payload", "returncode"),
    [
        (None, 0),  # no OPF written
        (KINDLE_OPF, 1),  # tool reported failure
        (b"", 0),  # empty OPF
        (b"<package><metadata>", 0),  # malformed XML
        (ENTITY_OPF, 0),  # entity declarations rejected by defusedxml
        (b" " * (intake.MAX_FORMAT_METADATA_BYTES + 1), 0),  # over the size cap
    ],
    ids=["no-output", "nonzero-exit", "empty", "malformed", "entities", "oversized"],
)
def test_kindle_metadata_falls_back_on_unusable_ebook_meta_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: bytes | None,
    returncode: int,
) -> None:
    monkeypatch.setattr(
        conversion,
        "_run_conversion_command",
        FakeEbookMeta(payload, returncode=returncode),
    )
    source = _kindle_file(tmp_path, "Odd Book.mobi")

    metadata = intake.extract_metadata(str(source), "mobi", original_filename=source.name)

    assert metadata == {"title": "Odd Book", "raw_metadata": {}, "container_type": "mobi"}


def test_parse_opf_metadata_rejects_entity_declarations() -> None:
    with pytest.raises(DefusedXmlException):
        intake.parse_opf_metadata(ENTITY_OPF)
