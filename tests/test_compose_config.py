"""Tests for the compose-side game configuration contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from converter.compose.config import GameConfig, load_config
from converter.compose.errors import ConfigError


def _payload(game_root: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": 1,
        "game_folder": str(game_root),
        "source_game": "Example",
        "source_path": str(game_root.parent / "runtime"),
        "rpy_files": ["script.rpy"],
        "ren_py_files": [],
        "extracted_archives": [
            {
                "archive": "assets.rpa",
                "extracted_folder": "assets",
            }
        ],
        "image_categories": {
            "classification_method": "folder_based",
            "image_categories": {
                "bg": "assets/bgs",
                "char": "assets/chars",
                "gui": "assets/gui",
                "scene": "assets/xxx",
            },
        },
    }
    payload.update(overrides)
    return payload


def _write_config(tmp_path: Path, payload: dict[str, Any]) -> Path:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(payload), encoding="utf-8")
    return config_path


def test_load_config_derives_archive_root_and_loads_optional_fields(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(
            game_root,
            compose_modes=["cfg"],
            special_speakers=["narrator", "player"],
            player_folder="player",
            player_aliases=["you", "y"],
            path_aliases={"chars/a/a.png": "chars/a/b.png"},
            non_character_folders=["props", "fx"],
            game_defaults={"month": 6, "day": 1},
            output_dir=str(tmp_path / "output"),
        ),
    )

    config = load_config(config_path)

    assert config.archive_dir == (game_root / "assets").resolve()
    assert config.compose_modes == ("cfg",)
    assert config.special_speakers == frozenset({"narrator", "player"})
    assert config.player_folder == "player"
    assert config.player_aliases == ("you", "y")
    assert config.path_aliases == {"chars/a/a.png": "chars/a/b.png"}
    assert config.non_character_folders == frozenset({"props", "fx"})
    assert config.game_defaults == {"month": 6, "day": 1}
    assert config.resolved_paths()["output_dir"] == tmp_path / "output"


def test_load_config_resolves_explicit_manifest_paths(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    report_dir = tmp_path / "custom-reports"
    index_path = report_dir / "custom-index.json"
    config_path = _write_config(
        tmp_path,
        _payload(
            game_root,
            report_dir=str(report_dir),
            manifest_index_path=str(index_path),
        ),
    )

    config = load_config(config_path)

    paths = config.resolved_paths()
    assert paths["report_dir"] == report_dir
    assert paths["manifest_index_path"] == index_path


def test_legacy_manifest_path_remains_a_report_directory_anchor(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    legacy_manifest = tmp_path / "custom-reports" / "manifest.json"
    config = GameConfig(
        name="Example",
        game_root=game_root,
        archive_dir=game_root / "archive",
        manifest_path=legacy_manifest,
    )

    paths = config.resolved_paths()

    assert paths["report_dir"] == legacy_manifest.parent
    assert paths["manifest_index_path"] == legacy_manifest.parent / "manifest_index.json"
    assert paths["manifest_path"] == legacy_manifest


def test_load_config_uses_built_in_defaults_when_optional_fields_are_absent(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(tmp_path, _payload(game_root))

    config = load_config(config_path)

    assert config.path_aliases
    assert config.game_defaults == {"month": 6, "day": 1}
    assert config.non_character_folders


def test_load_config_fills_missing_categories_with_defaults(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(
            game_root,
            image_categories={
                "classification_method": "folder_based",
                "image_categories": {"bg": "assets/bgs", "char": "assets/chars"},
            },
        ),
    )

    config = load_config(config_path)

    assert config.categories == {
        "bg": "bgs",
        "char": "chars",
        "scene": "xxx",
        "gui": "gui",
    }


def test_load_config_preserves_explicitly_empty_special_speakers(
    tmp_path: Path,
) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(game_root, special_speakers=[]),
    )

    config = load_config(config_path)

    assert config.special_speakers == frozenset()


def test_load_config_rejects_unsafe_path_alias(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(
            game_root,
            path_aliases={"chars/a/a.png": "../outside.png"},
        ),
    )

    with pytest.raises(ConfigError, match="path_aliases"):
        load_config(config_path)


def test_load_config_rejects_windows_drive_category_folder(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(
            game_root,
            image_categories={
                "classification_method": "folder_based",
                "image_categories": {
                    "bg": "C:outside",
                    "char": "assets/chars",
                    "gui": "assets/gui",
                    "scene": "assets/xxx",
                },
            },
        ),
    )

    with pytest.raises(ConfigError, match="category"):
        load_config(config_path)


def test_load_config_rejects_mode_based_image_lists(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(
            game_root,
            image_categories={
                "classification_method": "mode_based",
                "image_categories": {
                    "char": ["chars/alice.png"],
                    "scene": ["scenes/room.png"],
                },
            },
        ),
    )

    with pytest.raises(ConfigError, match="folder"):
        load_config(config_path)


def test_load_config_rejects_unsafe_category_folder(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = _write_config(
        tmp_path,
        _payload(
            game_root,
            image_categories={
                "classification_method": "folder_based",
                "image_categories": {
                    "bg": "../outside",
                    "char": "assets/chars",
                    "gui": "assets/gui",
                    "scene": "assets/xxx",
                },
            },
        ),
    )

    with pytest.raises(ConfigError, match="category"):
        load_config(config_path)
