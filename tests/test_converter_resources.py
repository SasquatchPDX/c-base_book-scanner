"""Harmless real subprocesses exercise converter resource and recovery boundaries."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from ebook_server.core import conversion, intake
from ebook_server.core.models import FileVariant, ProcessingJob
from ebook_server.settings import Settings
from tests.test_conversion import StubConverter
from tests.test_conversion_claim import create_conversion


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize("pause", [0, 10])
def test_noisy_command_is_stopped_with_bounded_diagnostics(
    monkeypatch: pytest.MonkeyPatch, stream: str, pause: int
) -> None:
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_CONVERTER_MAX_DIAGNOSTIC_BYTES", 4096)
    with pytest.raises(conversion.ConversionError, match="diagnostic limit") as failure:
        conversion._run_conversion_command(
            [
                sys.executable,
                "-c",
                f"import sys,time; sys.{stream}.write('x' * 100000); "
                f"sys.{stream}.flush(); time.sleep({pause})",
            ],
            check=True,
            capture_output=True,
        )
    assert len(str(failure.value)) < 2200
    result = conversion._run_conversion_command(
        [sys.executable, "-c", "print('next job')"], check=True, capture_output=True
    )
    assert result.stdout.strip() == "next job"


def test_successful_stdout_capture_has_a_fixed_memory_bound() -> None:
    result = conversion._run_conversion_command(
        [sys.executable, "-c", "print('a' * 200000)"], check=True, capture_output=True
    )
    assert len(result.stdout) == 65536


def test_scratch_environment_and_working_directory_are_private(tmp_path: Path) -> None:
    result = conversion._run_conversion_command(
        [
            sys.executable,
            "-c",
            "import os; from pathlib import Path; "
            "assert Path.cwd() == Path(os.environ['TMPDIR']); "
            "assert os.environ['CALIBRE_TEMP_DIR'] == os.environ['TMPDIR']; print('private')",
        ],
        check=True,
        capture_output=True,
        scratch_dir=tmp_path,
    )
    assert result.stdout.strip() == "private"


@pytest.mark.parametrize(
    "field",
    [
        "converter_max_diagnostic_bytes",
        "converter_max_output_bytes",
        "converter_max_scratch_bytes",
        "converter_address_space_bytes",
    ],
)
def test_resource_configuration_rejects_unbounded_zero_values(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        Settings(**{field: 0})  # type: ignore[arg-type]


@pytest.mark.skipif(sys.platform != "linux", reason="Linux process-group and /proc verification")
def test_timeout_reaps_converter_descendants_and_allows_next_command(tmp_path: Path) -> None:
    script = (
        "import subprocess,sys,time; from pathlib import Path; "
        "child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); "
        "Path('child.pid').write_text(str(child.pid)); time.sleep(30)"
    )
    with pytest.raises(conversion.ConversionError, match="timed out"):
        conversion._run_conversion_command(
            [sys.executable, "-c", script], check=True, timeout=1, scratch_dir=tmp_path
        )
    pid = int((tmp_path / "child.pid").read_text())
    status = Path(f"/proc/{pid}/stat")
    # An orphan may briefly be a zombie awaiting init's reap; it cannot execute.
    assert not status.exists() or status.read_text().rsplit(")", 1)[1].split()[0] == "Z"
    result = conversion._run_conversion_command(
        [sys.executable, "-c", "print('recovered')"], check=True, capture_output=True
    )
    assert result.stdout.strip() == "recovered"


@pytest.mark.skipif(os.name != "posix", reason="POSIX exec resource limits")
def test_child_address_space_limit_is_applied_before_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "django.conf.settings.BOOK_SCANNER_CONVERTER_ADDRESS_SPACE_BYTES", 64 * 1024**2
    )
    with pytest.raises(conversion.ConversionError, match="MemoryError"):
        conversion._run_conversion_command(
            [sys.executable, "-c", "data = bytearray(128 * 1024**2)"], check=True
        )


@pytest.mark.skipif(os.name != "posix", reason="POSIX exec file-size limit")
def test_child_file_size_limit_is_hard_even_between_monitor_samples(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_CONVERTER_MAX_OUTPUT_BYTES", 1024)
    with pytest.raises(conversion.ConversionError):
        conversion._run_conversion_command(
            [
                sys.executable,
                "-c",
                "from pathlib import Path; Path('too-big').write_bytes(b'x'*10000)",
            ],
            check=True,
            scratch_dir=tmp_path,
        )
    assert (tmp_path / "too-big").stat().st_size <= 1024


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("mode", ["noisy", "oversized", "scratch", "timeout"])
def test_limit_failure_preserves_original_reclaims_scratch_and_allows_next_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    job, storage = create_conversion(tmp_path, monkeypatch)
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    for name, value in {
        "SCRATCH_ROOT": str(scratch),
        "MAX_DIAGNOSTIC_BYTES": 2048,
        "MAX_OUTPUT_BYTES": 1024,
        "MAX_SCRATCH_BYTES": 4096,
    }.items():
        monkeypatch.setattr(f"django.conf.settings.BOOK_SCANNER_CONVERTER_{name}", value)
    processes: list[subprocess.Popen[Any]] = []
    real_popen = subprocess.Popen

    def record_process(*args: Any, **kwargs: Any) -> subprocess.Popen[Any]:
        process = real_popen(*args, **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(subprocess, "Popen", record_process)
    scripts = {
        "noisy": "import sys; sys.stderr.write('x' * 100000)",
        "oversized": "from pathlib import Path; import sys; "
        "Path(sys.argv[1]).write_bytes(b'x'*10000)",
        "scratch": "from pathlib import Path; "
        "[Path(f'aux-{i}').write_bytes(b'x'*800) for i in range(8)]",
        "timeout": "import time; time.sleep(10)",
    }

    class HarmlessTool(StubConverter):
        def convert(self, **kwargs: Any) -> None:
            output = kwargs["destination_path"]
            conversion._run_conversion_command(
                [sys.executable, "-c", scripts[mode], str(output)],
                check=True,
                timeout=0.5 if mode == "timeout" else 10,
                scratch_dir=output.parent,
                output_path=output,
            )

    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": HarmlessTool(b"")})
    hashes: list[str] = []
    real_hash = intake.compute_sha256

    def record_hash(path: str) -> str:
        hashes.append(path)
        return real_hash(path)

    monkeypatch.setattr(intake, "compute_sha256", record_hash)
    conversion.process_conversion_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.FAILED, job.details
    assert job.details["error"]
    assert hashes == []  # reject oversized/generated files before hashing or publication
    assert processes and all(process.poll() is not None for process in processes)
    assert list(scratch.iterdir()) == []
    assert FileVariant.objects.count() == 1
    source = FileVariant.objects.get(pk=job.file_variant_id)
    with storage.open(source.storage_identifier) as stream:
        assert stream.read() == b"original pdf"
    assert storage.hash(source.storage_identifier) == source.sha256

    monkeypatch.setattr(
        conversion, "CONVERTER_REGISTRY", {"calibre": StubConverter(b"next output")}
    )
    monkeypatch.setattr(conversion, "get_queue", lambda: None)
    next_job = conversion.request_conversion(source_variant=source, target_format="txt")
    assert next_job.state == ProcessingJob.JobState.SUCCESS, next_job.details
    assert len(hashes) == 1
    assert list(scratch.iterdir()) == []
    assert storage.hash(source.storage_identifier) == source.sha256


@pytest.mark.django_db(transaction=True)
def test_in_process_converter_output_is_rejected_before_hashing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_CONVERTER_MAX_OUTPUT_BYTES", 2)
    conversion.process_conversion_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.FAILED
    assert "output limit exceeded" in job.details["error"]
    assert FileVariant.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_version_probe_and_conversion_share_one_scratch_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    job, _ = create_conversion(tmp_path, monkeypatch)
    monkeypatch.setattr("django.conf.settings.BOOK_SCANNER_CONVERTER_MAX_SCRATCH_BYTES", 1024)

    class ProbeScratchTool(StubConverter):
        def version(self) -> str:
            result = conversion._run_conversion_command(
                [
                    sys.executable,
                    "-c",
                    "from pathlib import Path; "
                    "Path('probe-cache').write_bytes(b'x'*800); print('probe-v1')",
                ],
                check=True,
                capture_output=True,
            )
            return result.stdout.strip()

    monkeypatch.setattr(conversion, "CONVERTER_REGISTRY", {"calibre": ProbeScratchTool(b"x" * 300)})
    conversion.process_conversion_job(str(job.job_id))
    job.refresh_from_db()
    assert job.state == ProcessingJob.JobState.FAILED
    assert "scratch limit exceeded" in job.details["error"]
    assert FileVariant.objects.count() == 1
