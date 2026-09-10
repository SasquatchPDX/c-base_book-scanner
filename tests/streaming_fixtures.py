"""Streams that reject whole-artifact reads in storage regression tests."""

from io import BytesIO


class BoundedReadStream(BytesIO):
    def read(self, size: int | None = -1) -> bytes:
        assert size is not None and 0 < size <= 1 << 20, "artifact read must be bounded"
        return super().read(size)
