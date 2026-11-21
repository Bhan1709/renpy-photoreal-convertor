"""Tests for native workspace asset mirroring."""

from __future__ import annotations

from pathlib import Path

import pytest

from converter.compose.errors import NativeExecutionError
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


def test_workspace_asset_copy_does_not_write_through_to_live_file(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    assets = game_root / "assets"
    assets.mkdir(parents=True)
    live_asset = assets / "room.txt"
    live_asset.write_text("live", encoding="utf-8")

    workspace = create_workspace(
        game_root=game_root,
        script_files=[],
        engine_config={},
        workspace_parent=tmp_path,
        archive_dirs=[assets.resolve()],
    )

    try:
        staged_asset = workspace.game_dir / "room.txt"
        staged_asset.write_text("workspace", encoding="utf-8")
        assert live_asset.read_text(encoding="utf-8") == "live"
    finally:
        workspace.cleanup()


def test_workspace_rejects_a_linked_archive_entry(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    assets = game_root / "assets"
    assets.mkdir(parents=True)
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    try:
        (assets / "linked.txt").symlink_to(outside)
    except OSError:
        pytest.skip("symbolic-link creation is not available on this platform")

    with pytest.raises(NativeExecutionError, match="symbolic link"):
        create_workspace(
            game_root=game_root,
            script_files=[],
            engine_config={},
            workspace_parent=tmp_path,
            archive_dirs=[assets.resolve()],
        )


def test_workspace_setup_failure_removes_the_disposable_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    before = {path.name for path in tmp_path.iterdir()}

    def fail_runtime_copy(_target: Path) -> None:
        raise NativeExecutionError("simulated setup failure")

    monkeypatch.setattr(
        "converter.compose.native.workspace._copy_runtime_script",
        fail_runtime_copy,
    )

    with pytest.raises(NativeExecutionError, match="simulated setup failure"):
        create_workspace(
            game_root=game_root,
            script_files=[],
            engine_config={},
            workspace_parent=tmp_path,
        )

    assert {path.name for path in tmp_path.iterdir()} == before
