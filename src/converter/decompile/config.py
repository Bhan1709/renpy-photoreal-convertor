"""Decompile configuration generation and loading."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from cli import success, warning

from .constants import CONFIG_PATH
from .extract import ArchiveExtraction
from .scan import scan_source_files

PRESERVED_CONFIG_FIELDS = (
    "compose_modes",
    "special_speakers",
    "player_folder",
    "player_aliases",
    "path_aliases",
    "non_character_folders",
    "game_defaults",
    "output_dir",
    "report_dir",
    "manifest_index_path",
    "manifest_path",
    "declaration_path",
    "report_path",
)


def _load_existing_config(config_path: Path) -> dict[str, Any]:
    """Load an existing config for preserving user-controlled fields."""
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _preserved_fields(config_path: Path) -> dict[str, Any]:
    """Return supported user-controlled fields from an existing config."""
    existing = _load_existing_config(config_path)
    return {
        key: existing[key]
        for key in PRESERVED_CONFIG_FIELDS
        if key in existing
    }


def _archive_metadata(
    entry: ArchiveExtraction | Mapping[str, str],
    game_folder: Path,
) -> dict[str, str]:
    """Normalize one archive metadata entry."""
    if isinstance(entry, ArchiveExtraction):
        return entry.metadata(game_folder)

    archive = str(entry.get("archive", ""))
    extracted_folder = str(entry.get("extracted_folder", ""))
    if not archive or not extracted_folder:
        raise ValueError("archive metadata requires 'archive' and 'extracted_folder'")
    return {"archive": archive, "extracted_folder": extracted_folder}


def _write_json_atomic(config_path: Path, payload: Mapping[str, Any]) -> None:
    """Write JSON through a temporary file and atomically replace the target."""
    config_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{config_path.name}.",
            suffix=".tmp",
            dir=config_path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
        os.replace(temporary_path, config_path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def write_config(
    game_folder: Path,
    source_game: str,
    source_path: Path,
    extracted_archives: Sequence[ArchiveExtraction | Mapping[str, str]] | None = None,
    image_categories: dict | None = None,
    config_path: Path = CONFIG_PATH,
) -> Path:
    r"""Write ``config.json`` for the discovered game files.

    The config records every discovered ``.rpy`` and ``_ren.py`` file, the
    extracted ``.rpa`` archive folders, and the image category naming map.
    User-controlled compose settings already present in the file are preserved
    so a decompile rerun does not silently reset them.
    """
    source_files = scan_source_files(game_folder)
    archive_metadata = [
        _archive_metadata(entry, game_folder)
        for entry in (extracted_archives or [])
        if not isinstance(entry, ArchiveExtraction) or entry.success
    ]

    config: dict[str, Any] = {
        **_preserved_fields(config_path),
        "schema": 1,
        "game_folder": str(game_folder),
        "source_game": source_game,
        "source_path": str(source_path),
        "rpy_files": source_files["rpy"],
        "ren_py_files": source_files["ren_py"],
        "extracted_archives": archive_metadata,
        "image_categories": image_categories or {},
    }

    _write_json_atomic(config_path, config)
    success(f"Created the game configuration: {config_path}")
    return config_path


def load_config(config_path: Path = CONFIG_PATH) -> dict:
    r"""Load the generated ``config.json``.

    Returns the parsed JSON dict, or an empty dict if the file is missing
    or invalid (so legacy callers can degrade gracefully).  New code that
    requires a valid config should use
    :func:`converter.compose.config.load_config` instead.
    """
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError, OSError) as exc:
        warning(f"Could not load the game configuration from {config_path}: {exc}")
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "PRESERVED_CONFIG_FIELDS",
    "load_config",
    "write_config",
]
