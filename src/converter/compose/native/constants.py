"""Safety and protocol constants for the native compose stage."""

from __future__ import annotations

from typing import Final

PROTOCOL_VERSION: Final[str] = "renpy-compose-native-v2"
PROTOCOL_SCHEMA: Final[int] = 2

VALID_MODES: Final[tuple[str, ...]] = ("cfg", "layered_all", "conditions")
DEFAULT_MODES: Final[tuple[str, ...]] = VALID_MODES

# CFG-discovered states are mandatory.  This limit applies only to additional
# synthetic attribute combinations for one layered image.
MAX_SYNTHETIC_ATTRIBUTE_STATES: Final[int] = 1_024
MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE: Final[int] = 64
MAX_RENDER_STATES_PER_BATCH: Final[int] = 512
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
DEFAULT_RENDERER: Final[str] = "sw"

NATIVE_COMMAND: Final[str] = "composite_export"
INJECTED_SCRIPT: Final[str] = "_composite_export.rpy"

__all__ = [
    "DEFAULT_MODES",
    "DEFAULT_PROCESS_TIMEOUT_SECONDS",
    "DEFAULT_RENDERER",
    "FREE_MEMORY_RENDER_INTERVAL",
    "INJECTED_SCRIPT",
    "MAX_CFG_CALL_DEPTH",
    "MAX_CFG_PROCESSED_NODES",
    "MAX_CONDITION_ASSIGNMENTS_PER_CANDIDATE",
    "MAX_DIALOGUE_LOOKAHEAD",
    "MAX_RENDER_STATES_PER_BATCH",
    "MAX_SYNTHETIC_ATTRIBUTE_STATES",
    "MIN_RENDER_STATES_PER_BATCH",
    "NATIVE_COMMAND",
    "PROTOCOL_SCHEMA",
    "PROTOCOL_VERSION",
    "VALID_MODES",
]
