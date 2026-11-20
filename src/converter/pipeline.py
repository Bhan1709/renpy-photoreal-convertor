"""Unified decompile-to-compose pipeline for Ren'Py games.

The pipeline uses the canonical staged decompile service.  The source game is
never modified: relevant files are copied into ``src/renpy_game/game``, the
staged copy is extracted and decompiled, and compose consumes the config
written by that same run.
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
from .compose.errors import ConfigError
from .decompile.constants import CONFIG_PATH, GAME_FOLDER, RENPY_GAMES
from .decompile.flow import prepare_game

app = create_app(
    "Decompile a Ren'Py game, then create photoreal composites for review."
)


def _load_config(config_path: Path):
    """Load the compose config, reporting failures to the user."""
    try:
        return load_config(config_path)
    except (OSError, ConfigError) as exc:
        error(f"Could not load the game configuration from {config_path}:\n{exc}")
        return None


def run_pipeline(
    dry_run: bool = False,
    compose_only: bool = False,
    apply_only: bool = False,
    clean_output: bool = False,
    *,
    renpy_games: Path = RENPY_GAMES,
    dest: Path = GAME_FOLDER,
    config_path: Path = CONFIG_PATH,
) -> int:
    """Run the canonical decompile -> compose pipeline.

    The default flow always decompiles first, then runs compose.  The apply
    step is deliberately not run by default so the user can verify compose
    output.

    Returns:
        0 on success, 1 for a failed or missing game/config, and 2 for an
        invalid combination of options.
    """
    if compose_only and apply_only:
        error("--compose-only and --apply-only cannot be used together.")
        return 2

    if compose_only:
        config = _load_config(config_path)
        if config is None:
            return 1
        rule(f"Conversion pipeline: {config.name}")
        try:
            compose_game(config, dry_run=dry_run, clean=clean_output)
        except Exception as exc:  # noqa: BLE001
            error(f"Compose failed: {exc}")
            return 1
        if dry_run:
            rule("Dry run complete: no files were written.")
            return 0
        rule("Composites ready for review")
        print_review_instructions(config, config_path)
        return 0

    if apply_only:
        config = _load_config(config_path)
        if config is None:
            return 1
        if dry_run:
            rule("Conversion pipeline: apply dry run")
            rule("No live scripts were changed.")
            return 0
        try:
            return apply_game(config)
        except Exception as exc:  # noqa: BLE001
            error(f"Apply failed: {exc}")
            return 1

    rule("Conversion pipeline: decompile -> compose")
    step(1, "Stage, extract, and decompile the source game")
    try:
        result = prepare_game(
            renpy_games=renpy_games,
            dest=dest,
            config_path=config_path,
            dry_run=dry_run,
        )
    except Exception as exc:  # noqa: BLE001
        error(f"Decompile preparation failed: {exc}")
        return 1
    if not result.success:
        error("Decompile preparation failed; compose was not started.")
        return 1

    if dry_run:
        rule("Dry run complete: no files were written and compose was not started.")
        return 0

    prepared_config_path = result.config or config_path
    config = _load_config(prepared_config_path)
    if config is None:
        return 1

    step(2, "Create composites and manifests (apply after review)")
    try:
        compose_game(config, dry_run=False, clean=clean_output)
    except Exception as exc:  # noqa: BLE001
        error(f"Compose failed: {exc}")
        return 1
    rule("Composites ready for review")
    print_review_instructions(config, prepared_config_path)
    return 0


@app.command()
def main(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the pipeline without staging, extracting, or writing files.",
    ),
    clean_output: bool = typer.Option(
        False,
        "--clean-output",
        help="Remove previous composite images and manifests before composing.",
    ),
    compose_only: bool = typer.Option(
        False,
        "--compose-only",
        help="Create composites and manifests without decompiling first.",
    ),
    apply_only: bool = typer.Option(
        False,
        "--apply-only",
        help="Apply existing composites without running decompile again.",
    ),
) -> None:
    """Run the decompile and compose workflow; apply remains deferred for review."""
    raise typer.Exit(
        run_pipeline(
            dry_run=dry_run,
            compose_only=compose_only,
            apply_only=apply_only,
            clean_output=clean_output,
        )
    )


if __name__ == "__main__":
    sys.exit(app())
