"""Single interactive entry point for composing photoreal composites.

Flow:

1. **Menu** — clean previous output, compose, clean and compose, review
   composites, apply composites, or restore the original game files.
2. **After composing** — choose to review now or exit, with the manual review
   steps printed either way.

Usage::

    python -m converter.compose                  # interactive menu
    python -m converter.compose clean            # remove previous compose output
    python -m converter.compose compose          # compose only (no apply)
    python -m converter.compose clean-compose    # clean output, then compose
    python -m converter.compose review           # review scene character associations + unmapped speakers
    python -m converter.compose apply            # adopt composites into live .rpy scripts
    python -m converter.compose restore          # recover the live game folder from source, keep composites

Run from the repository root.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer
from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from cli import (
    CYAN,
    MAUVE,
    SLATE,
    Choice,
    command,
    console,
    counted,
    create_app,
    error,
    info,
    is_interactive,
    kv,
    loading,
    print_panel,
    rule,
    select,
    substep,
    success,
    warning,
)

from ..decompile.constants import CONFIG_PATH
from ..restore import restore_compose_from_source
from .apply_pass import Applier
from .compose_pass import Composer
from .config import GameConfig, load_config
from .errors import ConfigError
from .manifest import ManifestWriter
from .review_instructions import manual_scene_instructions, manual_speaker_instructions

app = create_app("Create, review, and apply photoreal composites for a Ren'Py game.")

ACTION_CLEAN = "clean"
ACTION_COMPOSE = "compose"
ACTION_CLEAN_COMPOSE = "clean-compose"
ACTION_REVIEW = "review"
ACTION_APPLY = "apply"
ACTION_RESTORE = "restore"
ACTION_QUIT = "quit"

ACTIONS = (
    ACTION_CLEAN,
    ACTION_COMPOSE,
    ACTION_CLEAN_COMPOSE,
    ACTION_REVIEW,
    ACTION_APPLY,
    ACTION_RESTORE,
)


# --------------------------------------------------------------------------- #
# Shared operations (also used by converter.pipeline)                          #
# --------------------------------------------------------------------------- #


def _print_missing_config_guidance(config_path: Path) -> int:
    """Explain how to create a missing game configuration."""
    info(f"No game configuration was found at {config_path}.")
    info("Next step: decompile the game to create the configuration.")
    command("mise run decompile")
    return 0


def load_game_config(config_path: Path) -> GameConfig | None:
    """Load a :class:`GameConfig`, printing a friendly error on failure."""
    try:
        return load_config(config_path)
    except (OSError, ConfigError) as exc:
        error(f"Could not load the game configuration from {config_path}.\n{exc}")
        info("Run 'mise run decompile' first to create it.")
        return None


def clean_output(config: GameConfig) -> None:
    """Remove previously generated composites (PNGs and report manifests)."""
    output_dir = config.resolved_paths()["output_dir"]
    if output_dir.exists():
        with loading("Removing previous composite output..."):
            shutil.rmtree(output_dir)
        success(f"Removed previous composite output: {output_dir}")
    else:
        info(f"No previous composite output was found at {output_dir}.")


def compose_game(
    config: GameConfig,
    *,
    dry_run: bool = False,
    clean: bool = False,
) -> None:
    """Run the compose pass and write the split category manifests."""
    rule("Creating composites")
    if clean:
        substep("Removing previous composite output", last=True)
        clean_output(config)

    manifest = Composer(config).compose(dry_run=dry_run, clean_output=clean)
    ManifestWriter(config).write(manifest, dry_run=dry_run)
    composite_count = counted(len(manifest.composites_list), "composite")
    if dry_run:
        info(f"Dry run: {composite_count} would be created.")
    else:
        success(f"Created {composite_count}.")


def _compose_action(
    config_path: Path,
    *,
    dry_run: bool,
    clean: bool,
) -> int:
    """Load the config, compose, and offer review."""
    config = load_game_config(config_path)
    if config is None:
        return 1

    compose_game(config, dry_run=dry_run, clean=clean)
    return post_compose(config, config_path, dry_run=dry_run)


def pending_review(config: GameConfig) -> tuple[list[str], list[str]]:
    """Return ``(unresolved_scenes, unmapped_speakers)`` from the pending report."""
    pending = ManifestWriter.load_pending_review(config)
    return pending.get("scenes", []), pending.get("unmapped_speakers", [])


def print_review_instructions(config: GameConfig, config_path: Path) -> None:
    """Print a boxed guide for resolving pending scene and speaker reviews."""
    scenes, speakers = pending_review(config)
    if not scenes and not speakers:
        return

    reports_dir = config.resolved_paths()["manifest_path"].parent
    pending_path = reports_dir / "review_pending.json"
    review_cmd = f'python -m converter.compose.review compose-review "{config_path}"'

    summary = Table.grid(expand=False, padding=(0, 2))
    summary.add_column(style=SLATE, no_wrap=True)
    summary.add_column(style=CYAN, overflow="fold")
    summary.add_row(
        Text("Scenes without character associations"),
        Text(counted(len(scenes), "scene composite")),
    )
    summary.add_row(
        Text("Unmapped speakers"),
        Text(counted(len(speakers), "speaker")),
    )
    summary.add_row(Text("Pending review file"), Text(str(pending_path)))

    recommended = Table.grid(expand=False, padding=(0, 2))
    recommended.add_column(style=CYAN, overflow="fold")
    recommended.add_column(style=SLATE, overflow="fold")
    recommended.add_row(
        Text("mise run compose"),
        Text("Open the menu and choose 'Review character and speaker mappings'."),
    )

    direct_command = Table.grid(expand=False, padding=(0, 2))
    direct_command.add_column(style=CYAN, overflow="fold")
    direct_command.add_row(Text(review_cmd))

    apply_command = Table.grid(expand=False, padding=(0, 2))
    apply_command.add_column(style=CYAN, overflow="fold")
    apply_command.add_row(Text("mise run compose apply"))

    content: list[RenderableType] = [
        Text("Summary", style="bold"),
        summary,
        Text(""),
        Text("Recommended action", style="bold"),
        Text("Resolve the remaining items interactively."),
        recommended,
        Text(""),
        Text("Direct command", style="bold"),
        Text("Run the review command directly if you prefer not to use the menu."),
        direct_command,
    ]

    if scenes:
        heading, instructions = manual_scene_instructions(reports_dir)
        content.extend([Text(""), Text(heading, style="bold"), *instructions])

    if speakers:
        heading, instructions = manual_speaker_instructions(reports_dir)
        content.extend([Text(""), Text(heading, style="bold"), *instructions])

    content.extend(
        [
            Text(""),
            Text("After review", style="bold"),
            Text("Apply the reviewed composites to the game scripts:"),
            apply_command,
        ]
    )

    print_panel(Group(*content), title="Review required", style=MAUVE)


def apply_game(config: GameConfig) -> int:
    """Adopt composites into live ``.rpy`` scripts.

    Refuses to run while scene composites still lack a character association,
    because that would emit composites with unknown speakers.
    """
    if not ManifestWriter.has_composite_manifest(config):
        info("No composites are available to apply.")
        info("Next step: create the composites before applying them.")
        command("mise run compose")
        return 0

    scenes, speakers = pending_review(config)
    if scenes:
        pending_path = (
            config.resolved_paths()["manifest_path"].parent / "review_pending.json"
        )
        warning(
            "Apply blocked: character associations are still missing for "
            f"{counted(len(scenes), 'scene composite')}.\n"
            f"Resolve them in {pending_path}, then run compose again.\n"
            "The live game scripts were not changed."
        )
        return 1
    if speakers:
        warning(
            f"Unmapped speakers remaining: {counted(len(speakers), 'speaker')}.\n"
            "Review them with 'mise run compose' if the composites look incorrect."
        )

    rule("Applying composites to game scripts")
    Applier(config).apply()
    success("Applied composites to the live .rpy scripts.")
    return 0


def restore_game(config_path: Path, *, dry_run: bool) -> int:
    """Restore the live game folder from the original source, keeping composites.

    Reads ``config.json`` (``source_path`` + ``game_folder``) and recovers the
    live ``game`` folder from the original source, overwriting apply-modified
    files while preserving ``game_folder/composites``.
    """
    rule("Restoring game files from source")
    kv("Config", str(config_path))
    results = restore_compose_from_source(config_path, dry_run=dry_run)
    if not results:
        info("No files needed restoring.")
        return 0
    for path, status in sorted(results.items()):
        console.print(f"  [{status}] {path}")
    if dry_run:
        info("Dry run: no files were changed.")
    else:
        success("Game files restored from source. Generated composites were kept.")
    return 0


def review_composites(config_path: Path) -> int:
    """Run the interactive scene/speaker review."""
    from .review.interactive import review

    return review(str(config_path))


# --------------------------------------------------------------------------- #
# Interactive flow                                                            #
# --------------------------------------------------------------------------- #


def post_compose(
    config: GameConfig,
    config_path: Path,
    *,
    dry_run: bool,
) -> int:
    """Offer to review after composing, and print the manual review steps."""
    print_review_instructions(config, config_path)
    rule("Dry run complete" if dry_run else "Composites ready for review")

    if dry_run:
        info("Dry run: no files were written, so the review prompt was skipped.")
        return 0

    if not is_interactive():
        info(
            "No interactive terminal detected. Review later with "
            "'mise run compose review'."
        )
        return 0

    choice = select(
        "What would you like to do next?",
        choices=[
            Choice(
                title="Review composites now",
                value=ACTION_REVIEW,
                shortcut_key="r",
            ),
            Choice(
                title="Exit for now (review and apply later)",
                value=ACTION_QUIT,
                shortcut_key="x",
            ),
        ],
        default=ACTION_REVIEW,
        use_shortcuts=True,
    )
    if choice is None or choice == ACTION_QUIT:
        info(
            "You can review later with 'mise run compose review' and apply with "
            "'mise run compose apply'."
        )
        return 0

    return review_composites(config_path)


def interactive(config_path: Path, *, dry_run: bool) -> int:
    """Show the compose action menu and run the selected action."""
    if not config_path.is_file():
        return _print_missing_config_guidance(config_path)

    if not is_interactive():
        error("The interactive compose menu requires an interactive terminal.")
        info(
            "Choose an action instead: clean, compose, clean-compose, review, "
            "apply, or restore."
        )
        info("Example: python -m converter.compose clean-compose")
        return 1

    config = load_game_config(config_path)
    if config is None:
        return 1

    rule(f"Compose: {config.name}")
    kv("Config", str(config_path))

    choice = select(
        "Choose an action",
        choices=[
            Choice(
                title="Clean previous composite output",
                value=ACTION_CLEAN,
                shortcut_key="c",
            ),
            Choice(
                title="Compose new composites (game scripts stay unchanged)",
                value=ACTION_COMPOSE,
                shortcut_key="n",
            ),
            Choice(
                title="Clean output, then compose",
                value=ACTION_CLEAN_COMPOSE,
                shortcut_key="a",
            ),
            Choice(
                title="Review character and speaker mappings",
                value=ACTION_REVIEW,
                shortcut_key="r",
            ),
            Choice(
                title="Apply reviewed composites to game scripts",
                value=ACTION_APPLY,
                shortcut_key="p",
            ),
            Choice(
                title="Restore original game files (keep composites)",
                value=ACTION_RESTORE,
                shortcut_key="s",
            ),
            Choice(title="Exit", value=ACTION_QUIT, shortcut_key="x"),
        ],
        default=ACTION_COMPOSE,
        use_shortcuts=True,
    )
    if choice is None or choice == ACTION_QUIT:
        info("Cancelled. The compose action was not started.")
        return 0

    if choice == ACTION_CLEAN:
        clean_output(config)
        return 0

    if choice == ACTION_COMPOSE:
        return _compose_action(
            config_path,
            dry_run=dry_run,
            clean=False,
        )

    if choice == ACTION_CLEAN_COMPOSE:
        return _compose_action(
            config_path,
            dry_run=dry_run,
            clean=True,
        )

    if choice == ACTION_REVIEW:
        return review_only(config_path)

    if choice == ACTION_APPLY:
        return apply_action(config, dry_run=dry_run)

    return restore_game(config_path, dry_run=dry_run)


def review_only(config_path: Path) -> int:
    """Run the interactive review and point at the follow-up apply step."""
    if not is_interactive():
        error("The interactive review requires an interactive terminal.")
        info("Run the review in a terminal with 'mise run compose review'.")
        return 1

    return review_composites(config_path)


def apply_action(config: GameConfig, *, dry_run: bool) -> int:
    """Run the apply pass, unless this is a dry run."""
    if dry_run:
        info("Dry run: the apply step was skipped.")
        return 0
    return apply_game(config)


# --------------------------------------------------------------------------- #
# Typer command                                                               #
# --------------------------------------------------------------------------- #


def run_action(action: str, config_path: Path, *, dry_run: bool) -> int:
    """Run one action without the menu (used by non-interactive invocations)."""
    if not config_path.is_file():
        return _print_missing_config_guidance(config_path)

    config = load_game_config(config_path)
    if config is None:
        return 1

    rule("Compose")
    kv("Config", str(config_path))

    if action == ACTION_CLEAN:
        clean_output(config)
        return 0

    if action in (ACTION_COMPOSE, ACTION_CLEAN_COMPOSE):
        return _compose_action(
            config_path,
            dry_run=dry_run,
            clean=action == ACTION_CLEAN_COMPOSE,
        )

    if action == ACTION_REVIEW:
        return review_only(config_path)

    if action == ACTION_APPLY:
        return apply_action(config, dry_run=dry_run)

    return restore_game(config_path, dry_run=dry_run)


@app.command()
def main(
    action: str | None = typer.Argument(
        None,
        help=(
            "Action to run without the menu: clean, compose, clean-compose, "
            "review, apply, or restore. Omit for the interactive menu."
        ),
    ),
    config: Path = typer.Option(  # noqa: B008
        CONFIG_PATH,
        "--config",
        help="Path to the game configuration created by decompile.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Preview the compose and apply steps without writing any files.",
    ),
) -> None:
    """Create, review, and apply photoreal composites for a Ren'Py game."""
    if action is None:
        raise typer.Exit(interactive(config, dry_run=dry_run))

    normalized = action.strip().lower()
    if normalized not in ACTIONS:
        error(
            f"Unknown action '{action}'.\n"
            f"Choose one of: {', '.join(ACTIONS)}.\n"
            "Omit the action to open the interactive menu."
        )
        raise typer.Exit(2)

    raise typer.Exit(run_action(normalized, config, dry_run=dry_run))


if __name__ == "__main__":
    sys.exit(app())
