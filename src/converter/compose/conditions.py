"""Static condition evaluation and call-state computation.

Responsibility: evaluate Ren'Py ``if``/``elif``/``else`` conditions against a
statically-reconstructed variable state (built from ``default`` statements,
``$`` assignments, and ``*ResetDefault`` labels in ``characters.rpy``).

No runtime execution — this is pure static analysis.
"""

from __future__ import annotations

import bisect
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import GameConfig
from .errors import ComposeError
from .models import Layer

UNKNOWN = object()

# Cache for static condition evaluation: (expression, relevant-env-key) -> result.
# Conditions like "month == 6" are evaluated against many call sites with the
# same variable values, so caching avoids repeated Python eval() calls
# (Optimization #7).
_eval_cond_cache: dict[tuple, bool | None | object] = {}

# Cache for call-state reconstruction: (path_str, line_number) -> CallState.
# Multiple usages on the same file at nearby lines share the same reconstructed
# state when no assignment occurs between them (Optimization #8).
_state_at_call_cache: dict[tuple, CallState] = {}

# Cache for reset-defaults map: config identity -> mapping.
_reset_defaults_cache: dict[int, dict[str, list[tuple[str, str]]]] = {}


class ConditionError(ComposeError):
    """Raised when a condition cannot be statically evaluated."""


def parse_literal(value: str) -> bool | int | float | str | object:
    """Parse a Ren'Py literal into a Python value, or return ``UNKNOWN``."""
    value = value.strip()
    if value == "True":
        return True
    if value == "False":
        return False
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if value[:1] in ("'", '"') and value[-1:] == value[:1]:
        return value[1:-1]
    return UNKNOWN


@dataclass(frozen=True)
class CallState:
    """A snapshot of Ren'Py variable values at a given call site."""

    values: dict[str, object] = field(default_factory=dict)

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.values


def state_at_call(
    path: Path, line_number: int, defaults: dict[str, object],
    config: GameConfig | None = None, lines: tuple[str, ...] | list[str] | None = None,
) -> CallState:
    """Compute the variable state at ``line_number`` in ``path``.

    Reconstructs state by scanning from the most recent label before the line,
    applying all ``$`` assignments and ``*ResetDefault`` calls up to (but not
    including) the target line.

    ``lines``, when supplied (e.g. from the single-pass script index), avoids
    re-reading the file from disk (Optimization #1).
    """
    config = config or _require_config()
    defaults_key = _defaults_cache_key(defaults)
    cache_key = (str(path), id(config), defaults_key, line_number)
    cached = _state_at_call_cache.get(cache_key)
    if cached is not None:
        return cached

    key = (str(path), id(config), defaults_key)
    if key not in _state_cache:
        _state_cache[key] = _scan_file(path, defaults, config, lines=lines)
    labels, assignments = _state_cache[key]

    label_start = labels[max(0, bisect.bisect_right(labels, line_number - 1) - 1)]
    state = dict(defaults)
    left = bisect.bisect_left(assignments, (label_start, "", None))
    right = bisect.bisect_left(assignments, (line_number, "", None))
    for _, name, value in assignments[left:right]:
        state[name] = value
    result = CallState(values=state)
    _state_at_call_cache[cache_key] = result
    return result


_state_cache: dict[tuple, tuple[list[int], list[tuple[int, str, object]]]] = {}


def _defaults_cache_key(defaults: dict[str, object]) -> tuple[tuple[str, str], ...]:
    """Return a stable cache key for the values used to reconstruct call state."""
    return tuple(sorted((name, repr(value)) for name, value in defaults.items()))


def _scan_file(
    path: Path, defaults: dict[str, object], config: GameConfig | None = None,
    lines: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[int], list[tuple[int, str, object]]]:
    config = config or _require_config()
    labels: list[int] = [0]
    assignments: list[tuple[int, str, object]] = []
    reset_map = get_reset_defaults_map(config)

    if lines is None:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return labels, assignments

    for i, raw in enumerate(lines, 1):
        text = raw.split("#", 1)[0].strip()
        if re.match(r"^\s*label\s+[\w.]+(?:\([^\n]*\))?\s*:", text):
            labels.append(i)
        m = re.match(r"^\s*\$\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", text)
        if m:
            name, value = m.groups()
            parsed = parse_literal(value)
            if parsed is UNKNOWN and value in defaults:
                parsed = defaults[value]
            if parsed is not UNKNOWN:
                assignments.append((i, name, parsed))
        m_call = re.match(r"^\s*call\s+([A-Za-z_]\w*ResetDefault)\b", text)
        if m_call and m_call.group(1) in reset_map:
            for name, value in reset_map[m_call.group(1)]:
                parsed = parse_literal(value)
                if parsed is UNKNOWN and value in defaults:
                    parsed = defaults[value]
                if parsed is not UNKNOWN:
                    assignments.append((i, name, parsed))

    return labels, assignments


