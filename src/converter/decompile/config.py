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
    r"""Write ``config.json`` with matching patterns and an exclusion parameter.

    The config records every discovered ``.rpy`` and ``_ren.py`` file as a
    matching pattern, the extracted ``.rpa`` archive folders, the image
    category naming map, plus an ``exclusion`` block the user can populate
    with glob/substring patterns to exclude from downstream processing.
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
        "exclusion": {
            "script": {
                "patterns": [],
                "note": "Patterns matched against rpy_files/ren_py_files entries; matching entries are removed from the scan list",
            },
            "image": {
                "patterns": [],
                "note": "Patterns matched against image source paths; layers whose source starts with a matching folder are excluded from compose scanning",
            },
        },
    }

    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    success(f"Wrote config: {config_path}")
    return config_path


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    r"""Load the generated ``config.json``.

    Returns the parsed JSON dict, or an empty dict if the file is missing
    or invalid (so callers can degrade gracefully).

    Expects ``exclusion`` to use the dual
    ``exclusion.script.patterns`` / ``exclusion.image.patterns`` format
    written by :func:`write_config`.
    """
    try:
        return json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        warning(f"Could not load config {config_path}: {exc}")
        return {}
