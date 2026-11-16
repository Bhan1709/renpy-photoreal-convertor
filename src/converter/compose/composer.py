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
    UNKNOWN,
    condition_variables,
    eval_cond,
    parse_literal,
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


# ``var == literal``, ``low <= var <= high`` and bare ``var`` condition atoms.
_CONDITION_EQ_RE = re.compile(
    r"\b([A-Za-z_]\w*)\s*==\s*"
    r"(-?\d+(?:\.\d+)?|True|False|None|'[^']*'|\"[^\"]*\")(?!\w)"
)
_CONDITION_BETWEEN_RE = re.compile(
    r"(-?\d+(?:\.\d+)?)\s*<=\s*([A-Za-z_]\w*)\s*<=\s*(-?\d+(?:\.\d+)?)"
)
_CONDITION_BARE_RE = re.compile(r"^\s*(not\s+)?([A-Za-z_]\w*)\s*$")


def _condition_vars(condition: str) -> set[str]:
    """Extract variable names from a condition expression."""
    return condition_variables(condition)


def _condition_candidates(conditions: list[str]) -> dict[str, list[object]]:
    """Collect candidate values for each variable mentioned in ``conditions``.

    Only values that can make the variable satisfy a condition atom are
    collected: equality literals, lower bounds of range tests, and truthy /
    falsy values for bare boolean tests.  Ordering follows source order so the
    first candidate is a stable reference value.
    """
    candidates: dict[str, list[object]] = {}

    def add(name: str, value: object) -> None:
        bucket = candidates.setdefault(name, [])
        if value not in bucket:
            bucket.append(value)

    for condition in conditions:
        for name, literal in _CONDITION_EQ_RE.findall(condition):
            value = parse_literal(literal)
            if value is not UNKNOWN:
                add(name, value)
        for low, name, high in _CONDITION_BETWEEN_RE.findall(condition):
            low_value = parse_literal(low)
            high_value = parse_literal(high)
            if low_value is not UNKNOWN and high_value is not UNKNOWN:
                add(name, low_value)
        bare = _CONDITION_BARE_RE.match(condition)
        if bare:
            # Bare boolean tests have two feasible states; the falsy one is
            # the neutral reference (no extra overlay) and the truthy one is
            # emitted as an additional scenario.
            add(bare.group(2), False)
            add(bare.group(2), True)
    return candidates


def _conditions_true(conditions: tuple[str, ...], env: dict[str, object]) -> bool:
    """Return True when every condition holds in ``env`` (UNKNOWN counts as False).

    ``<else>`` markers are skipped: callers evaluate chain branches in source
    order and stop at the first match, so an ``else`` branch only wins when no
    earlier branch did.
    """
    for condition in conditions:
        if "<else>" in condition:
            continue
        if eval_cond(condition, env) is not True:
            return False
    return True


def _branch_groups(chain_layers: list[Layer]) -> list[tuple[tuple[str, ...], list[Layer]]]:
    """Split one if/elif/else chain into ordered branch groups.

    Layers that share a condition tuple belong to the same branch (a branch
    may contribute several images), so they must be included or excluded
    together.
    """
    groups: list[tuple[tuple[str, ...], list[Layer]]] = []
    for layer in sorted(chain_layers, key=lambda item: item.line):
        if groups and groups[-1][0] == layer.conditions:
            groups[-1][1].append(layer)
        else:
            groups.append((layer.conditions, [layer]))
    return groups


def _layers_for_assignment(
    unchained: list[Layer],
    chained: dict[str, list[Layer]],
    env: dict[str, object],
) -> list[Layer]:
    """Resolve the layers Ren'Py would show for one variable assignment.

    This mirrors layeredimage semantics: unchained layers are shown when all
    their conditions hold, and each if/elif/else chain contributes only the
    first branch whose conditions hold (falling back to ``else``).
    """
    layers: list[Layer] = []
    for layer in unchained:
        if _conditions_true(layer.conditions, env):
            layers.append(layer)
    for chain_layers in chained.values():
        for conditions, branch_layers in _branch_groups(chain_layers):
            if _conditions_true(conditions, env):
                layers.extend(branch_layers)
                break
    return layers


def _env_key(env: dict[str, object]) -> tuple[tuple[str, str], ...]:
    """Return a stable key for a variable assignment."""
    return tuple(sorted((name, repr(value)) for name, value in env.items()))


def _satisfying_assignment(conditions: tuple[str, ...]) -> dict[str, object] | None:
    """Build a variable assignment that makes all ``conditions`` hold.

    Returns ``None`` when the constraints conflict or cannot be expressed with
    the supported atoms.
    """
    env: dict[str, object] = {}
    for condition in conditions:
        for name, literal in _CONDITION_EQ_RE.findall(condition):
            value = parse_literal(literal)
            if value is UNKNOWN:
                return None
            if name in env and env[name] != value:
                return None
            env[name] = value
        for low, name, high in _CONDITION_BETWEEN_RE.findall(condition):
            low_value = parse_literal(low)
            high_value = parse_literal(high)
            if not isinstance(low_value, (int, float)) or not isinstance(
                high_value, (int, float)
            ):
                return None
            if name in env:
                current = env[name]
                if not isinstance(current, (int, float)) or not (
                    low_value <= current <= high_value
                ):
                    return None
            else:
                env[name] = low_value
        bare = _CONDITION_BARE_RE.match(condition)
        if bare:
            name = bare.group(2)
            value = not bool(bare.group(1))
            if name in env and env[name] != value:
                return None
            env[name] = value
    if not _conditions_true(conditions, env):
        return None
    return env


