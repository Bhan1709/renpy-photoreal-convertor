"""Composite counting for the compose pass.

Mirrors the emit logic exactly so ``total == n`` at the end of the run, giving
an accurate progress-bar total.
"""

from __future__ import annotations

import re
from collections import defaultdict

from ..conditions import state_at_call
from ..config import GameConfig
from ..models import Layer
from ..parsing import (
    SHOW_SCENE_RE,
    ImageDeclaration,
    ScriptBlock,
    ScriptIndex,
    parse_screen_stack,
)
from ..records import _restore_layer_order
from .variants import _enumerate_condition_variants


def _count_layeredimage_variants(
    tag: str,
    tail: str,
    block: ScriptBlock,
    candidates: list[Layer],
    call_state,
    config: GameConfig,
    lines: tuple[str, ...] | list[str] | None = None,
) -> int:
    """Return the number of composites one ``show_scene`` usage will emit.

    Mirrors :func:`_emit_layeredimage_uses` but counts variants instead of
    calling the emitter, so the progress bar total is accurate.
    """
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
        return 0

    chained: dict[str, list[Layer]] = defaultdict(list)
    unchained: list[Layer] = []
    for layer in selected:
        cid = layer.chain_id
        if cid is not None:
            chained[cid].append(layer)
        else:
            unchained.append(layer)

    count = 0
    for variant_layers in _enumerate_condition_variants(unchained, chained, call_state):
        ordered = _restore_layer_order(variant_layers, selected)
        if ordered:
            count += 1
    return count


def _count_screen_variants(
    stack: list[Layer],
    call_state,
) -> int:
    """Return the number of composites one ``show screen`` usage will emit.

    Mirrors the screen-stack emit loop but counts variants instead of calling
    the emitter.
    """
    chained: dict[str, list[Layer]] = defaultdict(list)
    unchained: list[Layer] = []
    for layer in stack:
        cid = layer.chain_id
        if cid is not None:
            chained[cid].append(layer)
        else:
            unchained.append(layer)

    count = 0
    for variant_layers in _enumerate_condition_variants(unchained, chained, call_state):
        ordered = _restore_layer_order(variant_layers, stack)
        if ordered:
            count += 1
    return count


def _count_composites(
    index: ScriptIndex,
    definitions: dict[str, tuple[ScriptBlock, list[Layer]]],
    defaults: dict[str, object],
    screens: dict[str, ScriptBlock],
    image_declarations: dict[str, ImageDeclaration],
    config: GameConfig,
    label_entry_states: dict[str, dict[str, object]],
) -> int:
    """Return the total number of composites the compose pass will emit.

    Counts one emitter call per condition variant (Pass 1 layeredimage uses and
    Pass 3 screen stacks) plus one per resolvable static image declaration
    (Pass 2).  Mirrors the emit logic exactly so ``total == n`` at the end of
    the run.
    """
    total = 0

    # Pass 1: layeredimage show/scene statements.
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
            call_state = state_at_call(
                f,
                usage.line,
                defaults,
                config,
                lines=file_index.lines,
                label_entry_states=label_entry_states,
            )
            total += _count_layeredimage_variants(
                tag,
                tail,
                block,
                candidates,
                call_state,
                config,
                lines=file_index.lines,
            )

    # Pass 2: image declarations (static, resolvable ones only).
    for file_index in index.per_file.values():
        for usage in file_index.usages:
            if usage.kind != "show_scene":
                continue
            name = usage.tag
            if name not in image_declarations:
                continue
            decl = image_declarations[name]
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
            if dynamic or not decl.sources:
                continue
            total += 1

    # Pass 3: screen stacks.
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
            stack = screen_stack_cache[name]
            if not stack:
                continue
            call_state = state_at_call(
                f,
                usage.line,
                defaults,
                config,
                lines=file_index.lines,
                label_entry_states=label_entry_states,
            )
            total += _count_screen_variants(stack, call_state)

    return total
