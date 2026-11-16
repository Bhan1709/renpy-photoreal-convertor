"""Compose pass orchestration.

Responsibility: tie together parsing, resolution, condition evaluation, and
compositing to produce a :class:`~converter.compose.models.CompositeManifest`.

This is the main entry point for the compose pass: :class:`Composer` and its
:meth:`Composer.compose` method.

Record construction (_emit_record and helpers) lives in :mod:`.record`.
Composite deduplication and emission (_CompositeEmitter) lives in :mod:`.emitter`.
All compiled regular expressions live in :mod:`.regexp`.
"""

from __future__ import annotations

import json
import re
from collections import defaultdict
from pathlib import Path

from tqdm import tqdm  # type: ignore[import-untyped]

from .character_associator import CharacterAssociator
from .compositor import Compositor
from .conditions import (
    evaluate_layer_conditions,
    select_layers_for_chain,
    state_at_call,
)
from .config import GameConfig
from .emitter import _CompositeEmitter
from .models import CompositeManifest, Layer
from .parser import (
    SHOW_SCENE_RE,
    ImageDeclaration,
    ScriptBlock,
    ScriptIndex,
    index_scripts,
    parse_screen_stack,
)
from .record import _restore_layer_order
from .resolver import Resolver


def _load_previous_dynamic_states(config: GameConfig) -> list[dict]:
    previous_manifest_path = config.resolved_paths()["manifest_path"]
    if previous_manifest_path.is_file():
        try:
            return json.loads(previous_manifest_path.read_text(encoding="utf-8")).get("dynamic_states", [])
        except (OSError, json.JSONDecodeError):
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

    def compose(self, dry_run: bool = False, clean_output: bool = False) -> CompositeManifest:
        """Run the compose pass and return a :class:`CompositeManifest`.

        Args:
            dry_run: If True, do not write PNGs or the manifest.
            clean_output: Accepted for API parity; actual cleanup is handled
                by the caller/CLI before this method is invoked.
        """
        index = index_scripts(self.config)
        defaults = index.all_defaults
        definitions = index.layeredimage_defs
        screens = index.screens
        image_declarations = index.image_decls

        # Rebuild the associator with the indexed per-file lines so it never
        # re-reads scripts from disk (Optimization #1).
        self.associator = CharacterAssociator(
            self.config,
            script_lines={fi.path: fi.lines for fi in index.per_file.values()},
        )

        # The total is constant for the whole compose run: it is the number of
        # show/scene/show-screen usage statements across every indexed script.
        # Every call to the emitter updates the bar exactly once, so the bar
        # shows % complete + ETA and never needs a final correction.
        total_usages = sum(len(fi.usages) for fi in index.per_file.values())
        bar = tqdm(total=total_usages, desc="composites processed")

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
            index, definitions, defaults, emitter, self.config
        )

        # --- Pass 2: ordinary image declarations ---
        dynamic_declarations: list[dict] = []
        _process_image_declarations_from_index(
            index, image_declarations, emitter, dynamic_declarations, self.config
        )

        # --- Pass 3: screen stacks ---
        _process_screen_stacks_from_index(
            index, screens, definitions, defaults, emitter, self.config
        )

        bar.close()

        # --- Build manifest ---
        previous_dynamic_states = _load_previous_dynamic_states(self.config)
        declaration_records = [
            {
                "name": declaration.name,
                "script": declaration.script.relative_to(self.config.game_root.parent).as_posix(),
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


# ---------------------------------------------------------------------- #
# Pass 1: layeredimage show/scene statements
# ---------------------------------------------------------------------- #

def _process_layeredimage_uses_from_index(
    index: ScriptIndex,
    definitions: dict[str, tuple[ScriptBlock, list[Layer]]],
    defaults: dict[str, object],
    emit_wrapper,
    config: GameConfig,
) -> None:
    """Pass 1: emit layeredimage composites for show/scene uses of defined tags.

    Reads each script exactly once (via the single-pass index) instead of
    re-scanning every file line-by-line (Optimization #1).
    """
    for file_index in index.per_file.values():
        f = file_index.path
        for usage in file_index.usages:
            if usage.kind != "show_scene":
                continue
            m = SHOW_SCENE_RE.search(usage.statement)
            if not m or m.group(1) not in definitions:
                continue
            tag, tail = m.group(1), (m.group(2) or "")
            block, candidates = definitions[tag]
            _emit_layeredimage_uses(
                tag,
                tail,
                block,
                candidates,
                f,
                usage.line,
                usage.statement,
                defaults,
                emit_wrapper,
                config,
                lines=file_index.lines,
            )


def _enumerate_condition_variants(
    unchained: list[Layer],
    chained: dict[str, list[Layer]],
    values: dict[str, object],
) -> list[list[Layer]]:
    """
    Enumerate all feasible variant layer sets when some conditions are
    statically unresolved. Each variant is a list of layers that would be
    composited together for one possible runtime condition outcome.

    Unchained layers with unknown conditions branch (include/exclude).
    Chained layers (if/elif/else) produce one variant per possible branch.
    """
    # Base: layers that are always included (no conditions, or conditions
    # that statically evaluate to True).
    base: list[Layer] = []

    # Unchained layers whose conditions are statically unknown.
    unchained_unknown: list[tuple[Layer, tuple[str, ...]]] = []

    for layer in unchained:
        outcome, unresolved = evaluate_layer_conditions(layer, values)
        if outcome is True:
            base.append(layer)
        elif outcome is None:
            unchained_unknown.append((layer, tuple(unresolved)))
        # outcome is False: layer is dropped from every variant.

    # Per-chain branch options. Each chain is an independent if/elif/else
    # group; we produce one option per feasible branch outcome.
    chain_options: list[list[list[Layer]]] = []

    for chain_layers in chained.values():
        # Separate else (no conditions) from conditional layers.
        else_layers = [l for l in chain_layers if not l.conditions]
        cond_layers = [l for l in chain_layers if l.conditions]

        true_layers: list[Layer] = []
        unknown_layers: list[tuple[Layer, tuple[str, ...]]] = []

        for layer in cond_layers:
            outcome, chain_unresolved = evaluate_layer_conditions(layer, values)
            if outcome is True:
                true_layers.append(layer)
            elif outcome is None:
                unknown_layers.append((layer, tuple(chain_unresolved)))
            # outcome is False: layer is dropped.

        if true_layers:
            # Some conditions are True — only those layers are selected.
            # Else layers are excluded (if/elif takes priority).
            chain_options.append([true_layers])
        elif unknown_layers:
            # All conditional conditions unknown — one variant per branch.
            options = [[layer] for layer, _ in unknown_layers]
            if else_layers:
                options.append(list(else_layers))  # else/fallback variant
            chain_options.append(options)
        elif else_layers:
            # Only else layers — always selected.
            chain_options.append([list(else_layers)])
        else:
            chain_options.append([[]])

    # Cartesian product of all branch choices.
    variants: list[list[Layer]] = [list(base)]

    for layer, _ in unchained_unknown:
        new_variants = []
        for v in variants:
            new_variants.append(list(v))                       # without this layer
            new_variants.append(list(v) + [layer])             # with this layer
        variants = new_variants

    for options in chain_options:
        new_variants = []
        for v in variants:
            for option in options:
                new_variants.append(list(v) + list(option))
        variants = new_variants

    return variants


def _emit_layeredimage_uses(
    tag: str, tail: str, block: ScriptBlock, candidates: list[Layer],
    f: Path, no: int, statement: str, defaults: dict[str, object],
    emit_wrapper, config: GameConfig,
    lines: tuple[str, ...] | list[str] | None = None,
) -> None:
    attr_tokens = set(re.findall(r"\b[a-zA-Z][\w]*\b", tail))
    valid_attrs = {x.attribute for x in candidates if x.attribute}
    attrs = attr_tokens & valid_attrs
    groups = {x.group for x in candidates if x.group}
    chosen_attrs = set(attrs)
    for group in groups:
        group_candidates = [x for x in candidates if x.group == group]
        explicit = {x.attribute for x in group_candidates if x.attribute in attrs}
        defaults_for_group = {x.attribute for x in group_candidates if x.default_attribute}
        chosen_attrs.update(explicit or defaults_for_group)
    selected = [
        x for x in candidates if not x.attribute or x.attribute in chosen_attrs
    ]
    if not selected:
        return

    call_state = state_at_call(f, no, defaults, config, lines=lines)

    chained: dict[str, list[Layer]] = defaultdict(list)
    unchained: list[Layer] = []
    for layer in selected:
        cid = layer.chain_id
        if cid is not None:
            chained[cid].append(layer)
        else:
            unchained.append(layer)

    # Evaluate unchained layers: only True conditions are selected.
    # UNKNOWN conditions are recorded but the layer is NOT selected.
    resolved: list[Layer] = []
    unresolved_conditions: list[str] = []
    for layer in unchained:
        outcome, layer_unresolved = evaluate_layer_conditions(layer, call_state.values)
        if outcome is True:
            resolved.append(layer)
        elif outcome is None:
            unresolved_conditions.extend(layer_unresolved)

    # Evaluate chained layers via select_layers_for_chain.
    for cid, chain_layers in chained.items():
        chain_selected, chain_unresolved = select_layers_for_chain(chain_layers, call_state)
        unresolved_conditions.extend(chain_unresolved)
        resolved.extend(chain_selected)

    # If there are unresolved conditions, generate one static composite per
    # feasible runtime condition outcome instead of a single dynamic_review
    # superset.  Each variant gets status="static" and can be selected at
    # runtime by a ConditionSwitch built by the apply pass.
    if unresolved_conditions:
        variants = _enumerate_condition_variants(unchained, chained, call_state.values)
        locations = [
            {
                "script": f.relative_to(config.game_root.parent).as_posix(),
                "line": no,
                "statement": statement,
            }
        ]
        for variant_layers in variants:
            variant_layers = _restore_layer_order(variant_layers, selected)
            emit_wrapper(tag, "layeredimage", variant_layers, locations, block)
        return

    resolved = _restore_layer_order(resolved, selected)

    locations = [
        {
            "script": f.relative_to(config.game_root.parent).as_posix(),
            "line": no,
            "statement": statement,
        }
    ]
    if resolved:
        emit_wrapper(tag, "layeredimage", resolved, locations, block)


# ---------------------------------------------------------------------- #
# Pass 2: image declarations
# ---------------------------------------------------------------------- #

class _DeclBlock:
    """Minimal stand-in for a ScriptBlock for image declarations."""

    def __init__(self, path: Path, line: int) -> None:
        self.path = path
        self.line = line


def _process_image_declarations_from_index(
    index: ScriptIndex,
    image_declarations: dict[str, ImageDeclaration],
    emit_wrapper,
    dynamic_declarations: list[dict],
    config: GameConfig,
) -> None:
    """Pass 2: emit composites for show/scene uses of image declarations.

    Consumes the single-pass index instead of re-reading every script
    (Optimization #1).
    """
    path_regex = config.path_regex()

    # Pre-parse static declaration expressions once (canvas size + offsets) so
    # declarations used in many places do not re-run the regexes per usage.
    parsed_decls: dict[str, list[Layer] | None] = {}
    for name, decl in image_declarations.items():
        expression = decl.expression
        dynamic = any(
            token in expression
            for token in ("ConditionSwitch", "LiveComposite", "Function(", "DynamicDisplayable")
        )
        paths = decl.sources
        if dynamic or not paths:
            parsed_decls[name] = None
            continue
        decl_layers = [
            Layer(
                source=p, line=decl.line, role="image_declaration_layer",
                conditions=(), declaration_expression=expression,
            )
            for p in paths
        ]
        comp = re.search(r"Composite\s*\(\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", expression)
        if comp:
            canvas_size = (int(comp.group(1)), int(comp.group(2)))
            decl_layers = [l.replace(canvas_size=canvas_size) for l in decl_layers]
        position_pairs = list(
            re.finditer(
                r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*,\s*['\"]("
                + path_regex.pattern[1:-1] + r")['\"]",
                expression,
            )
        )
        for pair_index, match in enumerate(position_pairs):
            if pair_index < len(decl_layers):
                decl_layers[pair_index] = decl_layers[pair_index].replace(
                    offset=(int(match.group(1)), int(match.group(2)))
                )
        parsed_decls[name] = decl_layers

    for file_index in index.per_file.values():
        f = file_index.path
        for usage in file_index.usages:
            if usage.kind != "show_scene":
                continue
            name = usage.tag
            if name not in image_declarations:
                continue
            decl = image_declarations[name]
            expression = decl.expression
            loc = {
                "script": f.relative_to(config.game_root.parent).as_posix(),
                "line": usage.line,
                "statement": usage.statement,
            }
            dynamic = any(
                token in expression
                for token in ("ConditionSwitch", "LiveComposite", "Function(", "DynamicDisplayable")
            )
            if dynamic:
                dynamic_declarations.append({
                    "name": name,
                    "declaration": {
                        "script": decl.script.relative_to(config.game_root.parent).as_posix(),
                        "line": decl.line,
                    },
                    "usage_locations": [loc],
                    "expression": expression,
                    "source_candidates": list(decl.sources),
                    "status": "dynamic_review",
                    "reason": "runtime-dependent image declaration",
                })
                continue
            layers: list[Layer] | None = parsed_decls.get(name)
            if layers is None:
                continue
            emit_wrapper(name, "image_declaration", layers, [loc], _DeclBlock(decl.script, decl.line))


# ---------------------------------------------------------------------- #
# Pass 3: screen stacks
# ---------------------------------------------------------------------- #

def _process_screen_stacks_from_index(
    index: ScriptIndex,
    screens: dict[str, ScriptBlock],
    definitions: dict[str, tuple[ScriptBlock, list[Layer]]],
    defaults: dict[str, object],
    emit_wrapper,
    config: GameConfig,
) -> None:
    """Pass 3: emit composites for show screen uses of defined screens.

    Consumes the single-pass index instead of re-reading every script
    (Optimization #1).
    """
    # Parse screen stacks lazily: only screens that are actually referenced by
    # a ``show screen`` statement are parsed, avoiding work for unused screens.
    screen_stack_cache: dict[str, list[Layer]] = {}
    for file_index in index.per_file.values():
        f = file_index.path
        for usage in file_index.usages:
            if usage.kind != "show_screen":
                continue
            name = usage.tag
            if name not in screens:
                continue
            if name not in screen_stack_cache:
                screen_stack_cache[name] = parse_screen_stack(
                    screens[name], definitions, config
                )
            block = screens[name]
            stack = list(screen_stack_cache[name])  # shallow copy
            if stack:
                call_state = state_at_call(
                    f, usage.line, defaults, config, lines=file_index.lines
                )
                selected: list[Layer] = []
                unknown_conditions: list[str] = []

                chained: dict[str, list[Layer]] = defaultdict(list)
                unchained: list[Layer] = []
                for layer in stack:
                    cid = layer.chain_id
                    if cid is not None:
                        chained[cid].append(layer)
                    else:
                        unchained.append(layer)

                # Evaluate unchained layers: only True conditions are selected.
                # UNKNOWN conditions are recorded but the layer is NOT selected.
                for layer in unchained:
                    outcome, layer_unresolved = evaluate_layer_conditions(
                        layer, call_state.values
                    )
                    if outcome is True:
                        selected.append(layer)
                    elif outcome is None:
                        unknown_conditions.extend(layer_unresolved)

                # Evaluate chained layers via select_layers_for_chain.
                for cid, chain_layers in chained.items():
                    chain_selected, chain_unresolved = select_layers_for_chain(
                        chain_layers, call_state
                    )
                    unknown_conditions.extend(chain_unresolved)
                    selected.extend(chain_selected)

                # If there are unknown conditions, generate one static composite per
                # feasible runtime condition outcome instead of a single dynamic_review
                # superset.  Each variant gets status="static" and can be selected at
                # runtime by a ConditionSwitch built by the apply pass.
                if unknown_conditions:
                    variants = _enumerate_condition_variants(unchained, chained, call_state.values)
                    relevant_names = {
                        used_name
                        for layer in stack
                        for condition in layer.conditions
                        for used_name in re.findall(r"\b[A-Za-z_]\w*\b", condition)
                        if used_name not in {"True", "False", "and", "or", "not"}
                    }
                    state_snapshot = {
                        key: call_state.values[key]
                        for key in sorted(relevant_names)
                        if key in call_state.values
                    }
                    locations = [
                        {
                            "script": f.relative_to(config.game_root.parent).as_posix(),
                            "line": usage.line,
                            "statement": usage.statement,
                            "state_snapshot": state_snapshot,
                        }
                    ]
                    for variant_layers in variants:
                        variant_layers = _restore_layer_order(variant_layers, stack)
                        emit_wrapper(
                            name,
                            "screen",
                            variant_layers,
                            locations,
                            block,
                        )
                    continue

                selected = _restore_layer_order(selected, stack)

                if selected:
                    relevant_names = {
                        used_name
                        for layer in stack
                        for condition in layer.conditions
                        for used_name in re.findall(r"\b[A-Za-z_]\w*\b", condition)
                        if used_name not in {"True", "False", "and", "or", "not"}
                    }
                    state_snapshot = {
                        key: call_state.values[key]
                        for key in sorted(relevant_names)
                        if key in call_state.values
                    }
                    emit_wrapper(
                        name,
                        "screen",
                        selected,
                        [
                            {
                                "script": f.relative_to(config.game_root.parent).as_posix(),
                                "line": usage.line,
                                "statement": usage.statement,
                                "state_snapshot": state_snapshot,
                            }
                        ],
                        block,
                    )
