from pathlib import Path

import pytest

from ebook_server.core.models import StorageLocation
from ebook_server.core.storage_registry import StorageConfigurationError, backend_for_location
from ebook_server.storage import LocalFilesystemStorage


@pytest.mark.django_db
def test_mounted_storage_modes_resolve_to_configured_paths(tmp_path: Path) -> None:
    for backend_type in (
        StorageLocation.BackendType.NFS,
        StorageLocation.BackendType.SAMBA,
    ):
        mount_path = tmp_path / str(backend_type)
        location = StorageLocation.objects.create(
            identifier=f"mounted-{backend_type}",
            backend=backend_type,
            base_path=str(mount_path),
        )
        backend = backend_for_location(location)
        assert isinstance(backend, LocalFilesystemStorage)
        assert Path(backend.root) == mount_path


@pytest.mark.django_db
def test_unknown_legacy_storage_backend_fails_closed() -> None:
    location = StorageLocation.objects.create(
        identifier="legacy-object-store",
        backend="s3",
        base_path="unused",
    )
    with pytest.raises(StorageConfigurationError, match="unsupported storage backend"):
        backend_for_location(location)


def test_storage_choices_do_not_advertise_unimplemented_s3() -> None:
    assert "s3" not in dict(StorageLocation.BackendType.choices)