def _enumerate_condition_variants(
    unchained: list[Layer],
    chained: dict[str, list[Layer]],
    values: dict[str, object],
) -> list[list[Layer]]:
    """Enumerate feasible layer sets for one usage, one per runtime state.

    Earlier versions combined per-branch options independently, which mixed
    mutually exclusive layers (e.g. ``amberArmL == 3`` and ``amberArmL == 5``,
    or ``hair1`` and ``hair2``) into a single image because every ``if`` is its
    own chain.  Instead, variants are now derived from *consistent variable
    assignments*:

    * Start from the statically reconstructed call state.
    * Pick a reference value for every unresolved variable.
    * Vary one variable at a time (plus one scenario per coupled multi-variable
      condition) away from that reference.
    * Resolve the full layer set for each assignment, then deduplicate.

    This covers every branch without taking a cartesian product over chains,
    so no variant can contain two mutually exclusive overlays.

    Values already known at the call site (``$`` assignments and game
    ``default`` statements) are treated as fixed: forcing every defaulted
    variable to vary instead yields thousands of near-identical composites per
    usage.  Variables with no static value at all are the ones enumerated.
    """
    conditions: list[str] = []
    for layer in unchained:
        conditions.extend(layer.conditions)
    for chain_layers in chained.values():
        for layer in chain_layers:
            conditions.extend(layer.conditions)

    candidates = _condition_candidates(conditions)
    referenced: set[str] = set()
    for condition in conditions:
        referenced.update(_condition_vars(condition))

    # Variables whose value is not fixed by the reconstructed call state.
    varying = referenced - set(values)

    # Reference assignment: known state plus a canonical value for unknowns.
    reference: dict[str, object] = dict(values)
    for name in sorted(varying):
        options = candidates.get(name)
        if options:
            reference[name] = options[0]

    assignments: list[dict[str, object]] = [reference]
    seen_envs = {_env_key(reference)}

    def add_assignment(env: dict[str, object]) -> None:
        key = _env_key(env)
        if key in seen_envs:
            return
        seen_envs.add(key)
        assignments.append(env)

    # One scenario per alternative value of each varying variable.
    for name in sorted(varying):
        for value in candidates.get(name, ()):
            if name in reference and reference[name] == value:
                continue
            add_assignment({**reference, name: value})

    # One scenario per coupled, multi-variable condition (e.g.
    # ``amberBoobs == 3 and amberOutfit == 0``) so combined overlays are
    # produced without enumerating every other variable's values.
    seen_conditions: set[tuple[str, ...]] = set()
    for layer in unchained:
        seen_conditions.add(layer.conditions)
    for chain_layers in chained.values():
        seen_conditions.update(conditions for conditions, _ in _branch_groups(chain_layers))

    for group in sorted(seen_conditions):
        used = set()
        for condition in group:
            used.update(_condition_vars(condition))
        if len(used & varying) < 2:
            continue
        assignment = _satisfying_assignment(group)
        if assignment is None:
            continue
        add_assignment({**reference, **assignment})

    variants: list[list[Layer]] = []
    seen_variants: set[tuple[tuple[object, ...], ...]] = set()
    for env in assignments:
        resolved = _layers_for_assignment(unchained, chained, env)
        key = tuple(
            (layer.source, layer.line, layer.conditions, layer.attribute, layer.chain_id)
            for layer in resolved
        )
        if key in seen_variants:
            continue
        seen_variants.add(key)
        variants.append(resolved)
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

    # Resolve one composite per feasible runtime state.  The reconstructed
    # call state is used as the reference; only variables with no static value
    # are enumerated, so each variant is internally consistent.
    locations = [
        {
            "script": f.relative_to(config.game_root.parent).as_posix(),
            "line": no,
            "statement": statement,
        }
    ]
    for variant_layers in _enumerate_condition_variants(
        unchained, chained, call_state.values
    ):
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

                chained: dict[str, list[Layer]] = defaultdict(list)
                unchained: list[Layer] = []
                for layer in stack:
                    cid = layer.chain_id
                    if cid is not None:
                        chained[cid].append(layer)
                    else:
                        unchained.append(layer)

                # Variables that are statically known only through ``default``
                # are treated as fixed at their default value; only genuinely
                # unresolved variables produce alternative composites.
                relevant_names: set[str] = set()
                for layer in stack:
                    for condition in layer.conditions:
                        relevant_names.update(_condition_vars(condition))
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
                    unchained, chained, call_state.values
                ):
                    ordered = _restore_layer_order(variant_layers, stack)
                    if ordered:
                        emit_wrapper(name, "screen", ordered, locations, block)