def get_reset_defaults_map(config: GameConfig | None = None) -> dict[str, list[tuple[str, str]]]:
    """Parse ``characters.rpy`` for ``*ResetDefault`` labels and their assignments."""
    config = config or _require_config()
    cache_key = id(config)
    cached = _reset_defaults_cache.get(cache_key)
    if cached is not None:
        return cached
    mapping: dict[str, list[tuple[str, str]]] = {}
    char_file = config.game_root / "characters.rpy"
    if not char_file.exists():
        _reset_defaults_cache[cache_key] = mapping
        return mapping
    current_label: str | None = None
    try:
        for line in char_file.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            text = line.split("#", 1)[0].strip()
            m = re.match(r"^\s*label\s+([A-Za-z_]\w*ResetDefault):", text)
            if m:
                current_label = m.group(1)
                assert current_label is not None
                mapping[current_label] = []
                continue
            if current_label is not None:
                if re.match(r"^\s*return\b", text):
                    current_label = None
                    continue
                m_assign = re.match(r"^\s*\$\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", text)
                if m_assign:
                    name, value = m_assign.group(1), m_assign.group(2)
                    assert current_label is not None
                    mapping[current_label].append((name, value))
    except OSError:
        pass
    _reset_defaults_cache[cache_key] = mapping
    return mapping


def eval_cond(expr: str, env: dict[str, object]) -> bool | None | object:
    """Evaluate a Ren'Py condition expression against ``env``.

    Returns ``True``, ``False``, or ``UNKNOWN`` if the expression references
    variables not present in ``env``.
    """
    expr = expr.strip()
    if expr == "<else>":
        # An ``else`` branch is taken when no preceding if/elif matched.  In
        # static analysis we cannot know whether a previous condition matched,
        # so we treat ``else`` as UNKNOWN (not True) to avoid incorrectly
        # including ``else`` layers when a preceding ``if`` condition was True.
        return UNKNOWN
    # Build a cache key from the expression plus the values of the variables it
    # references.  The same condition evaluated against the same state yields
    # the same result, so we can skip the Python eval() on repeats.
    var_names = [
        v for v in sorted(
            {v for v in re.findall(r"\b[A-Za-z_]\w*\b", expr)
             if v not in {"True", "False", "and", "or", "not", "None"}}
        )
    ]
    cache_key = (expr, tuple((v, repr(env.get(v, "__missing__"))) for v in var_names))
    cached = _eval_cond_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        # Ren'Py conditions are standard Python comparison/boolean expressions.
        result: bool | None | object = bool(eval(expr, {"__builtins__": {}}, dict(env)))
    except (NameError, TypeError, KeyError):
        result = UNKNOWN
    except (ValueError, SyntaxError, AttributeError):
        result = UNKNOWN
    _eval_cond_cache[cache_key] = result
    return result


def evaluate_layer_conditions(
    layer: Layer, env: dict[str, object],
) -> tuple[bool | None, list[str]]:
    """Evaluate a single unchained layer's conditions against an environment.

    Returns ``(outcome, unresolved_conditions)``:

    * ``outcome=True`` — all conditions resolved to True; layer should be included.
    * ``outcome=False`` — at least one condition resolved to False; layer should be excluded.
    * ``outcome=None`` (UNKNOWN) — at least one condition could not be statically
      evaluated; layer status is uncertain and ``unresolved_conditions`` lists them.

    Layers with no conditions always return ``(True, [])``.
    """
    if not layer.conditions:
        return True, []

    unresolved: list[str] = []
    outcome: bool | None = True
    for condition in layer.conditions:
        value = eval_cond(condition, env)
        if value is None:  # UNKNOWN
            unresolved.append(condition)
            outcome = None
            break
        if value is False:
            outcome = False
            break
    return outcome, unresolved


def select_layers_for_chain(
    layers: list[Layer], call_state: CallState,
) -> tuple[list[Layer], list[str]]:
    """Select layers from an if/elif/else chain, respecting mutual exclusion.

    Given layers that all belong to the same if/elif/else chain (same
    ``chain_id``), evaluate each layer's conditions against ``call_state`` and
    return the layers that should be composited:

    * If exactly one layer's conditions all resolve to ``True``, select only
      that layer (mutual exclusion).
    * If multiple layers resolve to ``True`` (should not happen for well-formed
      if/elif/else), select the first one.
    * If all conditions are ``UNKNOWN``, select *all* layers so the dynamic
      review composite shows every possible alternative.  Each layer is marked
      with ``unresolved_conditions``.
    * If all conditions resolve to ``False``, select none.

    Layers without a ``chain_id`` are handled by the caller and are not passed
    here.
    """
    if not layers:
        return [], []

    results: list[tuple[Layer, bool | None, list[str]]] = []
    for layer in layers:
        outcome = True
        unresolved: list[str] = []
        for condition in layer.conditions:
            value = eval_cond(condition, call_state.values)
            if value is UNKNOWN:
                unresolved.append(condition)
                outcome = None  # type: ignore[assignment]
                break
            if value is False:
                outcome = False
                break
        results.append((layer, outcome, unresolved))

    true_layers = [r for r in results if r[1] is True]
    unknown_layers = [r for r in results if r[1] is None]

    if true_layers:
        # Return ALL layers with True conditions.  Mutual exclusion in Ren'Py
        # comes from the if/elif/else source structure (conditions are
        # mutually exclusive by design), not from the chain mechanism.
        # Sequential layers in the same ``if`` block must all be included.
        return [r[0] for r in true_layers], []
    if unknown_layers:
        selected: list[Layer] = []
        unresolved_conditions: list[str] = []
        for layer, _, unresolved in unknown_layers:
            layer = layer.with_unresolved(tuple(unresolved))
            selected.append(layer)
            unresolved_conditions.extend(unresolved)
        return selected, sorted(set(unresolved_conditions))
    return [], []


def _require_config() -> GameConfig:
    raise ComposeError("compose config not initialized; pass a GameConfig explicitly")
