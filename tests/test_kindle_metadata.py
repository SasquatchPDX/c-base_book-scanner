"""MOBI/Kindle-family metadata extraction through Calibre's ``ebook-meta`` (mocked)."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
from defusedxml.common import DefusedXmlException
from django.conf import settings as django_settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, override_settings

from ebook_server.core import conversion, intake
from ebook_server.core.models import (
    ImportJob,
    ReviewTask,
    SourceLocation,
    StorageLocation,
    get_or_create_profile,
)

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

    assert metadata == {
        "title": "Untooled Book",
        "raw_metadata": {},
        "container_type": "mobi",
        "title_source": "filename",
    }
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

    assert metadata == {
        "title": "Odd Book",
        "raw_metadata": {},
        "container_type": "mobi",
        "title_source": "filename",
    }


@pytest.mark.usefixtures("ebook_meta_installed")
def test_ebook_meta_staging_name_title_is_not_embedded_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Calibre names an untitled book after the file it read: the upload staging path.
    staged = _kindle_file(tmp_path, "upload-qe6h_xjg9.tmp")
    # ... with "_" turned into a space.
    opf = KINDLE_OPF.replace(b"Mobi Book", b"upload-qe6h xjg9")
    monkeypatch.setattr(conversion, "_run_conversion_command", FakeEbookMeta(opf))

    metadata = intake.extract_metadata(str(staged), "azw3", original_filename="Kindle Story.azw3")

    assert metadata["title"] == "Kindle Story"
    assert metadata["title_source"] == "filename"
    assert metadata["authors"] == ["Megan Mobi"]


@pytest.mark.usefixtures("ebook_meta_installed")
def test_ebook_meta_title_equal_to_ordinary_source_stem_stays_embedded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A source-folder file is read in place: "Midworld.mobi" really titled "Midworld"
    # is embedded evidence, not Calibre's untitled-book placeholder.
    source = _kindle_file(tmp_path, "Midworld.mobi")
    opf = KINDLE_OPF.replace(b"Mobi Book", b"Midworld")
    monkeypatch.setattr(conversion, "_run_conversion_command", FakeEbookMeta(opf))

    metadata = intake.extract_metadata(str(source), "mobi", original_filename=source.name)

    assert metadata["title"] == "Midworld"
    assert metadata["title_source"] == "embedded"


@pytest.mark.parametrize(
    ("file_name", "original_filename", "expected"),
    [
        ("upload-qe6h_xjg9.tmp", "Kindle Story.azw3", True),
        ("upload-qe6h_xjg9.tmp", "upload-qe6h_xjg9.tmp", False),  # the real file itself
        ("Midworld.mobi", "Midworld.mobi", False),
        ("upload-notes.mobi", "Other.mobi", False),  # not the staging suffix
        ("Upload-ABC.tmp", "Other.mobi", False),  # not NamedTemporaryFile's alphabet
    ],
)
def test_is_upload_staging_path(file_name: str, original_filename: str, expected: bool) -> None:
    path = str(Path("staging") / file_name)
    assert intake.is_upload_staging_path(path, original_filename) is expected


MIDWORLD_EBOOK_META_OPF = b"""<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="uuid_id" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Midworld</dc:title>
    <dc:creator opf:role="aut" opf:file-as="Foster, Alan Dean">Alan Dean Foster</dc:creator>
    <dc:identifier id="uuid_id" opf:scheme="uuid">0c9d6f2e-midworld</dc:identifier>
    <dc:language>eng</dc:language>
  </metadata>
</package>
"""

STALE_PHYLOGENESIS_SIDECAR = """<?xml version="1.0" encoding="UTF-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="2.0">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:opf="http://www.idpf.org/2007/opf">
    <dc:title>Phylogenesis</dc:title>
    <dc:creator opf:role="aut">Alan Dean Foster</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier opf:scheme="ISBN">034549427X</dc:identifier>
  </metadata>
