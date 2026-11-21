"""Validation for the native Ren'Py compose-stage result protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import NativeProtocolError
from .constants import PROTOCOL_SCHEMA, PROTOCOL_VERSION

_REQUIRED_CANDIDATE_FIELDS = (
    "candidate_id",
    "name",
    "kind",
    "attributes",
    "image_name",
    "condition_assignments",
    "content_hash",
    "width",
    "height",
    "render_file",
    "layers",
    "sources",
    "unresolved_errors",
    "usages",
    "error",
)


def read_result(path: Path) -> dict[str, Any]:
    """Read and validate one native-stage JSON result."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise NativeProtocolError(
            f"Native Ren'Py stage did not write its result file: {path}"
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise NativeProtocolError(
            f"Could not read native Ren'Py stage result {path}: {exc}"
        ) from exc

    validate_result(data, source=path)
    assert isinstance(data, dict)
    return data


def _is_integer(value: object) -> bool:
    """Return whether *value* is a JSON integer (not a boolean)."""
    return isinstance(value, int) and not isinstance(value, bool)


def _require_string(value: object, label: str, location: str) -> None:
    if not isinstance(value, str) or not value:
        raise NativeProtocolError(f"Native stage {label} must be a non-empty string{location}")


def _require_string_list(value: object, label: str, location: str) -> None:
    if not isinstance(value, list) or not all(
        isinstance(item, str) for item in value
    ):
        raise NativeProtocolError(
            f"Native stage {label} must be a list of strings{location}"
        )


def _validate_layer(layer: object, location: str) -> None:
    if not isinstance(layer, dict):
        raise NativeProtocolError(f"Native stage candidate layer must be an object{location}")
    for key in (
        "sources",
        "attribute",
        "group",
        "conditions",
        "unresolved",
        "unresolved_errors",
    ):
        if key not in layer:
            raise NativeProtocolError(
                f"Native stage candidate layer is missing {key!r}{location}"
            )
    _require_string_list(layer["sources"], "candidate layer sources", location)
    _require_string_list(layer["conditions"], "candidate layer conditions", location)
    _require_string_list(layer["unresolved"], "candidate layer unresolved", location)
    _require_string_list(
        layer["unresolved_errors"], "candidate layer unresolved_errors", location
    )
    for key in ("attribute", "group"):
        value = layer[key]
        if value is not None and not isinstance(value, str):
            raise NativeProtocolError(
                f"Native stage candidate layer {key} must be a string or null{location}"
            )


def _validate_usage(usage: object, location: str) -> None:
    if not isinstance(usage, dict):
        raise NativeProtocolError(
            f"Native stage candidate usage must be an object{location}"
        )
    for key in ("script", "line", "statement"):
        if key not in usage:
            raise NativeProtocolError(
                f"Native stage candidate usage is missing {key!r}{location}"
            )
    _require_string(usage["script"], "candidate usage script", location)
    _require_string(usage["statement"], "candidate usage statement", location)
    if not _is_integer(usage["line"]) or int(usage["line"]) < 0:
        raise NativeProtocolError(
            f"Native stage candidate usage line must be a non-negative integer{location}"
        )


def _validate_candidate(candidate: object, location: str) -> None:
    """Validate one candidate before the host attempts to adapt it."""
    if not isinstance(candidate, dict):
        raise NativeProtocolError(f"Native stage candidates must be objects{location}")

    for key in _REQUIRED_CANDIDATE_FIELDS:
        if key not in candidate:
            raise NativeProtocolError(
                f"Native stage candidate is missing {key!r}{location}"
            )

    for key in ("candidate_id", "name", "kind"):
        _require_string(candidate[key], f"candidate {key}", location)

    _require_string_list(candidate["attributes"], "candidate attributes", location)
    _require_string_list(candidate["image_name"], "candidate image_name", location)
    _require_string_list(candidate["sources"], "candidate sources", location)
    _require_string_list(
        candidate["unresolved_errors"], "candidate unresolved_errors", location
    )

    assignments = candidate["condition_assignments"]
    if not isinstance(assignments, dict) or not all(
        isinstance(key, str) for key in assignments
    ):
        raise NativeProtocolError(
            f"Native stage candidate condition_assignments must be an object{location}"
        )

    content_hash = candidate["content_hash"]
    if content_hash is not None and (
        not isinstance(content_hash, str) or not content_hash
    ):
        raise NativeProtocolError(
            f"Native stage candidate content_hash must be a string or null{location}"
        )

    for key in ("width", "height"):
        value = candidate[key]
        if not _is_integer(value) or int(value) < 0:
            raise NativeProtocolError(
                f"Native stage candidate {key} must be a non-negative integer{location}"
            )

    render_file = candidate["render_file"]
    if render_file is not None and (
        not isinstance(render_file, str) or not render_file
    ):
        raise NativeProtocolError(
            f"Native stage candidate render_file must be a string or null{location}"
        )

    error = candidate["error"]
    if error is not None and not isinstance(error, str):
        raise NativeProtocolError(
            f"Native stage candidate error must be a string or null{location}"
        )

    layers = candidate["layers"]
    if not isinstance(layers, list):
        raise NativeProtocolError(
            f"Native stage candidate layers must be a list{location}"
        )
    for layer in layers:
        _validate_layer(layer, location)

    usages = candidate["usages"]
    if not isinstance(usages, list):
        raise NativeProtocolError(
            f"Native stage candidate usages must be a list{location}"
        )
    for usage in usages:
        _validate_usage(usage, location)

    if error is None and content_hash is not None:
        if int(candidate["width"]) <= 0 or int(candidate["height"]) <= 0:
            raise NativeProtocolError(
                f"Native stage rendered candidate must have positive dimensions{location}"
            )
        if render_file is None:
            raise NativeProtocolError(
                f"Native stage rendered candidate must have a render_file{location}"
            )


