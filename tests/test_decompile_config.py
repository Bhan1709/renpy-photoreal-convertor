"""Tests for the decompile-generated game configuration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from converter.decompile.config import load_config, write_config
from converter.decompile.extract import ArchiveExtraction


def test_write_config_preserves_user_controlled_fields(tmp_path: Path) -> None:
    game_folder = tmp_path / "game"
    game_folder.mkdir()
    (game_folder / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")

    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "schema": 1,
                "game_folder": "old/game",
                "compose_modes": ["cfg"],
                "special_speakers": ["narrator", "player"],
                "player_folder": "player",
                "player_aliases": ["you", "y"],
                "path_aliases": {"chars/a/a.png": "chars/a/b.png"},
                "non_character_folders": ["props"],
                "game_defaults": {"month": 6},
                "output_dir": "custom/composites",
                "report_dir": "custom/reports",
                "manifest_index_path": "custom/reports/manifest_index.json",
            }
        ),
        encoding="utf-8",
    )

    extraction = ArchiveExtraction(
        archive=game_folder / "archive.rpa",
        extracted_folder=game_folder / "archive",
        files_extracted=1,
    )
    categories = {
        "classification_method": "folder_based",
        "image_categories": {
            "bg": "archive/bgs",
            "char": "archive/chars",
            "gui": "archive/gui",
            "scene": "archive/xxx",
        },
    }

    written = write_config(
        game_folder,
        source_game="Example",
        source_path=tmp_path / "runtime",
        extracted_archives=[extraction],
        image_categories=categories,
        config_path=config_path,
    )

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["game_folder"] == str(game_folder)
    assert payload["compose_modes"] == ["cfg"]
    assert payload["special_speakers"] == ["narrator", "player"]
    assert payload["player_folder"] == "player"
    assert payload["player_aliases"] == ["you", "y"]
    assert payload["path_aliases"] == {"chars/a/a.png": "chars/a/b.png"}
    assert payload["non_character_folders"] == ["props"]
    assert payload["game_defaults"] == {"month": 6}
    assert payload["output_dir"] == "custom/composites"
    assert payload["report_dir"] == "custom/reports"
    assert payload["manifest_index_path"] == "custom/reports/manifest_index.json"
    assert payload["extracted_archives"] == [
        {
            "archive": "archive.rpa",
            "extracted_folder": "archive",
        }
    ]
    assert not list(config_path.parent.glob("*.tmp"))


def test_load_config_returns_empty_dict_for_non_object_json(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text("[]", encoding="utf-8")

    assert load_config(config_path) == {}


def test_write_config_keeps_existing_file_when_serialisation_fails(
    tmp_path: Path,
) -> None:
    game_folder = tmp_path / "game"
    game_folder.mkdir()
    config_path = tmp_path / "config.json"
    original = '{"existing": true}\n'
    config_path.write_text(original, encoding="utf-8")

    with pytest.raises(TypeError):
        write_config(
            game_folder,
            source_game="Example",
            source_path=tmp_path / "runtime",
            image_categories={"invalid": {1, 2}},
            config_path=config_path,
        )

    assert config_path.read_text(encoding="utf-8") == original
    assert not list(config_path.parent.glob("*.tmp"))
