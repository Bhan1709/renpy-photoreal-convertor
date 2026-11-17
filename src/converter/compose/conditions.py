"""Static condition evaluation and call-state computation.

Responsibility: evaluate Ren'Py ``if``/``elif``/``else`` conditions against a
statically-reconstructed variable state (built from ``default`` statements,
``$`` assignments, and assignments performed by simple called labels such as
``undressEmma`` or ``*ResetDefault``).

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

# Cache for call-state reconstruction: (path_str, line_number) -> CallState.
# Multiple usages on the same file at nearby lines share the same reconstructed
# state when no assignment occurs between them (Optimization #8).
_state_at_call_cache: dict[tuple, CallState] = {}

# Cache for label-assignment map: config identity -> mapping.
_label_assignments_cache: dict[int, dict[str, list[tuple[str, str]]]] = {}


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
    """A snapshot of Ren'Py variable values at a given call site.

    ``values`` contains defaults overlaid with assignments from the current
    label; ``assigned`` records which names were written by ``$`` statements or
    simple called labels in that scope.  A name in ``assigned`` is genuinely
    pinned at the call site, whereas a name only present in ``values`` may have
    been changed by an earlier label and is therefore still variable.
    """

    values: dict[str, object] = field(default_factory=dict)
    assigned: frozenset[str] = frozenset()

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)

    def __contains__(self, key: str) -> bool:
        return key in self.values


def state_at_call(
    path: Path, line_number: int, defaults: dict[str, object],
    config: GameConfig | None = None, lines: tuple[str, ...] | list[str] | None = None,
    label_entry_states: dict[str, dict[str, object]] | None = None,
) -> CallState:
    """Compute the variable state at ``line_number`` in ``path``.

    Reconstructs state by scanning from the most recent label before the line,
    applying all ``$`` assignments and ``*ResetDefault`` calls up to (but not
    including) the target line.

    When ``label_entry_states`` is supplied (from interprocedural analysis),
    the state starts from that label's entry state (which reflects assignments
    propagated from caller labels) rather than from game ``default``
    statements alone.

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
    label_lines, label_names, assignments = _state_cache[key]

    label_idx = max(0, bisect.bisect_right(label_lines, line_number - 1) - 1)
    label_start = label_lines[label_idx]
    label_name = label_names[label_idx]

    # Start from the interprocedural label entry state if available, which
    # reflects variable assignments propagated from caller labels.  Fall back
    # to game defaults when no entry state is known.
    if label_entry_states is not None and label_name is not None:
        state = dict(label_entry_states.get(label_name, defaults))
    else:
        state = dict(defaults)

    left = bisect.bisect_left(assignments, (label_start, "", None))
    right = bisect.bisect_left(assignments, (line_number, "", None))
    scoped = assignments[left:right]
    for _, name, value in scoped:
        state[name] = value
    result = CallState(
        values=state,
        assigned=frozenset(name for _, name, _ in scoped),
    )
    _state_at_call_cache[cache_key] = result
    return result


_state_cache: dict[tuple, tuple[list[int], list[str | None], list[tuple[int, str, object]]]] = {}


def _defaults_cache_key(defaults: dict[str, object]) -> tuple[tuple[str, str], ...]:
    """Return a stable cache key for the values used to reconstruct call state."""
    return tuple(sorted((name, repr(value)) for name, value in defaults.items()))


def _scan_file(
    path: Path, defaults: dict[str, object], config: GameConfig | None = None,
    lines: tuple[str, ...] | list[str] | None = None,
) -> tuple[list[int], list[str | None], list[tuple[int, str, object]]]:
    config = config or _require_config()
    labels: list[int] = [0]
    label_names: list[str | None] = [None]
    assignments: list[tuple[int, str, object]] = []
    label_map = get_label_assignments_map(config)
    label_assign = re.compile(r"^\s*call\s+([A-Za-z_]\w*)\b")

    if lines is None:
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            return labels, label_names, assignments

    for i, raw in enumerate(lines, 1):
        text = raw.split("#", 1)[0].strip()
        m_label = re.match(r"^\s*label\s+([A-Za-z_]\w+)(?:\s*\([^)]*\))?\s*:", text)
        if m_label:
            labels.append(i)
            label_names.append(m_label.group(1))
        m = re.match(r"^\s*\$\s*([A-Za-z_]\w*)\s*=\s*(.*?)\s*$", text)
        if m:
            name, value = m.groups()
            parsed = parse_literal(value)
            if parsed is UNKNOWN and value in defaults:
                parsed = defaults[value]
            if parsed is not UNKNOWN:
                assignments.append((i, name, parsed))
        m_call = label_assign.match(text)
        if m_call and m_call.group(1) in label_map:
            for name, value in label_map[m_call.group(1)]:
                parsed = parse_literal(value)
                if parsed is UNKNOWN and value in defaults:
                    parsed = defaults[value]
                if parsed is not UNKNOWN:
                    assignments.append((i, name, parsed))

    return labels, label_names, assignments


def _parse_label_body(lines: list[str], start_line: int) -> list[tuple[int, str]]:
    """Parse the body of a label starting at ``start_line`` (1-based)."""
    body: list[tuple[int, str]] = []
    if start_line > len(lines):
        return body
    indent = len(lines[start_line - 1]) - len(lines[start_line - 1].lstrip())
    for i in range(start_line, len(lines)):
        raw = lines[i]
        if raw.strip() and len(raw) - len(raw.lstrip()) <= indent:
            break
        body.append((i + 1, raw))
    return body


