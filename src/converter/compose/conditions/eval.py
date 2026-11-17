"""Static condition evaluation.

Responsibility: evaluate Ren'Py ``if``/``elif``/``else`` conditions against a
statically-reconstructed variable state.  Pure static analysis — no runtime
execution.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from ..config import GameConfig
from ..errors import ComposeError
from ..models import Layer

if TYPE_CHECKING:
    from .callstate import CallState

UNKNOWN = object()

# Ren'Py/Python expression keywords that never name a game variable.
EXPR_KEYWORDS: frozenset[str] = frozenset(
    {"True", "False", "and", "or", "not", "None", "in", "is"}
)

# Quoted string literals inside conditions (e.g. ``x == "{s}Home{/s}"``).
_STRING_LITERAL_RE = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def condition_variables(condition: str) -> set[str]:
    """Return the variable names referenced by ``condition``.

    Words inside string literals are ignored: Ren'Py text tags such as
    ``"{s}Home{/s}"`` contain tag letters that are not variables.
    """
    expr = _STRING_LITERAL_RE.sub("''", condition)
    return {
        name
        for name in re.findall(r"\b[A-Za-z_]\w*\b", expr)
        if name not in EXPR_KEYWORDS
    }


# Cache for static condition evaluation: (expression, relevant-env-key) -> result.
# Conditions like "month == 6" are evaluated against many call sites with the
# same variable values, so caching avoids repeated Python eval() calls
# (Optimization #7).
_eval_cond_cache: dict[tuple, bool | None | object] = {}


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
    var_names = sorted(condition_variables(expr))
    cache_key = (expr, tuple((v, repr(env.get(v, "__missing__"))) for v in var_names))
    cached = _eval_cond_cache.get(cache_key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    try:
        # Ren'Py conditions are standard Python comparison/boolean expressions.
        result: bool | None | object = bool(eval(expr, {"__builtins__": {}}, dict(env)))
    except NameError, TypeError, KeyError:
        result = UNKNOWN
    except ValueError, SyntaxError, AttributeError:
        result = UNKNOWN
    _eval_cond_cache[cache_key] = result
    return result


def evaluate_layer_conditions(
    layer: Layer,
    env: dict[str, object],
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
        if value is UNKNOWN:  # UNKNOWN
            unresolved.append(condition)
            outcome = None
            break
        if value is False:
            outcome = False
            break
    return outcome, unresolved


def select_layers_for_chain(
    layers: list[Layer],
    call_state: CallState,
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
