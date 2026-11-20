"""Config file generation and loading."""

from __future__ import annotations

import json
from pathlib import Path

from cli import success, warning

from .constants import CONFIG_PATH
from .scan import scan_source_files


def write_config(
    game_folder: Path,
    source_game: str,
    source_path: Path,
    extracted_archives: list[dict[str, str]] | None = None,
    image_categories: dict | None = None,
    config_path: Path = CONFIG_PATH,
) -> Path:
    r"""Write ``config.json`` with the discovered game files.

    The config records every discovered ``.rpy`` and ``_ren.py`` file, the
    extracted ``.rpa`` archive folders, and the image category naming map.
    Compose scans the live game folder directly, so no exclusion patterns are
    generated; remove unwanted scripts from the game folder itself.
    """
    source_files = scan_source_files(game_folder)

    config = {
        "schema": 1,
        "game_folder": str(game_folder),
        "source_game": source_game,
        "source_path": str(source_path),
        "rpy_files": source_files["rpy"],
        "ren_py_files": source_files["ren_py"],
        "extracted_archives": extracted_archives or [],
        "image_categories": image_categories or {},
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    success(f"Created the game configuration: {config_path}")
    return config_path


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    r"""Load the generated ``config.json``.

    Returns the parsed JSON dict, or an empty dict if the file is missing
    or invalid (so callers can degrade gracefully).
    """
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        warning(f"Could not load the game configuration from {config_path}: {exc}")
        return {}
