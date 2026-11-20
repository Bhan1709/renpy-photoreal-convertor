"""Tests for isolated source-game staging."""

from __future__ import annotations

from pathlib import Path

from converter.decompile.staging import copy_game_files


def test_copy_game_files_accepts_uppercase_script_extensions(tmp_path: Path) -> None:
    source = tmp_path / "source" / "game"
    source.mkdir(parents=True)
    (source / "script.RPYC").write_bytes(b"compiled")
    destination = tmp_path / "staged"

    copied = copy_game_files(source, destination)

    assert (destination / "script.RPYC").is_file()
    assert copied == [destination / "script.RPYC"]


def test_copy_game_files_skips_prior_archive_extractions(tmp_path: Path) -> None:
    source = tmp_path / "source" / "game"
    prior_extraction = source / "archive"
    prior_extraction.mkdir(parents=True)
    (source / "archive.rpa").write_bytes(b"fake archive")
    (source / "root.rpyc").write_bytes(b"root")
    (prior_extraction / "duplicate.rpyc").write_bytes(b"duplicate")
    destination = tmp_path / "staged"

    copied = copy_game_files(source, destination)

    assert (destination / "archive.rpa").is_file()
    assert (destination / "root.rpyc").is_file()
    assert not (destination / "archive" / "duplicate.rpyc").exists()
    assert len(copied) == 2
