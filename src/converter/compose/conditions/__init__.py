"""Static condition evaluation and call-state computation.

Responsibility: evaluate Ren'Py ``if``/``elif``/``else`` conditions against a
statically-reconstructed variable state (built from ``default`` statements,
``$`` assignments, and assignments performed by simple called labels such as
``undressEmma`` or ``*ResetDefault``).

No runtime execution — this is pure static analysis.

Public API
----------

    from converter.compose.conditions import (
        UNKNOWN,
        CallState,
        condition_variables,
        eval_cond,
        parse_literal,
        state_at_call,
        compute_interprocedural_label_states,
        get_label_assignments_map,
        evaluate_layer_conditions,
        select_layers_for_chain,
    )
"""

from __future__ import annotations

from .callstate import (
    CallState,
    compute_interprocedural_label_states,
    get_label_assignments_map,
    state_at_call,
)
from .eval import (
    UNKNOWN,
    ConditionError,
    condition_variables,
    eval_cond,
    evaluate_layer_conditions,
    parse_literal,
    select_layers_for_chain,
)

__all__ = [
    "UNKNOWN",
    "CallState",
    "ConditionError",
    "compute_interprocedural_label_states",
    "condition_variables",
    "eval_cond",
    "evaluate_layer_conditions",
    "get_label_assignments_map",
    "parse_literal",
    "select_layers_for_chain",
    "state_at_call",
]
