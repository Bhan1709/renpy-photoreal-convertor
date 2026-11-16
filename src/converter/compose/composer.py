"""Compose pass orchestration.

Responsibility: tie together parsing, resolution, condition evaluation, and
compositing to produce a :class:`~converter.compose.models.CompositeManifest`.

This is the main entry point for the compose pass: :class:`Composer` and its
:meth:`Composer.compose` method.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path

from tqdm import tqdm
from .character_associator import CharacterAssociator
from .compositor import Compositor
from .conditions import eval_cond, select_layers_for_chain, state_at_call
from .config import GameConfig
from .errors import ComposeError
from .models import (
    CompositeManifest,
    CompositeRecord,
    DefinitionLocation,
    Layer,
    UsageLocation,
)
from .parser import (
    FileIndex,
    ScriptBlock,
    ScriptIndex,
    ScriptUsage,
    find_nearby_dialog,
    index_scripts,
    parse_screen_stack,
)
from .resolver import Resolver


_SHOW_RE = re.compile(r"^\s*(?:show|scene)\s+(?!screen\b)([\w.]+)(?:\s+([^\n#]+))?")
_DECLARATION_USE_RE = re.compile(r"^\s*(?:show|scene)\s+([\w.]+)")


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

        # We cannot know up front exactly how many usages will pass through
        # emit_wrapper (some are skipped because the tag/screen is undefined,
        # the stack is empty, or the declaration is dynamic), so we start the
        # progress bar without a fixed total.  It will show iterations
        # processed and rate; the final count is recorded in usages_processed.
        bar = tqdm(desc="composites processed")

        records: list[CompositeRecord] = []
        records_by_key: dict[tuple[str, str, str], CompositeRecord] = {}
        # Maps dedup key -> position in ``records`` list for O(1) replacement
        # instead of an O(n) linear scan calling _record_key() on every entry.
        key_to_index: dict[tuple[str, str, str], int] = {}
        use_count = 0
        usages_processed = 0

        def emit_wrapper(
            name: str, kind: str, layers: list[Layer], locations: list[dict], block: ScriptBlock,
        ) -> None:
            nonlocal use_count, usages_processed

            usages_processed += 1
            normalized = _normalize_layers(layers, self.config)
            if not normalized:
                bar.update(1)
                return
            key = _record_key(kind, name, normalized)
            digest = key[2]

            if key in records_by_key:
                # Duplicate composite: merge usage locations only.  Skip the
                # expensive render / association / dialog work, which was
                # already performed for the first occurrence of this key.
                existing = records_by_key[key]
                new_locs = [
                    loc for loc in locations
                    if (loc["script"], loc["line"]) not in
                    {(l.script, l.line) for l in existing.usage_locations}
                ]
                if new_locs:
                    existing_keys = {(l.script, l.line) for l in existing.usage_locations}
                    merged_list = list(existing.usage_locations)
                    for loc in new_locs:
                        k = (loc["script"], loc["line"])
                        if k not in existing_keys:
                            merged_list.append(_loc_to_usage(loc))
                            existing_keys.add(k)
                    merged = tuple(sorted(merged_list, key=lambda l: (l.script, l.line)))
                    existing = existing.replace(usage_locations=merged)
                    records_by_key[key] = existing
                    # O(1) replacement via key->index map instead of O(n) scan
                    idx = key_to_index.get(key)
                    if idx is not None:
                        records[idx] = existing
                bar.update(1)
                return

            record = _emit_record(
                name, kind, normalized, locations, block, index, self.config, self.resolver,
                self.compositor, self.associator, dry_run=dry_run, digest=digest,
            )
            if record is None:
                bar.update(1)
                return
            records_by_key[key] = record
            key_to_index[key] = len(records)
            records.append(record)
            use_count += 1
            bar.update(1)

        # --- Pass 1: layeredimage show/scene statements ---
        _process_layeredimage_uses_from_index(
            index, definitions, defaults, emit_wrapper, self.config
        )

        # --- Pass 2: ordinary image declarations ---
        dynamic_declarations: list[dict] = []
        _process_image_declarations_from_index(
            index, image_declarations, emit_wrapper, dynamic_declarations, self.config
        )

        # --- Pass 3: screen stacks ---
        _process_screen_stacks_from_index(
            index, screens, definitions, defaults, emit_wrapper, self.config
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
            composites=tuple(records),
            dynamic_states=previous_dynamic_states,
            image_declarations=declaration_records,
            dynamic_declarations=dynamic_declarations,
            unresolved=tuple(),
            summary={
                "scripts_scanned": len(index.files),
                "composites": len(records),
                "uses_processed": usages_processed,
                "image_declarations_indexed": len(declaration_records),
                "dynamic_declarations": len(dynamic_declarations),
            },
        )


# ---------------------------------------------------------------------- #
# Helpers
# ---------------------------------------------------------------------- #

def _loc_to_usage(loc: dict) -> UsageLocation:
    return UsageLocation(
        script=loc["script"], line=loc["line"], statement=loc["statement"],
        state_snapshot=loc.get("state_snapshot"),
    )


def _normalize_layers(layers: list[Layer], config: GameConfig) -> list[Layer]:
    """Keep only layers whose source lives under a configured asset folder.

    Pure filtering/copying — no disk or image work — so it is cheap to run for
    every usage before the dedup check.
    """
    prefixes = tuple(f"{n}/" for n in config.categories.values())
    return [
        Layer(
            source=x.source, line=x.line, group=x.group, conditions=x.conditions,
            attribute=x.attribute, default_attribute=x.default_attribute,
            offset=x.offset, offset_rel=x.offset_rel, chain_id=x.chain_id,
            resolved_path=x.resolved_path, resolution_method=x.resolution_method,
            warning=x.warning, unresolved_conditions=x.unresolved_conditions,
            via_image_tag=x.via_image_tag, role=x.role, canvas_size=x.canvas_size,
            declaration_expression=x.declaration_expression,
        )
        for x in layers
        if any(x.source.lower().startswith(p) for p in prefixes)
    ]


def _restore_layer_order(layers: list[Layer], source_order: list[Layer]) -> list[Layer]:
    """Restore declaration/stack order after conditional layers were grouped.

    The condition-selection pass separates unconditional layers from ``if``
    chains for evaluation. Emitting those buckets directly changes z-order:
    conditional layers that were declared before facial attributes can end up
    on top of them. Match selected layers back to their original positions.
    """
    def key(layer: Layer) -> tuple:
        return (
            layer.source, layer.line, layer.group, layer.conditions,
            layer.attribute, layer.chain_id, layer.via_image_tag, layer.role,
        )

    positions: dict[tuple, int] = {}
    for index, layer in enumerate(source_order):
        positions.setdefault(key(layer), index)
    return sorted(layers, key=lambda layer: positions.get(key(layer), len(positions)))


def _record_key(kind: str, name: str, normalized: list[Layer]) -> tuple[str, str, str]:
    """Dedup key for a composite: (kind, name, sha1 of normalized layer semantics)."""
    semantic = json.dumps(
        [
            kind,
            name,
            [
                (
                    x.source,
                    list(x.conditions),
                    x.attribute,
                    x.offset,
                    x.offset_rel,
                    x.canvas_size,
                )
                for x in normalized
            ],
        ],
        sort_keys=True,
    )
    digest = hashlib.sha1(semantic.encode()).hexdigest()[:12]
    return (kind, name, digest)


def _require_config() -> GameConfig:
    raise ComposeError("compose config not initialized; pass a GameConfig explicitly")


def _load_previous_dynamic_states(config: GameConfig) -> list[dict]:
    previous_manifest_path = config.resolved_paths()["manifest_path"]
    if previous_manifest_path.is_file():
        try:
            return json.loads(previous_manifest_path.read_text(encoding="utf-8")).get("dynamic_states", [])
        except (OSError, json.JSONDecodeError):
            return []
    return []


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
            m = _SHOW_RE.search(usage.statement)
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
    from .conditions import eval_cond

    # Base: layers that are always included (no conditions, or conditions
    # that statically evaluate to True).
    base: list[Layer] = []

    # Unchained layers whose conditions are statically unknown.
    unchained_unknown: list[tuple[Layer, tuple[str, ...]]] = []

    for layer in unchained:
        if not layer.conditions:
            base.append(layer)
            continue
        outcome = True
        unresolved: list[str] = []
        for condition in layer.conditions:
            value = eval_cond(condition, values)
            if value is None:  # UNKNOWN
                unresolved.append(condition)
                outcome = None  # type: ignore[assignment]
                break
            if value is False:
                outcome = False
                break
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
            outcome = True
            unresolved: list[str] = []
            for condition in layer.conditions:
                value = eval_cond(condition, values)
                if value is None:  # UNKNOWN
                    unresolved.append(condition)
                    outcome = None  # type: ignore[assignment]
                    break
                if value is False:
                    outcome = False
                    break
            if outcome is True:
                true_layers.append(layer)
            elif outcome is None:
                unknown_layers.append((layer, tuple(unresolved)))
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
        result = True
        layer_unresolved: list[str] = []
        for condition in layer.conditions:
            outcome = eval_cond(condition, call_state.values)
            if outcome is None:  # UNKNOWN
                layer_unresolved.append(condition)
                result = None  # type: ignore[assignment]
                break
            if outcome is False:
                result = False
                break
        if result is True:
            resolved.append(layer)
        elif result is None:
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
        layers = [
            Layer(
                source=p, line=decl.line, role="image_declaration_layer",
                conditions=(), declaration_expression=expression,
            )
            for p in paths
        ]
        comp = re.search(r"Composite\s*\(\s*\(\s*(\d+)\s*,\s*(\d+)\s*\)", expression)
        if comp:
            canvas_size = (int(comp.group(1)), int(comp.group(2)))
            layers = [l.replace(canvas_size=canvas_size) for l in layers]
        position_pairs = list(
            re.finditer(
                r"\(\s*(-?\d+)\s*,\s*(-?\d+)\s*\)\s*,\s*['\"]("
                + path_regex.pattern[1:-1] + r")['\"]",
                expression,
            )
        )
        for pair_index, match in enumerate(position_pairs):
            if pair_index < len(layers):
                layers[pair_index] = layers[pair_index].replace(
                    offset=(int(match.group(1)), int(match.group(2)))
                )
        parsed_decls[name] = layers

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
            layers = parsed_decls.get(name)
            if not layers:
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
                    outcome = True
                    layer_unresolved: list[str] = []
                    for condition in layer.conditions:
                        value = eval_cond(condition, call_state.values)
                        if value is None:  # UNKNOWN
                            layer_unresolved.append(condition)
                            outcome = None  # type: ignore[assignment]
                            break
                        if value is False:
                            outcome = False
                            break
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


# ---------------------------------------------------------------------- #
# Record emission (shared by all passes)
# ---------------------------------------------------------------------- #

def _emit_record(
    name: str, kind: str, normalized: list[Layer], locations: list[dict], block: ScriptBlock,
    index: ScriptIndex, config: GameConfig, resolver: Resolver, compositor: Compositor,
    associator: CharacterAssociator, *,
    dry_run: bool = False, digest: str | None = None,
) -> CompositeRecord | None:
    """Register a composite record (and optionally render its PNG).

    ``normalized`` must already be the prefix-filtered layer list; the dedup
    key is computed by the caller, so this function only performs the
    per-unique-composite work (render / association / dialog) once.
    Returns ``None`` if the stack could not be normalized to a configured
    asset folder or rendering failed.
    """
    dynamic = any(x.unresolved_conditions for x in normalized)

    folder = next(
        (x.source.split("/")[:-1] for x in normalized if "/" in x.source), [kind]
    )
    family = config.category_for_folder(folder[0]) if folder and folder[0] else kind
    if family is None:
        family = folder[0] if folder else kind
    sub = Path(*folder[1:]) if len(folder) > 1 else Path()
    status = "dynamic_review" if dynamic else "static"
    paths = config.resolved_paths()
    digest = digest or _record_key(kind, name, normalized)[2]
    target = paths["output_dir"] / status / family / sub / f"{name}__{digest}.png"
    dimensions: tuple[int, int] | None = None

    # The digest above now encodes offsets, offset_rel, and canvas size, so the
    # PNG filename is a self-validating cache key: if the output file exists,
    # its inputs are unchanged and PIL compositing can be skipped entirely.
    # No separate .hash sidecar is needed.
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_file():
            # Re-read dimensions from the existing PNG without re-compositing.
            try:
                from PIL import Image as _PILImage

                with _PILImage.open(target) as _im:
                    dimensions = _im.size
            except (OSError, Exception):
                dimensions = None
        else:
            image = compositor.composite(normalized, canvas_size=None)
            if image is None:
                return None
            dimensions = (int(image.size[0]), int(image.size[1]))
            image.save(target)

    family2 = ""
    for x in normalized:
        if "/" in x.source:
            f2 = config.category_for_folder(x.source.split("/", 1)[0].lower())
            if f2 is None:
                f2 = x.source.split("/", 1)[0].lower()
            family2 = f2
            break

    scene_folder = config.folder_for("scene").lower()
    is_xxx = any(
        x.source.split("/", 1)[0].lower() == scene_folder
        for x in normalized if "/" in x.source
    )

    usage_locations = tuple(
        UsageLocation(
            script=loc["script"], line=loc["line"], statement=loc["statement"],
            state_snapshot=loc.get("state_snapshot"),
        )
        for loc in locations
    )

    record = CompositeRecord(
        composite_path=target.relative_to(config.game_root.parent).as_posix(),
        name=name,
        kind=kind,
        status=status,
        definition=DefinitionLocation(
            script=block.path.relative_to(config.game_root.parent).as_posix(),
            line=block.line,
        ),
        usage_locations=usage_locations,
        ordered_layers=tuple(normalized),
        dimensions=dimensions,
        dynamic_conditions=tuple(
            sorted({c for x in normalized for c in x.unresolved_conditions})
        ),
        resolved_conditions=tuple(
            sorted({c for x in normalized for c in x.conditions})
        ),
        family="xxx" if is_xxx else (family2 or kind),
    )

    if is_xxx:
        char_locs = [{"script": l.script, "line": l.line, "statement": l.statement} for l in usage_locations]
        file_index = index.per_file.get(block.path)
        chars = associator.associate(
            list(normalized), name, block, char_locs,
            lines=file_index.lines if file_index is not None else None,
        )
        record = record.replace(
            characters=tuple(chars),
            character_association_status="associated" if chars else "missing",
            family="xxx",
        )
    elif family2 == "char":
        record = record.replace(family="char")
        char_folder = None
        for x in normalized:
            parts = x.source.split("/", 1)
            if len(parts) > 1 and parts[0].lower() == "chars":
                char_folder = parts[1].split("/", 1)[0]
                break
        if char_folder:
            record = record.replace(character_name=associator.display_name(char_folder))
    elif family2 == "scene":
        record = record.replace(family="scene")
        file_index = index.per_file.get(block.path)
        dialog = find_nearby_dialog(
            block.path,
            block.line,
            lines=list(file_index.lines) if file_index is not None else None,
        )
        if dialog:
            record = record.replace(
                dialog_context=tuple(
                    {"speaker": d.speaker, "text": d.text, "line": d.line}
                    for d in dialog
                )
            )

    return record
