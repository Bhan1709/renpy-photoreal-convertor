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

import argparse
import json
import shutil
import sys
from pathlib import Path

from .compose.apply_pass import Applier
from .compose.compose_pass import Composer
from .compose.config import GameConfig, load_config
from .compose.manifest import ManifestWriter
from .decompile.decompile import (
    CONFIG_PATH,
    decompile_rpyc_files,
    extract_rpa_file,
    find_game_folders,
    sync_exclusions,
)
from .restore import restore_from_manifest, restore_rpy_files


def _decompile_and_extract(target_folder: Path) -> None:
    """Decompile every ``.rpyc`` and extract every ``.rpa`` under a game folder."""
    decompile_rpyc_files(target_folder)
    for rpa_file in sorted(target_folder.rglob("*.rpa")):
        extract_rpa_file(rpa_file)


def _restore_compose_game(config: GameConfig, dry_run: bool) -> None:
    """Undo compose/apply for a single game: restore .rpy files from archive.rpa."""
    # Prefer the manifest to scope the restore to touched scripts, falling back
    # to restoring every .rpy stored in the archive.
    manifest_path = config.resolved_paths()["manifest_path"]
    game_root = config.game_root
    try:
        if manifest_path.is_file():
            results = restore_from_manifest(game_root, manifest_path, dry_run=dry_run)
        else:
            print(
                f"  (no manifest at {manifest_path}; restoring all .rpy from archive.rpa)"
            )
            results = restore_rpy_files(game_root, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERR] restore-compose failed for {config.name}: {exc}")
        return

    if not results:
        print("  (nothing to restore)")
        return
    for path, status in sorted(results.items()):
        print(f"  [{status}] {path}")


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
        print(f"Config not found: {config_path}")
        print("Run decompile first to generate it.")
        return

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"Could not read config {config_path}: {exc}")
        return

    exclusion = data.get("exclusion", {}) or {}
    script_current = exclusion.get("script", {}).get("patterns", [])
    image_current = exclusion.get("image", {}).get("patterns", [])

    print(f"\nCurrent script exclusion patterns: {script_current}")
    raw = input(
        "Enter additional script exclusion patterns (comma-separated) "
        "or press Enter to keep current: "
    ).strip()
    script_patterns = [p.strip() for p in raw.split(",") if p.strip()] if raw else []
    if script_patterns:
        script_current = sorted(set(script_current) | set(script_patterns))

    print(f"\nCurrent image exclusion patterns: {image_current}")
    raw = input(
        "Enter additional image exclusion patterns (comma-separated) "
        "or press Enter to keep current: "
    ).strip()
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
        print(
            f"Updated exclusion patterns: script={script_current}, image={image_current}"
        )
    except OSError as exc:
        print(f"Could not write config {config_path}: {exc}")


def _clean_output_dir(config: GameConfig) -> None:
    """Remove prior generated PNGs and manifest before composing."""
    paths = config.resolved_paths()
    output_dir = paths["output_dir"]
    if output_dir.exists():
        shutil.rmtree(output_dir)
        print(f"Cleaned output directory: {output_dir}")


