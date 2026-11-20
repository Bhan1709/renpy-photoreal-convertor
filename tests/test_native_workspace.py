"""Tests for native workspace asset mirroring."""

from __future__ import annotations

from pathlib import Path

from converter.compose.native.workspace import create_workspace


def test_workspace_mirrors_configured_archive_roots(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    assets = game_root / "assets"
    extras = game_root / "extras"
    (assets / "bgs").mkdir(parents=True)
    (extras / "gui").mkdir(parents=True)
    (assets / "bgs" / "room.txt").write_text("room", encoding="utf-8")
    (extras / "gui" / "button.txt").write_text("button", encoding="utf-8")
    (game_root / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")

    workspace = create_workspace(
        game_root=game_root,
        script_files=["script.rpy"],
        engine_config={},
        result_path=tmp_path / "result.json",
        workspace_parent=tmp_path,
        archive_dirs=[assets.resolve(), extras.resolve()],
    )

    try:
        assert (workspace.game_dir / "bgs" / "room.txt").is_file()
        assert (workspace.game_dir / "gui" / "button.txt").is_file()
        assert (workspace.game_dir / "script.rpy").is_file()
    finally:
        workspace.cleanup()
