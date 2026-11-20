"""Tests for source-game discovery and staged-output exclusion."""

from __future__ import annotations

from pathlib import Path

from converter.decompile.discovery import find_game_folder


def test_find_game_folder_excludes_descendants_of_staged_destination(
    tmp_path: Path,
) -> None:
    staged = tmp_path / "staged"
    nested_source = staged / "Nested" / "game"
    nested_source.mkdir(parents=True)

    result = find_game_folder(tmp_path, exclude=staged)

    assert result is None


def test_find_game_folder_excludes_requested_staged_destination(tmp_path: Path) -> None:
    staged = tmp_path / "game"
    source = tmp_path / "ZSource" / "game"
    staged.mkdir(parents=True)
    source.mkdir(parents=True)

    result = find_game_folder(tmp_path, exclude=staged)

    assert result == source
