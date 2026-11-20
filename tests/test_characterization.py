"""Baseline characterization tests for the Phase 0 test harness.

These tests intentionally describe behavior that exists before the Phase 1
orchestration and configuration hardening work. They remain useful after that
work as regression coverage for the original public behavior.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from converter import pipeline
from converter.compose.config import load_config
from converter.decompile import flow
from converter.decompile.config import write_config
from converter.decompile.discovery import find_game_folder
from converter.decompile.staging import copy_game_files


def test_discovery_finds_a_game_folder(tmp_path: Path) -> None:
    game_folder = tmp_path / "Example" / "game"
    game_folder.mkdir(parents=True)

    result = find_game_folder(tmp_path)

    assert result == game_folder


def test_staging_copies_supported_game_files(tmp_path: Path) -> None:
    source = tmp_path / "source" / "game"
    source.mkdir(parents=True)
    (source / "script.rpyc").write_bytes(b"compiled")
    (source / "archive.rpa").write_bytes(b"archive")
    (source / "notes.txt").write_text("ignored", encoding="utf-8")
    destination = tmp_path / "staged"

    copied = copy_game_files(source, destination)

    assert (destination / "script.rpyc").is_file()
    assert (destination / "archive.rpa").is_file()
    assert not (destination / "notes.txt").exists()
    assert len(copied) == 2


def test_write_config_records_discovered_scripts(tmp_path: Path) -> None:
    game_folder = tmp_path / "game"
    game_folder.mkdir()
    (game_folder / "script.rpy").write_text("label start:\n    pass\n", encoding="utf-8")
    config_path = tmp_path / "config.json"

    written = write_config(
        game_folder,
        source_game="Example",
        source_path=tmp_path / "runtime",
        image_categories={
            "classification_method": "folder_based",
            "image_categories": {
                "bg": "archive/bgs",
                "char": "archive/chars",
                "gui": "archive/gui",
                "scene": "archive/xxx",
            },
        },
        config_path=config_path,
    )

    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload["game_folder"] == str(game_folder)
    assert payload["rpy_files"] == ["script.rpy"]
    assert payload["image_categories"]["image_categories"]["char"] == "archive/chars"


def test_compose_config_loads_folder_based_categories(tmp_path: Path) -> None:
    game_root = tmp_path / "game"
    game_root.mkdir()
    config_path = tmp_path / "config.json"
    payload: dict[str, Any] = {
        "game_folder": str(game_root),
        "source_game": "Example",
        "image_categories": {
            "classification_method": "folder_based",
            "image_categories": {
                "bg": "archive/bgs",
                "char": "archive/chars",
                "gui": "archive/gui",
                "scene": "archive/xxx",
            },
        },
    }
    config_path.write_text(json.dumps(payload), encoding="utf-8")

    config = load_config(config_path)

    assert config.game_root == game_root.resolve()
    assert config.categories == {
        "bg": "bgs",
        "char": "chars",
        "scene": "xxx",
        "gui": "gui",
    }


def test_process_game_reports_a_missing_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        flow,
        "find_game_folder",
        lambda _root, **_kwargs: None,
    )

    result = flow.process_game(renpy_games=tmp_path)

    assert isinstance(result, dict)
    assert result["game_folder"] is None
    assert result["config"] is None


def test_compose_only_pipeline_uses_the_existing_config(monkeypatch) -> None:
    expected_config = SimpleNamespace(name="Example")
    composed: list[Any] = []

    monkeypatch.setattr(pipeline, "load_config", lambda _path: expected_config)
    monkeypatch.setattr(
        pipeline,
        "compose_game",
        lambda config, **_kwargs: composed.append(config),
    )
    monkeypatch.setattr(
        pipeline,
        "print_review_instructions",
        lambda *_args, **_kwargs: None,
    )

    result = pipeline.run_pipeline(compose_only=True)

    assert result == 0
    assert composed == [expected_config]
