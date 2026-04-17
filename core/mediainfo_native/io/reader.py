"""Streaming binary reader helpers."""

from __future__ import annotations

from pathlib import Path


class BinaryReader:
    def __init__(self, source: str | Path) -> None:
        self.path = Path(source)
        self._fh = self.path.open("rb")

    def tell(self) -> int:
        return self._fh.tell()

    def seek(self, offset: int, whence: int = 0) -> int:
        return self._fh.seek(offset, whence)

    def read(self, size: int) -> bytes:
        return self._fh.read(size)

    def close(self) -> None:
        self._fh.close()

    def __enter__(self) -> "BinaryReader":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()
