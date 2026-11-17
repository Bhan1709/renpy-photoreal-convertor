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
    uv run python -m converter.pipeline --restore-compose              # undo compose/apply (.rpy from archive)
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import typer

from cli import (
    console,
    create_app,
    error,
    info,
    kv,
    note,
    prompt_text,
    rule,
    step,
    substep,
    success,
)

from .compose.apply_pass import Applier
from .compose.compose_pass import Composer
from .compose.config import GameConfig, load_config
from .compose.manifest import ManifestWriter
from .decompile.constants import CONFIG_PATH
from .decompile.decompile_files import decompile_rpyc_files
from .decompile.discovery import find_game_folders
from .decompile.exclusions import sync_exclusions
from .decompile.extract import extract_rpa_file
from .restore import restore_from_manifest, restore_rpy_files

app = create_app("Decompile a Ren'Py game, then compose photoreal composites.")


# --------------------------------------------------------------------------- #
# Internal helpers (unchanged logic, rich output)                             #
# --------------------------------------------------------------------------- #


def _decompile_and_extract(target_folder: Path) -> None:
    """Decompile every ``.rpyc`` and extract every ``.rpa`` under a game folder."""
    decompile_rpyc_files(target_folder)
    for rpa_file in sorted(target_folder.rglob("*.rpa")):
        extract_rpa_file(rpa_file)


def _restore_compose_game(config: GameConfig, dry_run: bool) -> None:
    """Undo compose/apply for a single game: restore .rpy files from archive.rpa."""
    rule(f"Restore: {config.name}")
    # Prefer the manifest to scope the restore to touched scripts, falling back
    # to restoring every .rpy stored in the archive.
    manifest_path = config.resolved_paths()["manifest_path"]
    game_root = config.game_root
    try:
        if manifest_path.is_file():
            results = restore_from_manifest(game_root, manifest_path, dry_run=dry_run)
        else:
            note(
                f"(no manifest at {manifest_path}; restoring all .rpy from archive.rpa)"
            )
            results = restore_rpy_files(game_root, dry_run=dry_run)
    except Exception:  # noqa: BLE001
        error(f"restore-compose failed for {config.name}")
        return

    if not results:
        info("(nothing to restore)")
        return
    for path, status in sorted(results.items()):
        console.print(f"  [{status}] {path}")


def _prompt_exclusion_patterns(config_path: Path = CONFIG_PATH) -> None:
    """Prompt the user to add exclusion patterns to ``config.json``.

    Reads the current ``exclusion.script.patterns`` and
    ``exclusion.image.patterns`` from ``config.json``, asks the user for
    additional comma-separated patterns for each type, merges them in, and
    writes the file back.  Pressing Enter keeps the current patterns unchanged.

    Script exclusions filter matching entries from ``rpy_files``/``ren_py_files``
    (the include set used by parser.files() and character_associator).
    Image exclusions filter layers whose source path matches a pattern in
    record._normalize_layers, preventing those image folders from being scanned.
    """
    if not config_path.is_file():
        error(f"Config not found: {config_path}")
        info("Run decompile first to generate it.")
        return

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        error(f"Could not read config {config_path}: {exc}")
        return

    exclusion = data.get("exclusion", {}) or {}
    script_current = exclusion.get("script", {}).get("patterns", [])
    image_current = exclusion.get("image", {}).get("patterns", [])

    rule("Exclusion patterns")
    kv("Script exclusions", str(script_current))
    raw = prompt_text(
        "Enter additional script exclusion patterns (comma-separated) "
        "or press Enter to keep current",
        default="",
    )
    if raw is None:
        info("Aborted.")
        return
    script_patterns = [p.strip() for p in raw.split(",") if p.strip()] if raw else []
    if script_patterns:
        script_current = sorted(set(script_current) | set(script_patterns))

    kv("Image exclusions", str(image_current))
    raw = prompt_text(
        "Enter additional image exclusion patterns (comma-separated) "
        "or press Enter to keep current",
        default="",
    )
    if raw is None:
        info("Aborted.")
        return
    image_patterns = [p.strip() for p in raw.split(",") if p.strip()] if raw else []
    if image_patterns:
        image_current = sorted(set(image_current) | set(image_patterns))

    if not script_patterns and not image_patterns:
        return

    data["exclusion"] = {
        "script": {
            "patterns": script_current,
            "note": "Patterns matched against rpy_files/ren_py_files entries; matching entries are removed from the scan list",
        },
        "image": {
            "patterns": image_current,
            "note": "Patterns matched against image source paths; layers whose source starts with a matching folder are excluded from compose scanning",
        },
    }
    try:
        config_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        success(
            f"Updated exclusion patterns: script={script_current}, image={image_current}"
        )
    except OSError as exc:
        error(f"Could not write config {config_path}: {exc}")


def _clean_output_dir(config: GameConfig) -> None:
    """Remove prior generated PNGs and manifest before composing."""
    paths = config.resolved_paths()
    output_dir = paths["output_dir"]
    if output_dir.exists():
        shutil.rmtree(output_dir)
        success(f"Cleaned output directory: {output_dir}")


