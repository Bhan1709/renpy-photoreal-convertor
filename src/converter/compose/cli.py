"""Command-line entry point for the ``converter.compose`` subpackage.

Runs the compose pass (manifest + PNG generation) followed by the apply pass
(adopting composites into live Ren'Py scripts) for a single Ren'Py game.

Usage::

    python -m converter.compose.cli --game CampMourningWood-0.25.0.1
    python -m converter.compose.cli --config mygame/config.json
    python -m converter.compose.cli --game-root /abs/path/to/game --output-dir /abs/path/out
"""
from __future__ import annotations

import argparse
import sys
from dataclasses import replace
from pathlib import Path

from .config import GameConfig, default_config, load_config
from .apply_composite_tags import apply as apply_pass, set_config as apply_set_config
from .compose_script_elements import compose, set_config as compose_set_config, write_manifest


def build_config(args) -> GameConfig:
    """Resolve a :class:`GameConfig` from CLI arguments.

    Priority: explicit --config JSON > --game-root / overrides > default convention.
    """
    if args.config:
        return load_config(args.config)

    game_root = Path(args.game_root).resolve() if args.game_root else None
    if game_root is None and args.game:
        game_root = (Path("src/renpy_game") / args.game).resolve()
        # Common Ren'Py build layout: <game>-<version>-pc/game
        candidates = [p for p in game_root.iterdir() if p.is_dir() and p.name.endswith("-pc")]
        if candidates:
            game_root = (candidates[0] / "game").resolve()
    if game_root is None:
        game_root = Path(
            "src/renpy_game/CampMourningWood-0.25.0.1/CampMourningWood-0.25.0.1-pc/game"
        ).resolve()

    archive_dir = Path(args.archive_dir).resolve() if args.archive_dir else None
    config = default_config(
        args.game or game_root.parent.name,
        game_root,
        archive_dir,
    )

    updates: dict[str, object] = {}
    if args.output_dir:
        updates["output_dir"] = Path(args.output_dir).resolve()
    if args.manifest:
        updates["manifest_path"] = Path(args.manifest).resolve()
    if updates:
        config = replace(config, **updates)
    return config


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Compose and adopt photorealistic composites for a Ren'Py game.",
    )
    ap.add_argument("--game", help="Game name under src/renpy_game (default convention)")
    ap.add_argument("--game-root", help="Absolute path to the Ren'Py \\game folder")
    ap.add_argument("--config", help="Path to a GameConfig JSON file")
    ap.add_argument("--archive-dir", help="Absolute path to the extracted asset archive")
    ap.add_argument("--output-dir", help="Directory for generated composite PNGs")
    ap.add_argument("--manifest", help="Path for the manifest JSON report")
    ap.add_argument("--dry-run", action="store_true", help="Do not write PNGs or manifest")
    ap.add_argument("--clean-output", action="store_true",
                    help="Remove prior generated PNGs and manifest before rendering")
    ap.add_argument("--compose-only", action="store_true",
                    help="Run the compose pass only; skip script adoption")
    ap.add_argument("--apply-only", action="store_true",
                    help="Run the apply pass only (requires an existing manifest)")
    args = ap.parse_args(argv)

    config = build_config(args)

    compose_set_config(config)
    apply_set_config(config)

    if not args.apply_only:
        manifest = compose(config, dry_run=args.dry_run, clean_output=args.clean_output)
        write_manifest(manifest, config, dry_run=args.dry_run)
        if args.compose_only:
            return 0

    apply_pass(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
