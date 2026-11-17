"""Condition variant enumeration for the compose pass.

Responsibility: enumerate the feasible layer sets for one composite usage, one
per statically-reconstructed runtime state, without taking a cartesian product
over mutually-exclusive if/elif/else chains.
"""

from __future__ import annotations

import re

from ..conditions import UNKNOWN, condition_variables, eval_cond, parse_literal
from ..models import Layer

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


def _branch_groups(
    chain_layers: list[Layer],
) -> list[tuple[tuple[str, ...], list[Layer]]]:
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
    call_state,
) -> list[list[Layer]]:
    """Enumerate feasible layer sets for one usage, one per runtime state.

    Earlier versions combined per-branch options independently, which mixed
    mutually exclusive layers (e.g. ``amberArmL == 3`` and ``amberArmL == 5``,
    or ``hair1`` and ``hair2``) into a single image because every ``if`` is its
    own chain.  Instead, variants are now derived from *consistent variable
    assignments*:

    * Start from the interprocedurally-reconstructed call state (defaults
      overlaid with assignments propagated from caller labels plus explicit
      assignments in the current label scope).  This is the reference
      composite — what the apply pass uses for a direct mapping.
    * For truly unknown variables (no default, no assignment anywhere in the
      game), vary one at a time over their condition candidates.
    * One scenario per coupled multi-variable condition.
    * Resolve the full layer set for each assignment, then deduplicate.

    This covers every branch without taking a cartesian product over chains,
    so no variant can contain two mutually exclusive overlays.  Each usage
    gets one composite for its statically-determined state — no variants are
    generated that the scripts will never actually use.
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

    # Reference: statically determined state for this usage.
    reference: dict[str, object] = dict(call_state.values)
    pinned: set[str] = set(call_state.values)
    varying: set[str] = referenced - pinned
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

    # One scenario per alternative candidate value for truly unknown variables
    # (no default, no scoped assignment anywhere in the game).
    for name in sorted(varying):
        for value in candidates.get(name, ()):
            add_assignment({**reference, name: value})

    # One scenario per coupled, multi-variable condition (e.g.
    # ``amberBoobs == 3 and amberOutfit == 0``) so combined overlays are
    # produced without enumerating every other variable's values.
    seen_conditions: set[tuple[str, ...]] = set()
    for layer in unchained:
        seen_conditions.add(layer.conditions)
    for chain_layers in chained.values():
        seen_conditions.update(
            conditions for conditions, _ in _branch_groups(chain_layers)
        )

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
            (
                layer.source,
                layer.line,
                layer.conditions,
                layer.attribute,
                layer.chain_id,
            )
            for layer in resolved
        )
        if key in seen_variants:
            continue
        seen_variants.add(key)
        variants.append(resolved)
    return variants