def _validate_batch(batch: object, candidate_count: int, location: str) -> None:
    """Validate batch counters and the CFG walk state."""
    if not isinstance(batch, dict):
        raise NativeProtocolError(f"Native stage batch must be an object{location}")

    for key in (
        "index",
        "state_offset",
        "rendered",
        "total_states",
        "next_offset",
        "walk",
    ):
        if key not in batch:
            raise NativeProtocolError(
                f"Native stage batch is missing {key!r}{location}"
            )

    for key in ("index", "state_offset", "rendered", "total_states"):
        value = batch[key]
        if not _is_integer(value) or int(value) < 0:
            raise NativeProtocolError(
                f"Native stage batch {key} must be a non-negative integer{location}"
            )

    state_offset = int(batch["state_offset"])
    rendered = int(batch["rendered"])
    total_states = int(batch["total_states"])
    next_offset = batch["next_offset"]

    if state_offset > total_states:
        raise NativeProtocolError(
            f"Native stage batch state_offset exceeds total_states{location}"
        )
    if rendered != candidate_count:
        raise NativeProtocolError(
            f"Native stage batch rendered count does not match candidates{location}"
        )
    if state_offset + rendered > total_states:
        raise NativeProtocolError(
            f"Native stage batch rendered count exceeds total_states{location}"
        )

    if next_offset is None:
        if state_offset + rendered < total_states:
            raise NativeProtocolError(
                f"Native stage batch next_offset is required before the final page{location}"
            )
    elif not _is_integer(next_offset) or int(next_offset) <= state_offset:
        raise NativeProtocolError(
            f"Native stage batch next_offset must advance beyond state_offset{location}"
        )
    elif int(next_offset) != state_offset + rendered:
        raise NativeProtocolError(
            f"Native stage batch next_offset does not match rendered count{location}"
        )
    elif int(next_offset) > total_states:
        raise NativeProtocolError(
            f"Native stage batch next_offset exceeds total_states{location}"
        )

    walk = batch["walk"]
    if not isinstance(walk, dict):
        raise NativeProtocolError(f"Native stage batch walk must be an object{location}")
    if "processed" not in walk or "truncated" not in walk:
        raise NativeProtocolError(
            f"Native stage batch walk must include processed and truncated{location}"
        )
    if not _is_integer(walk["processed"]) or int(walk["processed"]) < 0:
        raise NativeProtocolError(
            f"Native stage batch walk processed must be a non-negative integer{location}"
        )
    if not isinstance(walk["truncated"], bool):
        raise NativeProtocolError(
            f"Native stage batch walk truncated must be a boolean{location}"
        )


def validate_result(data: object, *, source: Path | None = None) -> None:
    """Validate the stable fields of a native-stage result."""
    location = f" ({source})" if source is not None else ""
    if not isinstance(data, dict):
        raise NativeProtocolError(f"Native stage result is not an object{location}")

    version = data.get("protocol")
    if version != PROTOCOL_VERSION:
        raise NativeProtocolError(
            f"Unsupported native stage protocol {version!r}; "
            f"expected {PROTOCOL_VERSION!r}{location}"
        )

    schema = data.get("schema_version")
    if schema != PROTOCOL_SCHEMA:
        raise NativeProtocolError(
            f"Unsupported native stage schema {schema!r}; "
            f"expected {PROTOCOL_SCHEMA!r}{location}"
        )

    if data.get("status") not in {"ok", "error"}:
        raise NativeProtocolError(
            f"Native stage result has an invalid status{location}"
        )

    if data.get("status") == "error":
        error = data.get("error")
        if not isinstance(error, dict) or not error.get("message"):
            raise NativeProtocolError(
                f"Native stage error result has no error message{location}"
            )
        return

    for key in (
        "batch",
        "candidates",
        "layered_images",
        "image_definitions",
        "characters",
    ):
        if key not in data:
            raise NativeProtocolError(
                f"Native stage result is missing {key!r}{location}"
            )

    candidates = data["candidates"]
    if not isinstance(candidates, list):
        raise NativeProtocolError(
            f"Native stage candidates must be a list{location}"
        )
    for candidate in candidates:
        _validate_candidate(candidate, location)

    _validate_batch(data["batch"], len(candidates), location)

    layered_images = data["layered_images"]
    if not isinstance(layered_images, list) or not all(
        isinstance(entry, dict) for entry in layered_images
    ):
        raise NativeProtocolError(
            f"Native stage layered_images must be a list of objects{location}"
        )
    for entry in layered_images:
        if "name" not in entry or not isinstance(entry["name"], str):
            raise NativeProtocolError(
                f"Native stage layered image must have a name{location}"
            )

    image_definitions = data["image_definitions"]
    if not isinstance(image_definitions, dict) or not all(
        isinstance(definition, dict) or definition is None
        for definition in image_definitions.values()
    ):
        raise NativeProtocolError(
            f"Native stage image_definitions must be an object{location}"
        )

    characters = data["characters"]
    if not isinstance(characters, dict) or not all(
        isinstance(entry, dict) for entry in characters.values()
    ):
        raise NativeProtocolError(
            f"Native stage characters must be an object{location}"
        )


__all__ = ["read_result", "validate_result"]
