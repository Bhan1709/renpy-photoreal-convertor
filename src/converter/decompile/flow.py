"""End-to-end generalized decompile flow."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from rich.console import Group  # type: ignore[attr-defined]
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from cli import console, error, info, kv, rule, select, success

from .config import write_config
from .constants import CONFIG_PATH, GAME_FOLDER, RENPY_GAMES, ROOT
from .decompile_files import decompile_rpyc_files
from .discovery import find_game_folder, set_game_folder_rel_path
from .extract import extract_all_rpa
from .scan import scan_image_categories
from .staging import copy_game_files


def process_game(
    renpy_games: Path = RENPY_GAMES,
    dest: Path = GAME_FOLDER,
    config_path: Path = CONFIG_PATH,
    *,
    dry_run: bool = False,
) -> int | dict[str, Any]:
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
        error(f"No \\game folder found under {renpy_games}")
        return summary

    game_dir = game_folder.parent
    summary["game_folder"] = str(game_folder)
    summary["source_game"] = game_dir.name
    summary["source_path"] = str(game_dir)

    rule(f"Game folder: {game_folder}")
    kv("Source game", game_dir.name)

    if dry_run:
        info(f"[dry-run] would copy relevant files into {dest}")
    else:
        copied = copy_game_files(game_folder, dest)
        summary["copied"] = len(copied)
        success(f"Copied {len(copied)} file(s) into {dest}")
        game_folder = dest  # operate on the consolidated copy from here on

    if dry_run:
        info("[dry-run] would extract .rpa archives and decompile .rpyc files")
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

    grid = Table.grid(expand=False, padding=(0, 1))
    grid.add_column(style="dim", width=3)
    grid.add_column()
    grid.add_row(
        Text.from_markup("[dim]1.[/dim]"),
        Text.from_markup(f"Edit exclusion patterns in {CONFIG_PATH} if needed."),
    )
    grid.add_row(
        Text.from_markup("[dim]2.[/dim]"),
        Text.from_markup("Sync exclusions:"),
    )
    grid.add_row(
        Text(""),
        Text.from_markup("     [cyan]mise run exclusions-sync[/cyan]"),
    )
    grid.add_row(
        Text(""),
        Text.from_markup("[dim]   or[/dim]"),
    )
    grid.add_row(
        Text(""),
        Text.from_markup(
            "     [cyan]python src/converter/decompile/sync_exclusions_cli.py[/cyan]"
        ),
    )

    console.print()
    console.print(
        Panel(
            Group(
                Text(
                    "Manual alternative: adjust exclusions before composing",
                    style="bold blue",
                ),
                Text(""),
                grid,
                Text(""),
                Text(
                    "Copy-paste this command to run compose later:", style="bold blue"
                ),
                Text.from_markup("  [cyan]mise run compose-clean[/cyan]"),
                Text.from_markup("[dim]or[/dim]"),
                Text.from_markup(
                    "  [cyan]python -m converter.pipeline --compose-only --clean-output[/cyan]"
                ),
            ),
            title="Decompile flow complete",
            border_style="magenta",
            expand=False,
        )
    )
    console.print()

    choice = select(
        "What would you like to do next?",
        choices=[
            "Run compose-clean now (build photoreal composites)",
            "Run compose now (default options)",
            "Quit",
        ],
        default="Run compose-clean now (build photoreal composites)",
    )

    if choice is None:
        info("Aborted.")
        return summary

    if choice.startswith("Run compose-clean now"):
        cmd = [
            sys.executable,
            "-m",
            "converter.pipeline",
            "--compose-only",
            "--clean-output",
        ]
        info(f"Running: {' '.join(cmd)}")
        return subprocess.run(cmd, check=False).returncode

    if choice.startswith("Run compose now"):
        cmd = [sys.executable, "-m", "converter.pipeline", "--compose-only"]
        info(f"Running: {' '.join(cmd)}")
        return subprocess.run(cmd, check=False).returncode

    # Quit
    return 0


def process_all_games() -> None:
    r"""Backward-compatible entry point: process the first game found."""
    process_game()
