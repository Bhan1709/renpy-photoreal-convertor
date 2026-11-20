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

from cli import (
    Choice,
    counted,
    error,
    info,
    kv,
    print_summary,
    rule,
    select,
    success,
    warning,
)

from .constants import GAME_FOLDER, RENPY_GAMES
from .discovery import find_game_folder
from .flow import process_game
from .restore import restore_decompile

app = typer.Typer(
    help=(
        "Decompile a Ren'Py game: extract archives, restore readable scripts, "
        "and create the game configuration."
    ),
    invoke_without_command=True,
)


@app.callback()
def _default(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the decompile steps without changing any files.",
    ),
) -> None:
    """Run the interactive menu when no subcommand is given."""
    state = ctx.ensure_object(dict)
    state["dry_run"] = dry_run
    if ctx.invoked_subcommand is not None:
        return
    code = _interactive(dry_run=dry_run)
    raise typer.Exit(code)


def _effective_dry_run(ctx: typer.Context, command_dry_run: bool) -> bool:
    """Combine root-level and subcommand dry-run options."""
    state = ctx.ensure_object(dict)
    return bool(command_dry_run or state.get("dry_run", False))


def _run_pipeline(dry_run: bool = False) -> int:
    """Run the decompile pipeline via process_game and return an exit code."""
    try:
        result = process_game(dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        error(f"Decompile failed: {exc}")
        return 1

    if not result.get("success"):
        error("Decompile did not complete successfully.")
        return 1

    print_summary(
        "Decompile summary",
        [
            ("Game folder", str(result.get("game_folder"))),
            ("Source game", str(result.get("source_game"))),
            ("Files copied", str(result.get("copied", 0))),
            (
                "Archives extracted",
                str(len(result.get("extracted_archives", []))),
            ),
            (".rpyc decompiled", str(result.get("decompiled", 0))),
            ("Config written", str(result.get("config"))),
        ],
    )
    compose_exit_code = result.get("compose_exit_code")
    return int(compose_exit_code) if isinstance(compose_exit_code, int) else 0


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
            "Dry run: generated .rpy files and extracted archive directories "
            "would be removed."
        )
        return 0

    results = restore_decompile(target)
    if not results:
        info("No generated files needed cleaning.")
        return 0

    removed = sum(1 for v in results.values() if v == "removed")
    skipped = sum(1 for v in results.values() if v.startswith("skipped"))
    success(f"Removed {counted(removed, 'generated item')}.")
    if skipped:
        warning(f"Skipped {counted(skipped, 'item')}.")
    return 0


@app.command()
def clean(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Show what would be removed without doing it"
    ),
) -> None:
    """Remove generated .rpy files and extracted archive folders."""
    raise typer.Exit(_clean_decompile(dry_run=_effective_dry_run(ctx, dry_run)))


@app.command()
def decompile(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the decompile steps without changing any files.",
    ),
) -> None:
    """Extract archives, restore readable scripts, and write config.json."""
    raise typer.Exit(_run_pipeline(dry_run=_effective_dry_run(ctx, dry_run)))


@app.command()
def clean_decompile(
    ctx: typer.Context,
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the decompile steps without changing any files.",
    ),
) -> None:
    """Remove previous decompile output, then decompile the game."""
    effective_dry_run = _effective_dry_run(ctx, dry_run)
    clean_code = _clean_decompile(dry_run=effective_dry_run)
    if clean_code != 0 and not effective_dry_run:
        raise typer.Exit(clean_code)
    raise typer.Exit(_run_pipeline(dry_run=effective_dry_run))


def _interactive(dry_run: bool = False) -> int:
    """Interactive decompile menu: clean, decompile, or clean + decompile."""
    rule("Decompile")

    choice = select(
        "Choose an action",
        choices=[
            Choice(
                title="Clean previous decompile output",
                value="clean",
                shortcut_key="c",
            ),
            Choice(
                title="Decompile the game",
                value="decompile",
                shortcut_key="d",
            ),
            Choice(
                title="Clean output, then decompile",
                value="clean_decompile",
                shortcut_key="a",
            ),
            Choice(title="Exit", value="quit", shortcut_key="x"),
        ],
        default="decompile",
        use_shortcuts=True,
    )

    if choice is None:
        info("Cancelled. The decompile action was not started.")
        return 0

    if choice == "clean":
        return _clean_decompile(dry_run=dry_run)

    if choice == "decompile":
        return _run_pipeline(dry_run=dry_run)

    if choice == "clean_decompile":
        clean_code = _clean_decompile(dry_run=dry_run)
        if clean_code != 0 and not dry_run:
            return clean_code
        return _run_pipeline(dry_run=dry_run)

    # Quit
    return 0


if __name__ == "__main__":
    sys.exit(app())