def _print_manual_review_instructions(config: GameConfig) -> None:
    """Print end-of-compose instructions for resolving pending reviews."""
    reports_dir = config.resolved_paths()["manifest_path"].parent
    pending_path = reports_dir / "review_pending.json"

    if not pending_path.is_file():
        return

    try:
        pending = json.loads(pending_path.read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return

    scenes = pending.get("scenes", [])
    speakers = pending.get("unmapped_speakers", [])
    if not scenes and not speakers:
        return

    rule("Pending scene review")
    info(f"{len(scenes)} scene composite(s) lack character association.")
    info(f"{len(speakers)} speaker(s) not mapped to a character folder.")
    kv("Review file", str(pending_path))
    console.print()
    info("Recommended next step: review interactively with compose-review:")
    console.print("  [cyan]mise run compose-review[/cyan]")
    console.print("or")
    console.print(
        f"  [cyan]python -m converter.compose.review cli compose-review {CONFIG_PATH}[/cyan]"
    )
    console.print()

    info("Alternative: resolve manually by editing the report files:")
    if scenes:
        substep("Associate a character with a scene composite:")
        console.print(f"   - Open {reports_dir / 'manifest_scene.json'}")
        console.print("   - Find the scene composite under ``composites`` and add:")
        console.print('        "characters": [')
        console.print(
            '            {"character": "<folder>", "evidence": ["script_identifier:<script>:<line>"]}'
        )
        console.print("        ],")
        console.print('        "character_association_status": "associated",')
        console.print(
            "   - Then cut-and-paste that record into manifest_scene.json and"
        )
        console.print(
            "     remove it from the ``scenes`` array in review_pending.json."
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
        console.print("   - Then remove the speaker from ``unmapped_speakers`` in")
        console.print("     review_pending.json.")
        console.print()

    info("After resolving (either way), verify nothing remains:")
    console.print("  [cyan]mise run compose-review[/cyan]")
    console.print("or")
    console.print(
        f"  [cyan]python -m converter.compose.review cli compose-review {CONFIG_PATH}[/cyan]"
        f"  python -m converter.compose.review cli compose-review {CONFIG_PATH}"
    )
    console.print("  then apply:  mise run apply")


def run_pipeline(
    dry_run: bool = False,
    compose_only: bool = False,
    apply_only: bool = False,
    restore_compose: bool = False,
    clean_output: bool = False,
) -> int:
    """Run the decompile -> compose -> apply pipeline, or a restore mode.

    The default flow (no restore flag) always decompiles first, then prompts
    the user for exclusion patterns, then runs compose.  The apply step is
    deliberately not run by default so the user can verify compose output.

    All modes are config-driven via ``src/converter/config.json``; no
    ``--game`` lookup is needed.

    Args:
        dry_run: Compose/restore without writing; skips the apply pass.
        compose_only: Compose + write manifest, then stop (skip apply).
            Assumes the game has already been decompiled.
        apply_only: Run the apply pass only (requires an existing manifest).
        restore_compose: Undo compose/apply (restore .rpy files from
            archive.rpa).
        clean_output: Remove prior generated PNGs and manifest before
            composing (only applies to compose modes).

    Returns:
        0 on success, 1 if no matching games were found.
    """
    # --- Restore-compose mode (config-driven, no decompile) ---
    if restore_compose:
        sync_exclusions(CONFIG_PATH)
        config = load_config(CONFIG_PATH)
        rule(f"Pipeline: {config.name}")
        _restore_compose_game(config, dry_run)
        rule("Pipeline complete")
        return 0

    # --- Compose-only / apply-only modes (config-driven, no decompile) ---
    if compose_only or apply_only:
        sync_exclusions(CONFIG_PATH)
        config = load_config(CONFIG_PATH)
        rule(f"Pipeline: {config.name}")

        if compose_only:
            _prompt_exclusion_patterns(CONFIG_PATH)
            sync_exclusions(CONFIG_PATH)
            config = load_config(CONFIG_PATH)
            if clean_output:
                _clean_output_dir(config)
            manifest = Composer(config).compose(
                dry_run=dry_run, clean_output=clean_output
            )
            ManifestWriter(config).write(manifest, dry_run=dry_run)
            rule("Compose complete (apply step deferred)")
            _print_manual_review_instructions(config)
            return 0

        if apply_only:
            Applier(config).apply()
            rule("Pipeline complete")
            return 0

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
    _prompt_exclusion_patterns(CONFIG_PATH)

    # --- Stage 3: sync exclusions + load decompile config ---
    step(3, "Sync exclusions + load config")
    sync_exclusions(CONFIG_PATH)
    config = load_config(CONFIG_PATH)

    # --- Stage 4: compose (manifest + PNGs) only ---
    # Apply is intentionally deferred so the user can verify compose output.
    step(4, "Compose manifest + PNGs (apply deferred)")
    if clean_output:
        _clean_output_dir(config)
    manifest = Composer(config).compose(dry_run=dry_run, clean_output=clean_output)
    ManifestWriter(config).write(manifest, dry_run=dry_run)
    rule("Compose complete (apply step deferred)")
    _print_manual_review_instructions(config)

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
    restore_compose: bool = typer.Option(
        False,
        "--restore-compose",
        help="Undo compose/apply (restore .rpy files from archive.rpa).",
    ),
) -> int:
    """Run the decompile -> compose -> apply pipeline."""
    return run_pipeline(
        dry_run=dry_run,
        compose_only=compose_only,
        apply_only=apply_only,
        restore_compose=restore_compose,
        clean_output=clean_output,
    )


if __name__ == "__main__":
    sys.exit(app())
