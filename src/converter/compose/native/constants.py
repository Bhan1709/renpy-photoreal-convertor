"""Safety and protocol constants for the native compose stage."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final

PROTOCOL_VERSION: Final[str] = "renpy-compose-native-v2"
PROTOCOL_SCHEMA: Final[int] = 2

CANONICAL_MODES: Final[tuple[str, ...]] = ("cfg", "layered_all", "conditions")
DEFAULT_MODES: Final[tuple[str, ...]] = CANONICAL_MODES

# Compatibility names used by earlier native compose configurations.  The
# native CFG baseline is mandatory, while the two generators are additive.
LEGACY_MODE_ALIASES: Final[dict[str, tuple[str, ...]]] = {
    "all": CANONICAL_MODES,
    "condition": ("cfg", "conditions"),
    "declaration": ("cfg",),
    "layered": ("cfg", "layered_all"),
    "screen": ("cfg",),
}
VALID_MODES: Final[tuple[str, ...]] = CANONICAL_MODES + tuple(
    sorted(LEGACY_MODE_ALIASES)
)


def normalize_modes(modes: Iterable[str] | None) -> list[str]:
    """Return canonical native modes while preserving legacy aliases.

    An omitted or empty policy keeps the all-modes compatibility default.
    CFG is always present because it is the mandatory native baseline.
    """
    requested = [str(mode).strip().lower() for mode in (modes or []) if str(mode).strip()]
    if not requested:
        return list(DEFAULT_MODES)
    result = ["cfg"]
    for mode in requested:
        expansion = LEGACY_MODE_ALIASES.get(mode, (mode,))
        for canonical in expansion:
            if canonical not in result:
                result.append(canonical)
    return result

# CFG-discovered states are mandatory.  This limit applies only to additional
# synthetic attribute combinations for one layered image.
MAX_SYNTHETIC_ATTRIBUTE_STATES: Final[int] = 1_024
MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE: Final[int] = 64
MAX_RENDER_STATES_PER_BATCH: Final[int] = 512

# Global discovery budgets bound the complete state inventory before it is
# rendered.  Mandatory CFG states are never silently dropped: exceeding the
# total-state budget is an explicit limit error.  Optional synthetic and
# condition variants are truncated deterministically when either global
# budget is reached.
MAX_TOTAL_RENDER_STATES: Final[int] = 100_000
MAX_TOTAL_VARIANTS: Final[int] = 250_000
# A child that crashes mid-batch is retried with a halved sub-batch, down to
# this floor.  Small sub-batches cost an extra interpreter startup each, but
# they keep pagination moving forward when the renderer runs out of memory.
MIN_RENDER_STATES_PER_BATCH: Final[int] = 128
FREE_MEMORY_RENDER_INTERVAL: Final[int] = 250

# Bound pathological CFGs while retaining every state encountered on a real
# branch.  The label walk is linear in the number of statements, so the node
# budget is a runaway-loop guard rather than a coverage limit.
MAX_CFG_PROCESSED_NODES: Final[int] = 1_000_000
MAX_CFG_CALL_DEPTH: Final[int] = 6
MAX_DIALOGUE_LOOKAHEAD: Final[int] = 5

DEFAULT_PROCESS_TIMEOUT_SECONDS: Final[int] = 1_800
NATIVE_WORKER_STARTUP_TIMEOUT_SECONDS: Final[int] = 15
DEFAULT_RENDERER: Final[str] = "sw"

NATIVE_COMMAND: Final[str] = "composite_export"
NATIVE_WORKER_COMMAND: Final[str] = "composite_export_worker"
INJECTED_SCRIPT: Final[str] = "_composite_export.rpy"

# Persistent artifacts live below the configured output directory but outside
# the generated ``static``/``dynamic_review`` image trees.  They are therefore
# excluded from stale-output pruning by construction.
NATIVE_CACHE_DIR_NAME: Final[str] = ".compose-native-cache"
NATIVE_DISCOVERY_CACHE_VERSION: Final[int] = 5
NATIVE_RENDER_STORE_DIR_NAME: Final[str] = "renders"
NATIVE_CAS_DIR_NAME: Final[str] = "png-cas"
NATIVE_DISCOVERY_CACHE_NAME: Final[str] = "discovery.json"

__all__ = [
    "CANONICAL_MODES",
    "DEFAULT_MODES",
    "DEFAULT_PROCESS_TIMEOUT_SECONDS",
    "DEFAULT_RENDERER",
    "FREE_MEMORY_RENDER_INTERVAL",
    "INJECTED_SCRIPT",
    "LEGACY_MODE_ALIASES",
    "MAX_CFG_CALL_DEPTH",
    "MAX_CFG_PROCESSED_NODES",
    "MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE",
    "MAX_DIALOGUE_LOOKAHEAD",
    "MAX_RENDER_STATES_PER_BATCH",
    "MAX_SYNTHETIC_ATTRIBUTE_STATES",
    "MAX_TOTAL_RENDER_STATES",
    "MAX_TOTAL_VARIANTS",
    "MIN_RENDER_STATES_PER_BATCH",
    "NATIVE_CACHE_DIR_NAME",
    "NATIVE_CAS_DIR_NAME",
    "NATIVE_COMMAND",
    "NATIVE_DISCOVERY_CACHE_NAME",
    "NATIVE_DISCOVERY_CACHE_VERSION",
    "NATIVE_RENDER_STORE_DIR_NAME",
    "NATIVE_WORKER_COMMAND",
    "NATIVE_WORKER_STARTUP_TIMEOUT_SECONDS",
    "PROTOCOL_SCHEMA",
    "PROTOCOL_VERSION",
    "VALID_MODES",
    "normalize_modes",
]
