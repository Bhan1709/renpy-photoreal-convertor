"""Tests for native compose result protocol validation."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from converter.compose.errors import NativeLimitError, NativeProtocolError
from converter.compose.native.constants import PROTOCOL_SCHEMA, PROTOCOL_VERSION
from converter.compose.native.protocol import read_result_stream, validate_result


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
        "render_file": f"{'a' * 64}.png",
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


@pytest.mark.parametrize(
    "render_file",
    [
        "../outside.png",
        "nested/render.png",
        "different-hash.png",
    ],
)
def test_validate_result_rejects_an_unsafe_render_reference(
    render_file: str,
) -> None:
    result = _result()
    result["candidates"][0]["render_file"] = render_file

    with pytest.raises(NativeProtocolError, match="render_file"):
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


def _stream_records(
    *,
    candidate_count: int = 1,
    batch_updates: dict[str, Any] | None = None,
    identity: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    batch = copy.deepcopy(_result()["batch"])
    batch["rendered"] = candidate_count
    batch["total_states"] = candidate_count
    batch.update(batch_updates or {})
    if identity is not None:
        batch["identity"] = identity
    header = {
        "protocol": PROTOCOL_VERSION,
        "schema_version": PROTOCOL_SCHEMA,
        "record_type": "header",
        "status": "ok",
        "batch": batch,
        "layered_images": [],
        "image_definitions": {},
        "characters": {},
    }
    records = [header]
    for _index in range(candidate_count):
        records.append(
            {
                "protocol": PROTOCOL_VERSION,
                "schema_version": PROTOCOL_SCHEMA,
                "record_type": "candidate",
                **_candidate(),
            }
        )
    records.append(
        {
            "protocol": PROTOCOL_VERSION,
            "schema_version": PROTOCOL_SCHEMA,
            "record_type": "footer",
            "status": "ok",
            "batch": batch,
        }
    )
    return records


def _write_stream(path: Path, records: list[dict[str, Any]]) -> None:
    path.write_text(
        "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
        encoding="utf-8",
    )


def test_read_result_stream_accepts_incremental_schema_v2_batch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.jsonl"
    _write_stream(path, _stream_records())
    candidates: list[dict[str, Any]] = []
    headers: list[dict[str, Any]] = []

    result = read_result_stream(
        path,
        header_cb=headers.append,
        candidate_cb=candidates.append,
    )

    assert result["candidate_count"] == 1
    assert result["candidates"] == []
    assert len(headers) == 1
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == "candidate-1"


def test_read_result_stream_rejects_missing_footer(tmp_path: Path) -> None:
    path = tmp_path / "result.jsonl"
    records = _stream_records()
    _write_stream(path, records[:-1])

    with pytest.raises(NativeProtocolError, match="incomplete"):
        read_result_stream(path)


def test_read_result_stream_rejects_trailing_record(tmp_path: Path) -> None:
    path = tmp_path / "result.jsonl"
    records = _stream_records()
    records.append(records[-1])
    _write_stream(path, records)

    with pytest.raises(NativeProtocolError, match="after its footer"):
        read_result_stream(path)


def test_read_result_stream_rejects_mixed_legacy_and_streaming_records(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.jsonl"
    records = [json.dumps(_result(), sort_keys=True)]
    records.extend(json.dumps(record, sort_keys=True) for record in _stream_records())
    path.write_text("\n".join(records) + "\n", encoding="utf-8")

    with pytest.raises(NativeProtocolError, match="mixes legacy"):
        read_result_stream(path)


def test_read_result_stream_rejects_legacy_trailing_record(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.jsonl"
    path.write_text(
        json.dumps(_result(), sort_keys=True)
        + "\n"
        + json.dumps(_result(), sort_keys=True)
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NativeProtocolError, match="multiple legacy"):
        read_result_stream(path)


def test_read_result_stream_rejects_header_footer_metadata_mismatch(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.jsonl"
    records = _stream_records()
    records[-1]["batch"] = copy.deepcopy(records[-1]["batch"])
    records[-1]["batch"]["walk"]["truncated"] = True
    _write_stream(path, records)

    with pytest.raises(NativeProtocolError, match="disagree on walk"):
        read_result_stream(path)


@pytest.mark.parametrize(
    ("expected_batch_index", "expected_state_offset", "message"),
    [
        (1, None, "batch index"),
        (None, 4, "state_offset"),
    ],
)
def test_read_result_stream_rejects_wrong_batch_coordinates(
    tmp_path: Path,
    expected_batch_index: int | None,
    expected_state_offset: int | None,
    message: str,
) -> None:
    path = tmp_path / "result.jsonl"
    _write_stream(path, _stream_records())

    with pytest.raises(NativeProtocolError, match=message):
        read_result_stream(
            path,
            expected_batch_index=expected_batch_index,
            expected_state_offset=expected_state_offset,
        )


def test_read_result_stream_rejects_malformed_candidate(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.jsonl"
    records = _stream_records()
    del records[1]["name"]
    _write_stream(path, records)

    with pytest.raises(NativeProtocolError, match="candidate"):
        read_result_stream(path)


def test_read_result_stream_waits_for_complete_validation_before_callbacks(
    tmp_path: Path,
) -> None:
    path = tmp_path / "result.jsonl"
    records = _stream_records(candidate_count=2)
    del records[2]["name"]
    _write_stream(path, records)
    candidates: list[dict[str, Any]] = []

    with pytest.raises(NativeProtocolError, match="candidate"):
        read_result_stream(path, candidate_cb=candidates.append)

    assert candidates == []


def test_read_result_stream_classifies_protocol_error(tmp_path: Path) -> None:
    path = tmp_path / "result.jsonl"
    path.write_text(
        json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "schema_version": PROTOCOL_SCHEMA,
                "record_type": "error",
                "status": "error",
                "error": {"kind": "protocol", "message": "invalid candidate"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NativeProtocolError, match="invalid candidate"):
        read_result_stream(path)


def test_read_result_stream_classifies_limit_error(tmp_path: Path) -> None:
    path = tmp_path / "result.jsonl"
    path.write_text(
        json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "schema_version": PROTOCOL_SCHEMA,
                "record_type": "error",
                "status": "error",
                "error": {"kind": "limit", "message": "global state limit"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NativeLimitError, match="global state limit"):
        read_result_stream(path)


@pytest.mark.parametrize(
    ("expected", "message"),
    [
        ({"expected_session_id": "session-b"}, "session_id"),
        ({"expected_job_id": 2}, "job_id"),
        ({"expected_attempt": 2}, "attempt"),
    ],
)
def test_read_result_stream_rejects_wrong_worker_identity(
    tmp_path: Path,
    expected: dict[str, Any],
    message: str,
) -> None:
    path = tmp_path / "result.jsonl"
    _write_stream(
        path,
        _stream_records(
            identity={
                "session_id": "session-a",
                "job_id": 1,
                "attempt": 1,
            }
        ),
    )

    with pytest.raises(NativeProtocolError, match=message):
        read_result_stream(path, **expected)


def test_read_result_stream_rejects_wrong_error_identity(tmp_path: Path) -> None:
    path = tmp_path / "result.jsonl"
    path.write_text(
        json.dumps(
            {
                "protocol": PROTOCOL_VERSION,
                "schema_version": PROTOCOL_SCHEMA,
                "record_type": "error",
                "status": "error",
                "identity": {
                    "session_id": "session-a",
                    "job_id": 1,
                    "attempt": 1,
                },
                "error": {"kind": "protocol", "message": "invalid candidate"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NativeProtocolError, match="job_id"):
        read_result_stream(path, expected_job_id=2)
