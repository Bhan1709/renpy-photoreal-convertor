"""Tests for structured archive extraction behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Self

from converter.decompile.extract import extract_rpa_file_detailed


class _UnsafeArchive:
    def __init__(self, *_args, **_kwargs) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args) -> None:
        return None

    def list(self) -> list[str]:
        return ["../outside.txt"]

    def read(self, _filename: str) -> bytes:
        return b"unsafe"


def test_extract_rpa_file_rejects_path_traversal(monkeypatch, tmp_path: Path) -> None:
    archive_path = tmp_path / "game" / "archive.rpa"
    archive_path.parent.mkdir(parents=True)
    archive_path.write_bytes(b"fake archive")
    monkeypatch.setattr("rpatool.archive.RenPyArchive", _UnsafeArchive)

    result = extract_rpa_file_detailed(archive_path)

    assert not result.success
    assert result.error is not None
    assert "unsafe archive member" in result.error
    assert not (tmp_path / "game" / "outside.txt").exists()
