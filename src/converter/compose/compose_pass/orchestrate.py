"""Compose pass orchestration.

Responsibility: tie together parsing, resolution, condition evaluation, and
compositing to produce a :class:`~converter.compose.models.CompositeManifest`.

This is the main entry point for the compose pass: :class:`Composer` and its
:meth:`Composer.compose` method.

Record construction (_emit_record and helpers) lives in :mod:`.records`.
Composite deduplication and emission (_CompositeEmitter) lives in :mod:`.records.emitter`.
All compiled regular expressions live in :mod:`..regexp`.
"""

from __future__ import annotations

import json

from cli import counted, info, loading, progress_bar

from ..association import CharacterAssociator
from ..compositing import Compositor
from ..conditions import compute_interprocedural_label_states
from ..config import GameConfig
from ..models import CompositeManifest
from ..parsing import (
    index_scripts,
)
from ..records import _CompositeEmitter
from ..resolution import Resolver
from .counting import _count_composites
from .passes import (
    _process_image_declarations_from_index,
    _process_layeredimage_uses_from_index,
    _process_screen_stacks_from_index,
)


def _load_previous_dynamic_states(config: GameConfig) -> list[dict]:
    previous_manifest_path = config.resolved_paths()["manifest_path"]
    if previous_manifest_path.is_file():
        try:
            return json.loads(previous_manifest_path.read_text(encoding="utf-8")).get(
                "dynamic_states", []
            )
        except OSError, json.JSONDecodeError:
            return []
    return []


class Composer:
    """Runs the full compose pass for a :class:`GameConfig`."""

    def __init__(self, config: GameConfig) -> None:
        self.config = config
        self.resolver = Resolver(config)
        self.compositor = Compositor(config)
        self.associator = CharacterAssociator(config)

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
        with loading("Indexing game scripts and state..."):
            index = index_scripts(self.config)
            defaults = index.all_defaults
            definitions = index.layeredimage_defs
            screens = index.screens
            image_declarations = index.image_decls

            # Pre-compute interprocedural label entry states.  This tracks
            # variable assignments across label boundaries (e.g. ``call
            # undressHazel`` sets ``hazelScarf = 0``) so each ``show ha:``
            # usage gets the precise variable state rather than falling back
            # to game defaults.
            file_lines = {fi.path: fi.lines for fi in index.per_file.values()}
            label_entry_states = compute_interprocedural_label_states(
                defaults, file_lines
            )

        # Rebuild the associator with the indexed per-file lines so it never
        # re-reads scripts from disk (Optimization #1).
        self.associator = CharacterAssociator(
            self.config,
            script_lines=file_lines,
        )

        # Accurate total: count every emitter call up front (one per condition
        # variant, plus one per resolvable image declaration) so the percentage
        # bar and ETA track the real work instead of an upper bound.
        #
        # The count pass can take several seconds on large games, so show a
        # Rich loading spinner while it runs, then hand off to the Rich progress
        # bar for the actual composite processing.
        with loading("Counting composites..."):
            total = _count_composites(
                index,
                definitions,
                defaults,
                screens,
                image_declarations,
                self.config,
                label_entry_states,
            )

        info(f"Counted {counted(total, 'composite usage')} to process.")
        with progress_bar(
            total=total,
            desc="Composite usages processed",
            unit="usages",
        ) as bar:
            emitter = _CompositeEmitter(
                self.config,
                self.resolver,
                self.compositor,
                self.associator,
                index,
                bar,
                dry_run=dry_run,
            )

            # --- Pass 1: layeredimage show/scene statements ---
            _process_layeredimage_uses_from_index(
                index, definitions, defaults, emitter, self.config, label_entry_states
            )

            # --- Pass 2: ordinary image declarations ---
            dynamic_declarations: list[dict] = []
            _process_image_declarations_from_index(
                index, image_declarations, emitter, dynamic_declarations, self.config
            )

            # --- Pass 3: screen stacks ---
            _process_screen_stacks_from_index(
                index,
                screens,
                definitions,
                defaults,
                emitter,
                self.config,
                label_entry_states,
            )

        # --- Build manifest ---
        previous_dynamic_states = _load_previous_dynamic_states(self.config)
        declaration_records = [
            {
                "name": declaration.name,
                "script": declaration.script.relative_to(
                    self.config.game_root.parent
                ).as_posix(),
                "line": declaration.line,
                "expression": declaration.expression,
                "source_candidates": list(declaration.sources),
                "status": (
                    "dynamic_review"
                    if "ConditionSwitch" in declaration.expression
                    or "LiveComposite" in declaration.expression
                    else "declaration_candidate"
                ),
            }
            for declaration in sorted(image_declarations.values(), key=lambda d: d.name)
        ]

        return CompositeManifest(
            composites=tuple(emitter.records),
            dynamic_states=previous_dynamic_states,
            image_declarations=declaration_records,
            dynamic_declarations=dynamic_declarations,
            unresolved=(),
            summary={
                "scripts_scanned": len(index.files),
                "composites": len(emitter.records),
                "uses_processed": emitter.usages_processed,
                "image_declarations_indexed": len(declaration_records),
                "dynamic_declarations": len(dynamic_declarations),
            },
        )
