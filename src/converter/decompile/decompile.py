"""Decompile entry point for ``converter.decompile`` (typer + rich + questionary).

Single entry point for the decompile flow with interactive options::

    python -m converter.decompile              # Interactive menu
    python -m converter.decompile decompile    # Run decompile only
    python -m converter.decompile clean        # Clean previous decompile only
    python -m converter.decompile clean-decompile  # Clean then decompile

Run from the repository root.
"""

from __future__ import annotations

import sys

import typer

from cli import error, info, kv, rule, select, success, warning

from .constants import GAME_FOLDER, RENPY_GAMES
from .discovery import find_game_folder
from .flow import process_game
from .restore import restore_decompile

app = typer.Typer(
    help="Decompile a Ren'Py game: extract .rpa archives, decompile .rpyc files, and write config.json.",
    invoke_without_command=True,
)


@app.callback()
def _default(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without doing it"
    ),
) -> None:
    """Run the interactive menu when no subcommand is given."""
    if ctx.invoked_subcommand is not None:
        return
    code = _interactive(dry_run=dry_run)
    raise typer.Exit(code)


def _run_pipeline(dry_run: bool = False) -> int:
    """Run the decompile pipeline via process_game and return an exit code."""
    try:
        result = process_game(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        error(f"Decompile failed: {exc}")
        return 1

    if isinstance(result, dict):
        info(f"Game folder: {result.get('game_folder')}")
        kv("Source game", str(result.get("source_game")))
        kv("Files copied", str(result.get("copied", 0)))
        kv("Archives extracted", str(len(result.get("extracted_archives", []))))
        kv(".rpyc decompiled", str(result.get("decompiled", 0)))
        kv("Config written", str(result.get("config")))
        return 0
    return 1


def _clean_decompile(dry_run: bool = False) -> int:
    """Remove previous decompile artifacts and return an exit code."""
    game_folder = find_game_folder(RENPY_GAMES)
    if game_folder is None:
        error(f"No \\game folder found under {RENPY_GAMES}")
        return 1

    target = GAME_FOLDER
    rule("Clean decompile artifacts")
    kv("Target folder", str(target))

    if dry_run:
        info(
            "[dry-run] would remove generated .rpy files and extracted archive directories"
        )
        return 0

    results = restore_decompile(target)
    if not results:
        info("(nothing to clean)")
        return 0

    removed = sum(1 for v in results.values() if v == "removed")
    skipped = sum(1 for v in results.values() if v.startswith("skipped"))
    success(f"Cleaned {removed} file(s)/directory(ies)")
    if skipped:
        warning(f"Skipped {skipped} item(s)")
    return 0


@app.command()
def clean(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be removed without doing it"
    ),
) -> int:
    """Clean previous decompile artifacts (.rpy files and extracted archive dirs)."""
    return _clean_decompile(dry_run=dry_run)


@app.command()
def decompile(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without doing it"
    ),
) -> int:
    """Run the decompile pipeline (find game, copy, extract, decompile, write config)."""
    return _run_pipeline(dry_run=dry_run)


@app.command()
def clean_decompile(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be done without doing it"
    ),
) -> int:
    """Clean previous decompile artifacts, then run the decompile pipeline."""
    clean_code = _clean_decompile(dry_run=dry_run)
    if clean_code != 0 and not dry_run:
        return clean_code
    return _run_pipeline(dry_run=dry_run)


def _interactive(dry_run: bool = False) -> int:
    """Interactive decompile menu: clean, decompile, or clean + decompile."""
    rule("Decompile")

    choice = select(
        "What would you like to do?",
        choices=[
            "Clean previous decompile",
            "Decompile now",
            "Clean then decompile",
            "Quit",
        ],
        default="Decompile now",
    )

    if choice is None:
        info("Aborted.")
        return 0

    if choice == "Clean previous decompile":
        return _clean_decompile(dry_run=dry_run)

    if choice == "Decompile now":
        return _run_pipeline(dry_run=dry_run)

    if choice == "Clean then decompile":
        clean_code = _clean_decompile(dry_run=dry_run)
        if clean_code != 0 and not dry_run:
            return clean_code
        return _run_pipeline(dry_run=dry_run)

    # Quit
    return 0


if __name__ == "__main__":
    sys.exit(app())