def _print_manual_review_instructions(config: GameConfig) -> None:
    """Print end-of-compose instructions for resolving pending reviews.

    The primary recommendation is to run ``compose-review`` interactively.
    Manual editing of the report files is offered as an alternative for users
    who prefer to resolve items by hand.
    """
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

    print("\n=== Pending scene review ===")
    print(f"{len(scenes)} scene composite(s) lack character association.")
    print(f"{len(speakers)} speaker(s) not mapped to a character folder.")
    print(f"Review file: {pending_path}")
    print()
    print("Recommended next step: review interactively with compose-review:")
    print("  mise run compose-review")
    print("or")
    print(f"  python -m converter.compose.review.cli compose-review {CONFIG_PATH}")
    print()

    print("Alternative: resolve manually by editing the report files:")
    if scenes:
        print("1) Associate a character with a scene composite:")
        print(f"   - Open {reports_dir / 'manifest_scene.json'}")
        print("   - Find the scene composite under ``composites`` and add:")
        print('        "characters": [')
        print(
            '            {"character": "<folder>", "evidence": ["script_identifier:<script>:<line>"]}'
        )
        print("        ],")
        print('        "character_association_status": "associated",')
        print("   - Then cut-and-paste that record into manifest_scene.json and")
        print("     remove it from the ``scenes`` array in review_pending.json.")
        print()
    if speakers:
        print("2) Resolve an unmapped speaker:")
        print(f"   - Open {reports_dir / 'character_aliases.json'}")
        print("   - Add the speaker under its character folder, e.g.:")
        print('        "clothes": {')
        print('            "folder": "archive/chars/clothes",')
        print('            "aliases": ["clothes"]')
        print("        },")
        print("   - Then remove the speaker from ``unmapped_speakers`` in")
        print("     review_pending.json.")
        print()

    print("After resolving (either way), verify nothing remains:")
    print("  mise run compose-review")
    print("or")
    print(f"  python -m converter.compose.review.cli compose-review {CONFIG_PATH}")
    print("  then apply:  mise run apply")


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
        print(f"\n=== Pipeline: {config.name} ===")
        _restore_compose_game(config, dry_run)
        print("\n=== Pipeline complete ===")
        return 0

    # --- Compose-only / apply-only modes (config-driven, no decompile) ---
    if compose_only or apply_only:
        sync_exclusions(CONFIG_PATH)
        config = load_config(CONFIG_PATH)
        print(f"\n=== Pipeline: {config.name} ===")

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
            print("\n=== Compose complete. Apply step deferred. ===")
            _print_manual_review_instructions(config)
            return 0

        if apply_only:
            Applier(config).apply()
            print("\n=== Pipeline complete ===")
            return 0

    # --- Default flow: decompile -> exclusions prompt -> compose (no apply) ---
    # Auto-discover the first game folder under src/renpy_game (no --game needed).
    folders = find_game_folders()
    if not folders:
        print("No game folders found under src/renpy_game.")
        return 1

    game_dir, _rel_path, target_folder = folders[0]
    name = game_dir.name
    print(f"\n=== Pipeline: {name} ===")

    # --- Stage 1: decompile (.rpyc -> .rpy) + extract (.rpa -> archive/) ---
    _decompile_and_extract(target_folder)

    # --- Stage 2: prompt user for exclusion patterns ---
    _prompt_exclusion_patterns(CONFIG_PATH)

    # --- Stage 3: sync exclusions + load decompile config ---
    sync_exclusions(CONFIG_PATH)
    config = load_config(CONFIG_PATH)

    # --- Stage 4: compose (manifest + PNGs) only ---
    # Apply is intentionally deferred so the user can verify compose output.
    if clean_output:
        _clean_output_dir(config)
    manifest = Composer(config).compose(dry_run=dry_run, clean_output=clean_output)
    ManifestWriter(config).write(manifest, dry_run=dry_run)
    print("\n=== Compose complete. Apply step deferred. ===")
    _print_manual_review_instructions(config)

    print("\n=== Pipeline complete ===")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Decompile a Ren'Py game, then compose photoreal composites.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose without writing; skips the apply pass.",
    )
    ap.add_argument(
        "--clean-output",
        action="store_true",
        help="Remove prior generated PNGs and manifest before composing.",
    )
    ap.add_argument(
        "--compose-only",
        action="store_true",
        help="Compose + write manifest, then stop (skip apply). Assumes decompiled.",
    )
    ap.add_argument(
        "--apply-only",
        action="store_true",
        help="Run the apply pass only (requires an existing manifest).",
    )
    ap.add_argument(
        "--restore-compose",
        action="store_true",
        help="Undo compose/apply (restore .rpy files from archive.rpa).",
    )
    args = ap.parse_args(argv)

    return run_pipeline(
        dry_run=args.dry_run,
        compose_only=args.compose_only,
        apply_only=args.apply_only,
        restore_compose=args.restore_compose,
        clean_output=args.clean_output,
    )


if __name__ == "__main__":
    sys.exit(main())
