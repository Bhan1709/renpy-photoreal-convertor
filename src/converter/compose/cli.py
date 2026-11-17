"""Single interactive entry point for composing photoreal composites.

Flow:

1. **Exclusion check** — asks whether to add script/image exclusion patterns and
   syncs them into ``src/converter/config.json`` (the same logic as
   ``python -m converter.decompile.sync_exclusions_cli``).
2. **Menu** — clean previous output, compose (no apply), clean and compose,
   review composites, or apply composites.
3. **After composing** — choose to review now or quit, with the manual review
   steps printed either way.

Usage::

    python -m converter.compose                  # interactive menu
    python -m converter.compose clean            # remove previous compose output
    python -m converter.compose compose          # compose only (no apply)
    python -m converter.compose clean-compose    # clean output, then compose
    python -m converter.compose review           # review scenes/speakers
    python -m converter.compose apply            # adopt composites into scripts

Run from the repository root.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer

from cli import (
    Choice,
    console,
    create_app,
    error,
    info,
    is_interactive,
    kv,
    rule,
    select,
    substep,
    success,
    warning,
)

from ..decompile.constants import CONFIG_PATH
from ..decompile.sync_exclusions_cli import run_exclusion_sync
from .apply_pass import Applier
from .compose_pass import Composer
from .config import GameConfig, load_config
from .errors import ConfigError
from .manifest import ManifestWriter

app = create_app("Compose photoreal composites for a Ren'Py game (interactive menu).")

ACTION_CLEAN = "clean"
ACTION_COMPOSE = "compose"
ACTION_CLEAN_COMPOSE = "clean-compose"
ACTION_REVIEW = "review"
ACTION_APPLY = "apply"
ACTION_QUIT = "quit"

ACTIONS = (
    ACTION_CLEAN,
    ACTION_COMPOSE,
    ACTION_CLEAN_COMPOSE,
    ACTION_REVIEW,
    ACTION_APPLY,
)


# --------------------------------------------------------------------------- #
# Shared operations (also used by converter.pipeline)                          #
# --------------------------------------------------------------------------- #


def load_game_config(config_path: Path) -> GameConfig | None:
    """Load a :class:`GameConfig`, printing a friendly error on failure."""
    try:
        return load_config(config_path)
    except (OSError, ConfigError) as exc:
        error(f"Could not load config {config_path}: {exc}")
        info("Run 'mise run decompile' first to generate it.")
        return None


def clean_output(config: GameConfig) -> None:
    """Remove previously generated composites (PNGs and report manifests)."""
    output_dir = config.resolved_paths()["output_dir"]
    if output_dir.exists():
        shutil.rmtree(output_dir)
        success(f"Cleaned composite output: {output_dir}")
    else:
        info(f"Nothing to clean: {output_dir} does not exist.")


def compose_game(
    config: GameConfig,
    *,
    dry_run: bool = False,
    clean: bool = False,
) -> None:
    """Run the compose pass and write the split category manifests."""
    rule("Compose pass")
    if clean:
        substep("Removing previous composites")
        clean_output(config)

    manifest = Composer(config).compose(dry_run=dry_run, clean_output=clean)
    ManifestWriter(config).write(manifest, dry_run=dry_run)
    if dry_run:
        info(f"Dry run: {len(manifest.composites_list)} composite(s) planned.")
    else:
        success(f"Composed {len(manifest.composites_list)} composite(s).")


def pending_review(config: GameConfig) -> tuple[list[str], list[str]]:
    """Return ``(unresolved_scenes, unmapped_speakers)`` from the pending report."""
    pending = ManifestWriter.load_pending_review(config)
    return pending.get("scenes", []), pending.get("unmapped_speakers", [])


def print_review_instructions(config: GameConfig, config_path: Path) -> None:
    """Print the manual steps for resolving pending scene/speaker reviews."""
    scenes, speakers = pending_review(config)
    if not scenes and not speakers:
        return

    reports_dir = config.resolved_paths()["manifest_path"].parent
    pending_path = reports_dir / "review_pending.json"
    review_cmd = f"python -m converter.compose.review compose-review {config_path}"

    rule("Pending review")
    info(f"{len(scenes)} scene composite(s) lack character association.")
    info(f"{len(speakers)} speaker(s) are not mapped to a character folder.")
    kv("Review file", str(pending_path))
    console.print()

    info("Review interactively (recommended):")
    console.print("  [cyan]mise run compose[/cyan] [dim]-> Review composites[/dim]")
    console.print("or")
    console.print(f"  [cyan]{review_cmd}[/cyan]")
    console.print()

    info("Alternative: resolve manually by editing the report files.")
    if scenes:
        substep("Associate a character with a scene composite:")
        console.print(f"   - Open {reports_dir / 'manifest_scene.json'}")
        console.print('   - Find the scene under "composites" and add:')
        console.print('        "characters": [')
        console.print(
            '            {"character": "<folder>", '
            '"evidence": ["script_identifier:<script>:<line>"]}'
        )
        console.print("        ],")
        console.print('        "character_association_status": "associated",')
        console.print(
            "   - Remove that record from the ``scenes`` array in review_pending.json."
        )
        console.print()
    if speakers:
        substep("Resolve an unmapped speaker:")
        console.print(f"   - Open {reports_dir / 'character_aliases.json'}")
        console.print("   - Add the speaker under its character folder, e.g.:")
        console.print('        "clothes": {')
        console.print('            "folder": "archive/chars/clothes",')
        console.print('            "aliases": ["clothes"]')
        console.print("        },")
        console.print(
            "   - Remove the speaker from ``unmapped_speakers`` in review_pending.json."
        )
        console.print()

    info("When nothing is pending, apply the composites:")
    console.print("  [cyan]mise run compose apply[/cyan]")
    console.print()


def apply_game(config: GameConfig) -> int:
    """Adopt composites into live ``.rpy`` scripts.

    Refuses to run while scene composites still lack a character association,
    because that would emit composites with unknown speakers.
    """
    scenes, speakers = pending_review(config)
    if scenes:
        pending_path = (
            config.resolved_paths()["manifest_path"].parent / "review_pending.json"
        )
        warning(
            f"BLOCKED: {len(scenes)} scene composite(s) lack character "
            f"association.\nResolve them in {pending_path}, then re-run compose.\n"
            "Skipping apply pass."
        )
        return 1
    if speakers:
        warning(
            f"{len(speakers)} unmapped speaker(s) remain. "
            "Review with `mise run compose` -> Review composites if composites "
            "look wrong."
        )

    rule("Apply pass")
    Applier(config).apply()
    success("Applied composites into live .rpy scripts.")
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
    rule("Compose complete (apply deferred)")

    if dry_run:
        info("Dry run: no files were written; skipping the review prompt.")
        return 0

    if not is_interactive():
        info("No interactive terminal: review later with 'mise run compose review'.")
        return 0

    choice = select(
        "What would you like to do next?",
        choices=[
            Choice(
                title="Review the composites now (interactive)",
                value=ACTION_REVIEW,
            ),
            Choice(title="Quit (review/apply later)", value=ACTION_QUIT),
        ],
        default=ACTION_REVIEW,
        use_shortcuts=True,
    )
    if choice is None or choice == ACTION_QUIT:
        info(
            "Review later with 'mise run compose review'; "
            "apply with 'mise run compose apply'."
        )
        return 0

    code = review_composites(config_path)
    if code == 0:
        info("Apply when ready: 'mise run compose apply'.")
    return code


def interactive(config_path: Path, *, dry_run: bool) -> int:
    """Exclusion check, then the compose action menu."""
    if not is_interactive():
        error("No interactive terminal: the compose menu cannot be shown.")
        info("Pass an action instead: clean, compose, clean-compose, review, apply.")
        info("Example: python -m converter.compose clean-compose")
        return 1

    if run_exclusion_sync(config_path, prompt=True) != 0:
        return 1

    config = load_game_config(config_path)
    if config is None:
        return 1

    rule(f"Compose: {config.name}")
    kv("Config", str(config_path))

    choice = select(
        "What would you like to do?",
        choices=[
            Choice(title="Clean previous compose output", value=ACTION_CLEAN),
            Choice(title="Compose", value=ACTION_COMPOSE),
            Choice(title="Clean and compose", value=ACTION_CLEAN_COMPOSE),
            Choice(title="Review composites (scenes + speakers)", value=ACTION_REVIEW),
            Choice(title="Apply composites into live scripts", value=ACTION_APPLY),
            Choice(title="Quit", value=ACTION_QUIT),
        ],
        default=ACTION_COMPOSE,
        use_shortcuts=True,
    )
    if choice is None or choice == ACTION_QUIT:
        info("Aborted.")
        return 0

    if choice == ACTION_CLEAN:
        clean_output(config)
        return 0

    if choice == ACTION_COMPOSE:
        compose_game(config, dry_run=dry_run)
        return post_compose(config, config_path, dry_run=dry_run)

    if choice == ACTION_CLEAN_COMPOSE:
        compose_game(config, dry_run=dry_run, clean=True)
        return post_compose(config, config_path, dry_run=dry_run)

    if choice == ACTION_REVIEW:
        return review_only(config_path)

    return apply_action(config, dry_run=dry_run)


def review_only(config_path: Path) -> int:
    """Run the interactive review and point at the follow-up apply step."""
    if not is_interactive():
        error("No interactive terminal: the review step needs a terminal.")
        info("Review in a terminal with 'mise run compose review'.")
        return 1

    code = review_composites(config_path)
    if code == 0:
        info("Apply when ready: 'mise run compose apply'.")
    return code


def apply_action(config: GameConfig, *, dry_run: bool) -> int:
    """Run the apply pass, unless this is a dry run."""
    if dry_run:
        info("Dry run: skipping the apply pass.")
        return 0
    return apply_game(config)


# --------------------------------------------------------------------------- #
# Typer command                                                               #
# --------------------------------------------------------------------------- #


def run_action(action: str, config_path: Path, *, dry_run: bool) -> int:
    """Run one action without the menu (used by non-interactive invocations)."""
    # The exclusion check is always the first step, matching the interactive flow.
    if run_exclusion_sync(config_path, prompt=True) != 0:
        return 1

    config = load_game_config(config_path)
    if config is None:
        return 1

    rule("Compose")
    kv("Config", str(config_path))

    if action == ACTION_CLEAN:
        clean_output(config)
        return 0

    if action in (ACTION_COMPOSE, ACTION_CLEAN_COMPOSE):
        compose_game(config, dry_run=dry_run, clean=action == ACTION_CLEAN_COMPOSE)
        return post_compose(config, config_path, dry_run=dry_run)

    if action == ACTION_REVIEW:
        return review_only(config_path)

    return apply_action(config, dry_run=dry_run)


@app.command()
def main(
    action: str | None = typer.Argument(
        None,
        help=(
            "Action to run without the menu: clean, compose, clean-compose, "
            "review, apply. Omit for the interactive menu."
        ),
    ),
    config: Path = typer.Option(  # noqa: B008
        CONFIG_PATH,
        "--config",
        help="Path to the decompile-generated config JSON.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Compose without writing PNGs or manifests (skips review/apply).",
    ),
) -> None:
    """Compose photoreal composites, interactively by default."""
    if action is None:
        raise typer.Exit(interactive(config, dry_run=dry_run))

    normalized = action.strip().lower()
    if normalized not in ACTIONS:
        error(
            f"Unknown action '{action}'. Choose one of: {', '.join(ACTIONS)}, "
            "or omit the action for the interactive menu."
        )
        raise typer.Exit(2)

    raise typer.Exit(run_action(normalized, config, dry_run=dry_run))


if __name__ == "__main__":
    sys.exit(app())
