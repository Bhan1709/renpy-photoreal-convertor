"""Compose pass orchestration.

Responsibility: tie together parsing, resolution, condition evaluation, and
compositing to produce a :class:`~converter.compose.models.CompositeManifest`.

This is the main entry point for the compose pass: :class:`Composer` and its
:meth:`Composer.compose` method.

Compose v2 delegates extraction and rendering to the native Ren'Py stage
(:mod:`converter.compose.native.stage`), which renders through the game's own
displayables.  Screen-stack composites are still produced by the static Pass 3
logic, which the native stage runs for us.
"""

from __future__ import annotations

from ..config import GameConfig
from ..models import CompositeManifest
from ..native.constants import DEFAULT_MODES
from ..native.stage import _run_native_stage


class Composer:
    """Runs the full compose pass for a :class:`GameConfig`."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def compose(
        self, dry_run: bool = False, clean_output: bool = False
    ) -> CompositeManifest:
        """Run the compose pass and return a :class:`CompositeManifest`.

        Args:
            dry_run: If True, do not write PNGs or the manifest.
            clean_output: Accepted for API parity; actual cleanup is handled
                by the caller/CLI before this method is invoked.
        """
        result = _run_native_stage(
            self.config.source_path or self.config.game_root.parent,
            self.config.resolved_paths()["output_dir"],
            list(self.config.compose_modes or DEFAULT_MODES),
            config=self.config,
            dry_run=dry_run,
        )
        return result.manifest
