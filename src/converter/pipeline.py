"""End-to-end converter pipeline: decompile then compose Ren'Py games.

Links :mod:`converter.decompile` (decompiles ``.rpyc -> .rpy`` and extracts
``.rpa`` archives under ``src/renpy_game``) with :mod:`converter.compose`
(builds photoreal composites from the resulting ``.rpy`` scripts and adopts
them back into the live scripts) and :mod:`converter.restore` (reverts
modified ``.rpy`` files from the archive).

The default flow is interactive::

    1. Decompile every ``.rpyc`` and extract every ``.rpa`` under the game folder.
    2. Prompt the user for exclusion patterns to persist in
       ``src/converter/config.json``.
    3. Run compose (manifest + PNGs).  The apply step is deliberately deferred
       so the user can verify the compose output first.

All modes are config-driven via ``src/converter/config.json`` (produced by
decompile); none require ``--game``.

Run from the repo root::

    uv run python -m converter.pipeline                                # default game
    uv run python -m converter.pipeline --compose-only                 # compose only (skip decompile)
    uv run python -m converter.pipeline --apply-only                   # apply only (needs manifest)
    uv run python -m converter.pipeline --dry-run                      # compose without writing
"""

from __future__ import annotations

import sys
from pathlib import Path

import typer

from cli import create_app, error, rule, step

from .compose.cli import (
    apply_game,
    compose_game,
    print_review_instructions,
)
from .compose.config import load_config
from .decompile.constants import CONFIG_PATH
from .decompile.decompile_files import decompile_rpyc_files
from .decompile.discovery import find_game_folders
from .decompile.exclusions import prompt_exclusion_patterns, sync_exclusions
from .decompile.extract import extract_rpa_file

app = create_app("Decompile a Ren'Py game, then compose photoreal composites.")


# --------------------------------------------------------------------------- #
# Internal helpers (unchanged logic, rich output)                             #
# --------------------------------------------------------------------------- #


def _decompile_and_extract(target_folder: Path) -> None:
    """Decompile every ``.rpyc`` and extract every ``.rpa`` under a game folder."""
    decompile_rpyc_files(target_folder)
    for rpa_file in sorted(target_folder.rglob("*.rpa")):
        extract_rpa_file(rpa_file)


def run_pipeline(
    dry_run: bool = False,
    compose_only: bool = False,
    apply_only: bool = False,
    clean_output: bool = False,
) -> int:
    """Run the decompile -> compose -> apply pipeline.

    The default flow always decompiles first, then prompts the user for
    exclusion patterns, then runs compose.  The apply step is deliberately not
    run by default so the user can verify compose output.

    All modes are config-driven via ``src/converter/config.json``; no
    ``--game`` lookup is needed.

    Restore-compose is handled by the compose CLI (``python -m converter.compose restore``),
    not by the pipeline.

    Args:
        dry_run: Compose without writing; skips the apply pass.
        compose_only: Compose + write manifest, then stop (skip apply).
            Assumes the game has already been decompiled.
        apply_only: Run the apply pass only (requires an existing manifest).
        clean_output: Remove prior generated PNGs and manifest before
            composing (only applies to compose modes).

    Returns:
        0 on success, 1 if no matching games were found.
    """
    # --- Compose-only / apply-only modes (config-driven, no decompile) ---
    if compose_only or apply_only:
        sync_exclusions(CONFIG_PATH)
        config = load_config(CONFIG_PATH)
        rule(f"Pipeline: {config.name}")

        if compose_only:
            prompt_exclusion_patterns(CONFIG_PATH)
            sync_exclusions(CONFIG_PATH)
            config = load_config(CONFIG_PATH)
            compose_game(config, dry_run=dry_run, clean=clean_output)
            rule("Compose complete (apply step deferred)")
            print_review_instructions(config, CONFIG_PATH)
            return 0

        if apply_only:
            return apply_game(config)

    # --- Default flow: decompile -> exclusions prompt -> compose (no apply) ---
    # Auto-discover the first game folder under src/renpy_game (no --game needed).
    folders = find_game_folders()
    if not folders:
        error("No game folders found under src/renpy_game.")
        return 1

    game_dir, _rel_path, target_folder = folders[0]
    name = game_dir.name
    rule(f"Pipeline: {name}")

    # --- Stage 1: decompile (.rpyc -> .rpy) + extract (.rpa -> archive/) ---
    step(1, f"Decompile .rpyc + extract .rpa under {target_folder}")
    _decompile_and_extract(target_folder)

    # --- Stage 2: prompt user for exclusion patterns ---
    step(2, "Prompt for exclusion patterns")
    prompt_exclusion_patterns(CONFIG_PATH)

    # --- Stage 3: sync exclusions + load decompile config ---
    step(3, "Sync exclusions + load config")
    sync_exclusions(CONFIG_PATH)
    config = load_config(CONFIG_PATH)

    # --- Stage 4: compose (manifest + PNGs) only ---
    # Apply is intentionally deferred so the user can verify compose output.
    step(4, "Compose manifest + PNGs (apply deferred)")
    compose_game(config, dry_run=dry_run, clean=clean_output)
    rule("Compose complete (apply step deferred)")
    print_review_instructions(config, CONFIG_PATH)

    rule("Pipeline complete")
    return 0


# --------------------------------------------------------------------------- #
# Typer command                                                               #
# --------------------------------------------------------------------------- #


@app.command()
def main(
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Compose without writing; skips the apply pass."
    ),
    clean_output: bool = typer.Option(
        False,
        "--clean-output",
        help="Remove prior generated PNGs and manifest before composing.",
    ),
    compose_only: bool = typer.Option(
        False,
        "--compose-only",
        help="Compose + write manifest, then stop (skip apply). Assumes decompiled.",
    ),
    apply_only: bool = typer.Option(
        False,
        "--apply-only",
        help="Run the apply pass only (requires an existing manifest).",
    ),
) -> int:
    """Run the decompile -> compose -> apply pipeline."""
    return run_pipeline(
        dry_run=dry_run,
        compose_only=compose_only,
        apply_only=apply_only,
        clean_output=clean_output,
    )


if __name__ == "__main__":
    sys.exit(app())
