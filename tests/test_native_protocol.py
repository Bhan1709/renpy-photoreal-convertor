"""Tests for native compose result protocol validation."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from converter.compose.errors import NativeProtocolError
from converter.compose.native.constants import PROTOCOL_SCHEMA, PROTOCOL_VERSION
from converter.compose.native.protocol import validate_result


def _candidate() -> dict[str, Any]:
    return {
        "candidate_id": "candidate-1",
        "name": "home",
        "kind": "layeredimage",
        "attributes": ["pose"],
        "image_name": ["home", "pose"],
        "condition_assignments": {"seen_home": True},
        "content_hash": "a" * 64,
        "width": 64,
        "height": 32,
        "render_file": "home.png",
        "layers": [
            {
                "sources": ["bgs/home.png"],
                "attribute": "pose",
                "group": "master",
                "conditions": ["seen_home"],
                "unresolved": [],
                "unresolved_errors": [],
            }
        ],
        "sources": ["bgs/home.png"],
        "unresolved_errors": [],
        "usages": [
            {
                "script": "game/script.rpy",
                "line": 12,
                "statement": "show home",
            }
        ],
        "error": None,
    }


def _result() -> dict[str, Any]:
    return {
        "protocol": PROTOCOL_VERSION,
        "schema_version": PROTOCOL_SCHEMA,
        "status": "ok",
        "batch": {
            "index": 0,
            "state_offset": 0,
            "rendered": 1,
            "total_states": 1,
            "next_offset": None,
            "walk": {"processed": 42, "truncated": False},
        },
        "candidates": [_candidate()],
        "layered_images": [],
        "image_definitions": {},
        "characters": {},
    }


def test_validate_result_accepts_complete_candidate_and_walk_state() -> None:
    validate_result(_result())


@pytest.mark.parametrize(
    ("candidate_updates", "expected_rendered"),
    [
        (
            {
                "content_hash": None,
                "width": 0,
                "height": 0,
                "render_file": None,
            },
            1,
        ),
        (
            {
                "content_hash": None,
                "width": 0,
                "height": 0,
                "render_file": None,
                "error": "render failed: renderer crashed",
            },
            1,
        ),
    ],
)
def test_validate_result_accepts_empty_and_failed_candidates(
    candidate_updates: dict[str, Any],
    expected_rendered: int,
) -> None:
    result = _result()
    result["candidates"][0].update(candidate_updates)
    result["batch"]["rendered"] = expected_rendered

    validate_result(result)


def test_validate_result_rejects_incomplete_candidate() -> None:
    result = _result()
    result["candidates"] = [{}]

    with pytest.raises(NativeProtocolError, match="candidate"):
        validate_result(result)


def test_validate_result_rejects_missing_cfg_walk_state() -> None:
    result = _result()
    del result["batch"]["walk"]

    with pytest.raises(NativeProtocolError, match="walk"):
        validate_result(result)


def test_validate_result_rejects_invalid_pagination_counter() -> None:
    result = copy.deepcopy(_result())
    result["batch"]["next_offset"] = -1

    with pytest.raises(NativeProtocolError, match="next_offset"):
        validate_result(result)