def compute_interprocedural_label_states(
    defaults: dict[str, object],
    file_lines: dict[Path, tuple[str, ...]],
) -> dict[str, dict[str, object]]:
    """Interprocedural data-flow analysis: variable state at each label entry.

    Uses a worklist algorithm seeded from Ren'Py entry-point labels
    (``start``, ``splashscreen``, ``before_main_menu``, ``main_menu``).  For
    each label, the body is scanned sequentially: ``$ var = value`` updates
    the running state, and ``call target`` propagates the current state to
    ``target``'s entry.  The worklist loops until no label entry state
    changes (fixpoint).

    Returns a mapping of label name -> variable state dict at that label's
    entry.  Labels unreachable from any entry point are omitted.
    """
    # Collect all labels: label_name -> (path, line, body_lines)
    labels: dict[str, tuple[Path, int, list[tuple[int, str]]]] = {}
    for path, lines in file_lines.items():
        lines_list = list(lines)
        i = 0
        while i < len(lines_list):
            raw = lines_list[i]
            text = raw.split("#", 1)[0].strip()
            m = _LABEL_RE.match(text)
            if m:
                label_name = m.group(1)
                if label_name not in labels:
                    labels[label_name] = (
                        path,
                        i + 1,
                        _parse_label_body(lines_list, i + 1),
                    )
            i += 1

    # Worklist algorithm
    entry_states: dict[str, dict[str, object]] = {}
    worklist: list[str] = []

    # Seed from entry points
    for label_name in labels:
        if label_name in _ENTRY_LABELS:
            entry_states[label_name] = dict(defaults)
            worklist.append(label_name)

    while worklist:
        name = worklist.pop(0)
        entry_state = dict(entry_states[name])
        body = labels[name][2]

        current_state = dict(entry_state)
        for _line_no, raw in body:
            text = raw.split("#", 1)[0].strip()

            # Handle ``$ var = value``
            m = _ASSIGNMENT_RE.match(text)
            if m:
                var_name = m.group(1)
                value = parse_literal(m.group(2))
                if value is not UNKNOWN:
                    current_state[var_name] = value

            # Handle ``call target_label``
            m_call = _CALL_RE.match(text)
            if m_call:
                target = m_call.group(1)
                if target in labels:
                    new_state = dict(current_state)
                    if target not in entry_states:
                        entry_states[target] = new_state
                        worklist.append(target)
                    else:
                        merged = dict(entry_states[target])
                        merged.update(new_state)
                        if merged != entry_states[target]:
                            entry_states[target] = merged
                            worklist.append(target)

    return entry_states


_LABEL_RE = re.compile(r"^\s*label\s+([A-Za-z_]\w*)(?:\s*\([^)]*\))?\s*:\s*$")
_ASSIGNMENT_RE = re.compile(r"^\s*\$\s*([A-Za-z_]\w*)\s*=\s*(.+?)\s*$")
_CALL_RE = re.compile(r"^\s*call\s+([A-Za-z_]\w*)\b")

# Ren'Py reserved entry-point labels.  Execution begins at one of these, so
# the interprocedural analysis seeds its worklist from here.
_ENTRY_LABELS = frozenset({"start", "splashscreen", "before_main_menu", "main_menu"})


def get_label_assignments_map(
    config: GameConfig | None = None,
) -> dict[str, list[tuple[str, str]]]:
    """Map simple labels to the variables they assign.

    A label qualifies when its body contains only ``$ name = value``
    assignments, ``pass``, and ``return`` (optionally with an expression).
    Calling such a label deterministically writes those variables, so the
    compose pass can apply them at ``call`` sites — for example
    ``call undressEmma`` sets ``emmaOutfit = 0`` and friends.

    Labels with branches, menus, or other statements are omitted because their
    effects depend on runtime control flow.
    """
    config = config or _require_config()
    cache_key = id(config)
    cached = _label_assignments_cache.get(cache_key)
    if cached is not None:
        return cached

    if config.rpy_files:
        include = {rel.replace("\\", "/") for rel in config.rpy_files}
        paths = [
            p for p in config.game_root.rglob("*.rpy")
            if p.relative_to(config.game_root).as_posix() in include
        ]
    else:
        paths = [
            p for p in config.game_root.rglob("*.rpy")
            if p.name != "composite_images.rpy"
            and "cache" not in p.parts
            and "archive" not in p.parts
        ]

    mapping: dict[str, list[tuple[str, str]]] = {}
    for path in sorted(paths, key=lambda p: p.as_posix().lower()):
        try:
            lines = path.read_text(encoding="utf-8-sig", errors="replace").splitlines()
        except OSError:
            continue

        current: str | None = None
        assignments: list[tuple[str, str]] = []
        simple = True
        for raw in lines:
            text = raw.split("#", 1)[0].strip()
            m_label = _LABEL_RE.match(text)
            if m_label:
                if current is not None and simple and assignments:
                    mapping[current] = assignments
                current = m_label.group(1)
                assignments = []
                simple = True
                continue
            if current is None:
                continue
            if not text:
                continue
            if re.match(r"^return\b", text):
                if simple and assignments:
                    mapping[current] = assignments
                current = None
                continue
            if re.fullmatch(r"pass", text):
                continue
            m_assign = _ASSIGNMENT_RE.match(text)
            if m_assign:
                assignments.append((m_assign.group(1), m_assign.group(2)))
            else:
                simple = False
        if current is not None and simple and assignments:
            mapping[current] = assignments

    _label_assignments_cache[cache_key] = mapping
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
    var_names = sorted(condition_variables(expr))
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
        if value is UNKNOWN:  # UNKNOWN
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
