"""Command-line entry point for the ``converter.compose`` subpackage.

Runs the compose pass (manifest + PNG generation) followed by the apply pass
(adopting composites into live Ren'Py scripts) for a single Ren'Py game.

All modes are config-driven via ``src/converter/config.json`` (produced by
decompile); no ``--game`` lookup is needed.

Usage::

    python -m converter.compose.cli --config src/converter/config.json
    python -m converter.compose.cli --config mygame/config.json --compose-only
    python -m converter.compose.cli --config mygame/config.json --apply-only
    python -m converter.compose.cli --config mygame/config.json --restore-compose
"""

from __future__ import annotations

import argparse
import shutil
import sys

from converter.restore import restore_from_manifest

from .applier import Applier
from .composer import Composer
from .config import GameConfig, load_config
from .errors import ComposeError
from .manifest import ManifestWriter


def build_config(args) -> GameConfig:
    """Resolve a :class:`GameConfig`` from CLI arguments.

    Requires an explicit ``--config`` JSON file produced by decompile
    (``src/converter/config.json``).  No ``--game`` lookup is performed.
    """
    if not args.config:
        raise ComposeError("--config is required; pass src/converter/config.json")
    return load_config(args.config)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Compose and adopt photorealistic composites for a Ren'Py game.",
    )
    ap.add_argument(
        "--config",
        required=True,
        help="Path to the decompile-generated GameConfig JSON (src/converter/config.json)",
    )
    ap.add_argument(
        "--output-dir",
        help="Directory for generated composite PNGs",
    )
    ap.add_argument("--manifest", help="Path for the manifest JSON report")
    ap.add_argument("--dry-run", action="store_true", help="Do not write PNGs or manifest")
    ap.add_argument("--clean-output", action="store_true",
                    help="Remove prior generated PNGs and manifest before rendering")
    ap.add_argument("--compose-only", action="store_true",
                    help="Run the compose pass only; skip script adoption")
    ap.add_argument("--apply-only", action="store_true",
                    help="Run the apply pass only (requires an existing manifest)")
    # Restore operations
    ap.add_argument("--restore-compose", action="store_true",
                    help="Restore .rpy files touched by the apply pass (per manifest)")

    args = ap.parse_args(argv)

    config = build_config(args)

    # --- Restore-compose mode (standalone, skip compose/apply) ---
    if args.restore_compose:
        results = restore_from_manifest(config, dry_run=args.dry_run)
        action = "Would restore" if args.dry_run else "Restored"
        print(f"{action} {len(results)} .rpy file(s) from manifest:")
        for rel, status in sorted(results.items()):
            print(f"  {rel}: {status}")
        return 0

    # --- Normal compose + apply flow ---
    if not args.apply_only:
        if args.clean_output:
            paths = config.resolved_paths()
            output_dir = paths["output_dir"]
            if output_dir.exists():
                shutil.rmtree(output_dir)
                print(f"Cleaned output directory: {output_dir}")
        composer = Composer(config)
        manifest = composer.compose(dry_run=args.dry_run, clean_output=args.clean_output)
        ManifestWriter(config).write(manifest, dry_run=args.dry_run)
        if args.compose_only or args.dry_run:
            return 0

    applier = Applier(config)
    applier.apply()
    return 0


if __name__ == "__main__":
    sys.exit(main())