</package>
"""


class _EchoProvider:
    """Returns Phylogenesis for an ISBN query and echoes the title otherwise."""

    provider_id = "kindle-echo"

    def __init__(self) -> None:
        self.isbn_queries: list[str] = []

    def search(
        self,
        title: str | None = None,
        *,
        author: str | None = None,
        isbn: str | None = None,
        folder_hint: str | None = None,
    ) -> list[dict[str, Any]]:
        del folder_hint
        if isbn:
            self.isbn_queries.append(isbn)
            return [
                {
                    "key": f"isbn:{isbn}",
                    "title": "Phylogenesis",
                    "author_name": ["Alan Dean Foster"],
                    "isbn_13": ["9780345494276"],
                }
            ]
        return [{"key": "echo", "title": title or "Unknown", "author_name": [author or "?"]}]


def _mobi_bytes() -> bytes:
    payload = bytearray(256)
    payload[60:68] = b"BOOKMOBI"
    return bytes(payload)


def _default_storage() -> None:
    StorageLocation.objects.get_or_create(
        identifier="default",
        defaults={"backend": StorageLocation.BackendType.LOCAL, "base_path": "default"},
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("ebook_meta_installed")
def test_source_kindle_title_equal_to_basename_survives_stale_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # PR #81 verifier probe: incoming Midworld.mobi embeds Midworld / Alan Dean Foster
    # next to a stale metadata.opf for Phylogenesis (ISBN 034549427X). The genuine
    # embedded title equals the file stem; it must stay embedded so the sidecar
    # contradiction goes to review instead of importing the book as Phylogenesis.
    provider = _EchoProvider()
    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [provider],
    )
    monkeypatch.setattr(
        conversion, "_run_conversion_command", FakeEbookMeta(MIDWORLD_EBOOK_META_OPF)
    )
    _default_storage()
    root = tmp_path / "library"
    book_dir = root / "Alan Dean Foster" / "Midworld"
    book_dir.mkdir(parents=True)
    (book_dir / "metadata.opf").write_text(STALE_PHYLOGENESIS_SIDECAR, encoding="utf-8")
    (book_dir / "Midworld.mobi").write_bytes(_mobi_bytes())
    location = SourceLocation.objects.create(
        name="kindle-midworld",
        root_path=str(root),
        transfer_mode=SourceLocation.TransferMode.COPY,
        recursive=True,
        default_source_type=SourceLocation.SourceType.INCOMING_FOLDER,
        is_active=True,
    )

    with override_settings(APP_DATA_DIR=tmp_path / "app-data"):
        (discovered,) = intake.discover_source_jobs_for_location(location)
        intake.process_import_job(str(discovered.id))
    job = ImportJob.objects.get(pk=discovered.pk)

    assert job.evidence["title"] == "Midworld"
    assert job.evidence["title_source"] == "embedded"
    assert intake.sidecar_conflicts_in(job.evidence)
    assert provider.isbn_queries == []
    assert job.book is None or job.book.title != "Phylogenesis"
    assert job.state == ImportJob.State.LOW_CONFIDENCE
    assert ReviewTask.objects.filter(import_job=job, state=ReviewTask.State.OPEN).exists()


class _StagingNameEbookMeta:
    """ebook-meta on an untitled book: Calibre reports the file's own stem ("_" -> " ")."""

    def __call__(self, command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        del kwargs
        stem = Path(command[1]).stem.replace("_", " ")
        opf = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/" '
            'xmlns:opf="http://www.idpf.org/2007/opf">'
            f'<dc:title>{stem}</dc:title><dc:creator opf:role="aut">Jane Doe</dc:creator>'
            "</metadata></package>"
        )
        Path(command[command.index("--to-opf") + 1]).write_text(opf, encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, None, "")


@pytest.mark.django_db(transaction=True)
@pytest.mark.usefixtures("ebook_meta_installed")
def test_untitled_kindle_upload_staging_name_is_not_embedded_title(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubProvider:
        provider_id = "stub"

        def search(self, title: str | None = None, **kwargs: Any) -> list[dict[str, Any]]:
            del title, kwargs
            return [{"title": "Kindle Story", "author_name": ["Jane Doe"]}]

    monkeypatch.setattr(
        "ebook_server.core.identification._provider_instances",
        lambda: [StubProvider()],
    )
    monkeypatch.setattr(conversion, "_run_conversion_command", _StagingNameEbookMeta())
    # Same thresholds as test_intake's azw3 upload test.
    monkeypatch.setattr("ebook_server.core.intake.AUTO_ACCEPT_SCORE", 0.5)
    monkeypatch.setattr("ebook_server.core.intake.REVIEW_SCORE", 0.3)
    monkeypatch.setattr(django_settings, "BOOK_SCANNER_IDENTIFICATION_STRONG_MATCH_ENABLED", True)
    _default_storage()
    client = Client()
    client.force_login(get_or_create_profile("kindle-staging-user").user)

    response = client.post(
        "/imports/upload/",
        {
            "file": SimpleUploadedFile(
                "Kindle Story.azw3", _mobi_bytes(), content_type="application/octet-stream"
            )
        },
    )

    assert response.status_code == 201
    job = ImportJob.objects.get(pk=response.json()["job_id"])
    assert job.evidence["title"] == "Kindle Story"
    assert job.evidence["title_source"] == "filename"
    assert job.state == ImportJob.State.COMPLETE
    assert job.book is not None and job.book.title == "Kindle Story"


def test_parse_opf_metadata_rejects_entity_declarations() -> None:
    with pytest.raises(DefusedXmlException):
        intake.parse_opf_metadata(ENTITY_OPF)
