"""Command-line entry point for the ``converter.compose`` subpackage (typer + rich).

Runs the compose pass (manifest + PNG generation) followed by the apply pass
(adopting composites into live Ren'Py scripts) for a single Ren'Py game.

All modes are config-driven via ``src/converter/config.json`` (produced by
decompile); no ``--game`` lookup is needed.

Usage::

    python -m converter.compose --config src/converter/config.json
    python -m converter.compose --config mygame/config.json --compose-only
    python -m converter.compose --config mygame/config.json --apply-only
    python -m converter.compose --config mygame/config.json --restore-compose
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import typer

from cli import (
    console,
    create_app,
    info,
    kv,
    rule,
    step,
    substep,
    success,
    warning,
)

from .apply_pass import Applier
from .compose_pass import Composer
from .config import GameConfig, load_config
from .manifest import ManifestWriter

app = create_app("Compose and adopt photorealistic composites for a Ren'Py game.")


def build_config(config_path: Path) -> GameConfig:
    """Resolve a :class:`GameConfig` from a config file path.

    Requires an explicit config JSON file produced by decompile
    (``src/converter/config.json``).  No ``--game`` lookup is performed.
    """
    return load_config(config_path)


@app.command()
def main(
    config: Path = typer.Argument(  # noqa: B008
        ...,
        help="Path to the decompile-generated GameConfig JSON (src/converter/config.json)",
    ),
    output_dir: Path | None = typer.Option(  # noqa: B008
        None,
        "--output-dir",
        help="Directory for generated composite PNGs",
    ),
    manifest: Path | None = typer.Option(  # noqa: B008
        None,
        "--manifest",
        help="Path for the manifest JSON report",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Do not write PNGs or manifest"
    ),
    clean_output: bool = typer.Option(
        False,
        "--clean-output",
        help="Remove prior generated PNGs and manifest before rendering",
    ),
    compose_only: bool = typer.Option(
        False,
        "--compose-only",
        help="Run the compose pass only; skip script adoption",
    ),
    apply_only: bool = typer.Option(
        False,
        "--apply-only",
        help="Run the apply pass only (requires an existing manifest)",
    ),
    restore_compose: bool = typer.Option(
        False,
        "--restore-compose",
        help="Restore .rpy files touched by the apply pass (per manifest)",
    ),
) -> int:
    """Compose and adopt photorealistic composites for a Ren'Py game."""
    config_obj = build_config(config)
    rule(f"Compose: {config_obj.name}")
    kv("Config", str(config))

    # --- Restore-compose mode (standalone, skip compose/apply) ---
    if restore_compose:
        rule("Restore compose")
        paths = config_obj.resolved_paths()
        from converter.restore import restore_from_manifest

        results = restore_from_manifest(
            config_obj.game_root, paths["manifest_path"], dry_run=dry_run
        )
        action = "Would restore" if dry_run else "Restored"
        success(f"{action} {len(results)} .rpy file(s) from manifest:")
        for rel, status in sorted(results.items()):
            console.print(f"  {rel}: {status}")
        return 0

    # --- Normal compose + apply flow ---
    if not apply_only:
        step("1", "Compose pass")
        if clean_output:
            paths = config_obj.resolved_paths()
            output_dir_resolved = paths["output_dir"]
            if output_dir_resolved.exists():
                shutil.rmtree(output_dir_resolved)
                success(f"Cleaned output directory: {output_dir_resolved}")
        composer = Composer(config_obj)
        manifest_obj = composer.compose(dry_run=dry_run, clean_output=clean_output)
        ManifestWriter(config_obj).write(manifest_obj, dry_run=dry_run)
        if compose_only or dry_run:
            rule("Compose complete (apply step deferred)")
            return 0

    # Block apply if there are pending scene reviews (scene composites without
    # character association).  The user must resolve them first, then re-run
    # compose to regenerate manifests and clear the pending list.
    pending = ManifestWriter.load_pending_review(config_obj)
    scenes = pending.get("scenes", [])
    speakers = pending.get("unmapped_speakers", [])
    if scenes:
        reports_dir = config_obj.resolved_paths()["manifest_path"].parent
        pending_path = reports_dir / "review_pending.json"
        warning(
            f"BLOCKED: {len(scenes)} scene composite(s) lack character association.\n"
            "Review and resolve them in:\n"
            f"  {pending_path}\n"
            "Re-run compose after resolving. Skipping apply pass.",
        )
        return 1

    # Suggest interactive review for any remaining pending items (unmapped
    # speakers, or scenes already associated but still pending).
    if scenes or speakers:
        console.print()
        info(
            f"Next step: review {len(scenes)} unresolved scene(s) and "
            f"{len(speakers)} unmapped speaker(s):\n"
            "  mise run compose-review\n"
            "or\n"
            f"  python -m converter.compose.review cli compose-review {config}"
        )

    substep("Apply pass", last=True)
    applier = Applier(config_obj)
    applier.apply()
    rule("Pipeline complete")
    return 0


if __name__ == "__main__":
    sys.exit(app())
