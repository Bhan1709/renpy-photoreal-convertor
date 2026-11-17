"""Compose pass processors (Pass 1/2/3).

Responsibility: iterate the single-pass script index and emit composites for
layeredimage uses, image declarations, and screen stacks.
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path

from ..conditions import condition_variables, state_at_call
from ..config import GameConfig
from ..models import (
    Layer,
)
from ..parsing import (
    SHOW_SCENE_RE,
    ImageDeclaration,
    ScriptBlock,
    ScriptIndex,
    parse_screen_stack,
)
from ..records import (
    _restore_layer_order,
)
from .variants import _enumerate_condition_variants


def _process_layeredimage_uses_from_index(
    index: ScriptIndex,
    definitions: dict[str, tuple[ScriptBlock, list[Layer]]],
    defaults: dict[str, object],
    emit_wrapper,
    config: GameConfig,
    label_entry_states: dict[str, dict[str, object]],
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
                label_entry_states,
                lines=file_index.lines,
            )


def _emit_layeredimage_uses(
    tag: str,
    tail: str,
    block: ScriptBlock,
    candidates: list[Layer],
    f: Path,
    no: int,
    statement: str,
    defaults: dict[str, object],
    emit_wrapper,
    config: GameConfig,
    label_entry_states: dict[str, dict[str, object]],
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
        defaults_for_group = {
            x.attribute for x in group_candidates if x.default_attribute
        }
        chosen_attrs.update(explicit or defaults_for_group)
    selected = [x for x in candidates if not x.attribute or x.attribute in chosen_attrs]
    if not selected:
        return

    call_state = state_at_call(
        f,
        no,
        defaults,
        config,
        lines=lines,
        label_entry_states=label_entry_states,
    )

    chained: dict[str, list[Layer]] = defaultdict(list)
    unchained: list[Layer] = []
    for layer in selected:
        cid = layer.chain_id
        if cid is not None:
            chained[cid].append(layer)
        else:
            unchained.append(layer)

    # Resolve one composite per feasible runtime state.  The reconstructed
    # call state (from interprocedural analysis) is used as the reference;
    # only truly unknown variables (no default, no assignment anywhere) are
    # enumerated.  Each variant is internally consistent.
    locations = [
        {
            "script": f.relative_to(config.game_root.parent).as_posix(),
            "line": no,
            "statement": statement,
        }
    ]
    for variant_layers in _enumerate_condition_variants(unchained, chained, call_state):
        ordered = _restore_layer_order(variant_layers, selected)
        if ordered:
            emit_wrapper(tag, "layeredimage", ordered, locations, block)


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
            for token in (
                "ConditionSwitch",
                "LiveComposite",
                "Function(",
                "DynamicDisplayable",
            )
        )
        paths = decl.sources
        if dynamic or not paths:
            parsed_decls[name] = None
            continue
        decl_layers = [
            Layer(
                source=p,
                line=decl.line,
                role="image_declaration_layer",
                conditions=(),
                declaration_expression=expression,
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
                + path_regex.pattern[1:-1]
                + r")['\"]",
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
                for token in (
                    "ConditionSwitch",
                    "LiveComposite",
                    "Function(",
                    "DynamicDisplayable",
                )
            )
            if dynamic:
                dynamic_declarations.append(
                    {
                        "name": name,
                        "declaration": {
                            "script": decl.script.relative_to(
                                config.game_root.parent
                            ).as_posix(),
                            "line": decl.line,
                        },
                        "usage_locations": [loc],
                        "expression": expression,
                        "source_candidates": list(decl.sources),
                        "status": "dynamic_review",
                        "reason": "runtime-dependent image declaration",
                    }
                )
                continue
            layers: list[Layer] | None = parsed_decls.get(name)
            if layers is None:
                continue
            emit_wrapper(
                name,
                "image_declaration",
                layers,
                [loc],
                _DeclBlock(decl.script, decl.line),
            )


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
    label_entry_states: dict[str, dict[str, object]],
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
                    f,
                    usage.line,
                    defaults,
                    config,
                    lines=file_index.lines,
                    label_entry_states=label_entry_states,
                )

                chained: dict[str, list[Layer]] = defaultdict(list)
                unchained: list[Layer] = []
                for layer in stack:
                    cid = layer.chain_id
                    if cid is not None:
                        chained[cid].append(layer)
                    else:
                        unchained.append(layer)

                # Variables assigned in the current label scope are pinned;
                # truly unresolved variables vary over candidates.
                relevant_names: set[str] = set()
                for layer in stack:
                    for condition in layer.conditions:
                        relevant_names.update(condition_variables(condition))
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
                for variant_layers in _enumerate_condition_variants(
                    unchained, chained, call_state
                ):
                    ordered = _restore_layer_order(variant_layers, stack)
                    if ordered:
                        emit_wrapper(name, "screen", ordered, locations, block)
