"""End-to-end converter pipeline: decompile then compose Ren'Py games.

Links :mod:`converter.decompile` (decompiles ``.rpyc -> .rpy`` and extracts
``.rpa`` archives under ``src/renpy_game``) with :mod:`converter.compose`
(builds photoreal composites from the resulting ``.rpy`` scripts and adopts
them back into the live scripts) and :mod:`converter.restore` (reverts
modified ``.rpy`` files from the archive).

Run from the repo root::

    uv run python -m converter.pipeline                                # all games
    uv run python -m converter.pipeline --game CampMourningWood-0.25.0.1-pc
    uv run python -m converter.pipeline --game X --decompile-only      # decompile + extract only
    uv run python -m converter.pipeline --game X --compose-only        # compose + manifest only
    uv run python -m converter.pipeline --game X --apply-only          # apply only (needs manifest)
    uv run python -m converter.pipeline --game X --dry-run             # compose without writing
    uv run python -m converter.pipeline --game X --restore-decompile   # undo decompile outputs
    uv run python -m converter.pipeline --game X --restore-compose     # undo compose/apply (.rpy from archive)
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .compose.applier import Applier
from .compose.composer import Composer
from .compose.config import GameConfig, load_config
from .compose.manifest import ManifestWriter
from .decompile.decompile import (
    CONFIG_PATH,
    decompile_rpyc_files,
    extract_rpa_file,
    find_game_folders,
    restore_decompile,
    sync_exclusions,
)
from .restore import restore_from_manifest, restore_rpy_files


def _decompile_and_extract(target_folder: Path) -> None:
    """Decompile every ``.rpyc`` and extract every ``.rpa`` under a game folder."""
    decompile_rpyc_files(target_folder)
    for rpa_file in sorted(target_folder.rglob("*.rpa")):
        extract_rpa_file(rpa_file)


def _restore_decompile_game(target_folder: Path, dry_run: bool) -> None:
    """Undo decompile outputs for a single game folder."""
    results = restore_decompile(target_folder, dry_run=dry_run)
    if not results:
        print("  (nothing to restore)")
        return
    for path, status in sorted(results.items()):
        print(f"  [{status}] {path}")


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
            print(f"  (no manifest at {manifest_path}; restoring all .rpy from archive.rpa)")
            results = restore_rpy_files(game_root, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        print(f"  [ERR] restore-compose failed for {config.name}: {exc}")
        return

    if not results:
        print("  (nothing to restore)")
        return
    for path, status in sorted(results.items()):
        print(f"  [{status}] {path}")


def run_pipeline(
    game_name: str | None = None,
    *,
    dry_run: bool = False,
    decompile_only: bool = False,
    compose_only: bool = False,
    apply_only: bool = False,
    restore_decompile: bool = False,
    restore_compose: bool = False,
    pipeline: bool = False,
) -> int:
    """Run the decompile -> compose -> apply pipeline, or a restore mode.

    Args:
        game_name: Process only the game whose directory under
            ``src/renpy_game`` matches this name.  Only required for the
            folder-driven modes (``--decompile-only``, ``--pipeline``,
            ``--restore-decompile``).  The compose-driven modes
            (``--compose-only``, ``--apply-only``, ``--restore-compose``)
            work exclusively from ``src/converter/config.json``, which records
            ``game_folder``, ``rpy_files``, ``ren_py_files``, and
            ``image_categories`` produced by decompile.
        dry_run: Compose/restore without writing; skips the apply pass.
        decompile_only: Stop after decompile + extract.
        compose_only: Compose + write manifest, then stop (skip apply).
            Assumes the game has already been decompiled.
        apply_only: Run the apply pass only (requires an existing manifest).
        restore_decompile: Undo decompile outputs (remove generated .rpy and
            extracted archive dirs). Mutually exclusive with forward modes.
        restore_compose: Undo compose/apply (restore .rpy files from
            archive.rpa). Mutually exclusive with forward modes.
        pipeline: Run decompile + compose, then stop (skip apply). Equivalent
            to running ``--decompile-only`` followed by ``--compose-only``.

    Returns:
        0 on success, 1 if no matching games were found.
    """
    restore_mode = restore_decompile or restore_compose
    forward_mode = decompile_only or compose_only or apply_only or pipeline

    if restore_mode and forward_mode:
        print("Error: --restore-* cannot be combined with --decompile-only/--compose-only/--apply-only/--pipeline.")
        return 1

    # --- Config-driven modes: compose / apply / restore-compose ---
    # These operate exclusively on the game_folder recorded in
    # src/converter/config.json (produced by decompile).  No --game lookup is
    # needed — the config is the source of truth for game_folder, rpy_files,
    # ren_py_files, and image_categories.
    if compose_only or apply_only or restore_compose:
        sync_exclusions(CONFIG_PATH)
        config = load_config(CONFIG_PATH)
        print(f"\n=== Pipeline: {config.name} ===")

        if restore_compose:
            _restore_compose_game(config, dry_run)
            print("\n=== Pipeline complete ===")
            return 0

        run_decompile = False
        run_compose = not apply_only
        run_apply = not (compose_only or dry_run)

        # --- Stage 1: compose (manifest + PNGs) ---
        if run_compose:
            manifest = Composer(config).compose(dry_run=dry_run)
            ManifestWriter(config).write(manifest, dry_run=dry_run)
            if compose_only or dry_run:
                print("\n=== Pipeline complete ===")
                return 0

        # --- Stage 2: apply (adopt composites into live .rpy scripts) ---
        if run_apply:
            Applier(config).apply()

        print("\n=== Pipeline complete ===")
        return 0

    # --- Folder-driven modes: decompile / pipeline / restore-decompile ---
    # These are deprecated: compose-driven modes (--compose-only/--apply-only/
    # --restore-compose) work exclusively from src/converter/config.json.
    # --game is honored only here and defaults to the first game found under
    # src/renpy_game.
    if decompile_only or pipeline or restore_decompile:
        print(
            "[DEPRECATED] --decompile-only/--pipeline/--restore-decompile are "
            "folder-driven modes and will be removed in a future release. Use "
            "the config-driven compose/apply/restore-compose modes instead.",
        )
    folders = find_game_folders()
    if game_name:
        folders = [f for f in folders if f[0].name == game_name]

    if not folders:
        print("No game folders found under src/renpy_game.")
        return 1

    for game_dir, _rel_path, target_folder in folders:
        name = game_dir.name
        print(f"\n=== Pipeline: {name} ===")

        if restore_decompile:
            _restore_decompile_game(target_folder, dry_run)
            continue

        # --- Forward pipeline ---
        run_decompile = not (apply_only or compose_only)
        run_compose = not apply_only or pipeline
        run_apply = not (compose_only or dry_run or pipeline)

        # --- Stage 1: decompile (.rpyc -> .rpy) + extract (.rpa -> archive/) ---
        if run_decompile:
            _decompile_and_extract(target_folder)
            if decompile_only:
                continue

        # --- Stage 2: sync exclusions + load decompile config ---
        config = None
        if run_compose or run_apply:
            # Sync user-defined exclusions (exclusion.patterns) into config.json
            # before composing, then load the generalized config produced by
            # decompile (game_folder, image_categories, rpy_files, ren_py_files).
            sync_exclusions(CONFIG_PATH)
            config = load_config(CONFIG_PATH)

        # --- Stage 3: compose (manifest + PNGs) ---
        if run_compose:
            assert config is not None
            manifest = Composer(config).compose(dry_run=dry_run)
            ManifestWriter(config).write(manifest, dry_run=dry_run)

        # --- Stage 4: apply (adopt composites into live .rpy scripts) ---
        if run_apply:
            assert config is not None
            Applier(config).apply()

    print("\n=== Pipeline complete ===")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Decompile a Ren'Py game, then compose and apply photoreal composites.",
    )
    ap.add_argument(
        "--game",
        help="Game name under src/renpy_game (only needed for --decompile-only/--pipeline/--restore-decompile; compose modes use src/converter/config.json)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Compose/restore without writing; skips the apply pass.",
    )
    ap.add_argument(
        "--decompile-only",
        action="store_true",
        help="(deprecated) Only decompile .rpyc and extract .rpa, then stop.",
    )
    ap.add_argument(
        "--compose-only",
        action="store_true",
        help="Compose + write manifest, then stop (skip apply). Assumes decompiled.",
    )
    ap.add_argument(
        "--pipeline",
        action="store_true",
        help="(deprecated) Run decompile + compose, then stop (skip apply).",
    )
    ap.add_argument(
        "--apply-only",
        action="store_true",
        help="Run the apply pass only (requires an existing manifest).",
    )
    ap.add_argument(
        "--restore-decompile",
        action="store_true",
        help="(deprecated) Undo decompile outputs (remove generated .rpy and extracted archive dirs).",
    )
    ap.add_argument(
        "--restore-compose",
        action="store_true",
        help="Undo compose/apply (restore .rpy files from archive.rpa).",
    )
    args = ap.parse_args(argv)

    return run_pipeline(
        args.game,
        dry_run=args.dry_run,
        decompile_only=args.decompile_only,
        compose_only=args.compose_only,
        apply_only=args.apply_only,
        pipeline=args.pipeline,
        restore_decompile=args.restore_decompile,
        restore_compose=args.restore_compose,
    )


if __name__ == "__main__":
    sys.exit(main())
