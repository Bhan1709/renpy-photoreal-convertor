"""End-to-end generalized decompile flow."""

from __future__ import annotations

from pathlib import Path

from .constants import CONFIG_PATH, GAME_FOLDER, RENPY_GAMES, ROOT
from .discovery import find_game_folder, set_game_folder_rel_path
from .decompile_files import decompile_rpyc_files
from .extract import extract_all_rpa
from .config import write_config
from .scan import scan_image_categories
from .staging import copy_game_files


def process_game(
    renpy_games: Path = RENPY_GAMES,
    dest: Path = GAME_FOLDER,
    config_path: Path = CONFIG_PATH,
    *,
    dry_run: bool = False,
) -> dict:
    r"""Run the full generalized decompile flow for one game.

    Steps:

      1. Find the first ``\game`` folder under ``renpy_games``.
      2. Copy relevant files into ``dest`` (``src/renpy_game/game``).
      3. Extract every ``.rpa`` alongside itself.
      4. Decompile every ``.rpyc`` into a sibling ``.rpy``.
      5. Write ``config.json`` with matching patterns + exclusion parameter.

    Returns a summary dict.
    """
    summary: dict = {
        "game_folder": None,
        "source_game": None,
        "source_path": None,
        "copied": 0,
        "extracted_archives": [],
        "decompiled": 0,
        "config": None,
        "dry_run": dry_run,
    }

    game_folder = find_game_folder(renpy_games)
    if game_folder is None:
        print(f"No \\game folder found under {renpy_games}")
        return summary

    game_dir = game_folder.parent
    summary["game_folder"] = str(game_folder)
    summary["source_game"] = game_dir.name
    summary["source_path"] = str(game_dir)

    print(f"\n=== Game folder: {game_folder} ===")
    print(f"Source game: {game_dir.name}")

    if dry_run:
        print(f"[dry-run] would copy relevant files into {dest}")
    else:
        copied = copy_game_files(game_folder, dest)
        summary["copied"] = len(copied)
        print(f"  [OK] Copied {len(copied)} file(s) into {dest}")
        game_folder = dest  # operate on the consolidated copy from here on

    if dry_run:
        print("[dry-run] would extract .rpa archives and decompile .rpyc files")
    else:
        extracted = extract_all_rpa(game_folder)
        summary["extracted_archives"] = extracted
        summary["decompiled"] = decompile_rpyc_files(game_folder)
        image_categories = scan_image_categories(game_folder)
        summary["image_categories"] = image_categories
        config_out = write_config(
            game_folder,
            source_game=game_dir.name,
            source_path=game_dir,
            extracted_archives=extracted,
            image_categories=image_categories,
            config_path=config_path,
        )
        summary["config"] = str(config_out)
        try:
            set_game_folder_rel_path(game_folder.relative_to(ROOT))
        except ValueError:
            set_game_folder_rel_path(game_folder)

    print("\n=== Decompile flow complete ===")
    return summary


def process_all_games() -> None:
    r"""Backward-compatible entry point: process the first game found."""
    process_game()
