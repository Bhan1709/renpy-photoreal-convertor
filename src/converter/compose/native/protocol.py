"""Validation for the native Ren'Py compose-stage result protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..errors import NativeProtocolError
from .constants import PROTOCOL_SCHEMA, PROTOCOL_VERSION


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
    return data


def validate_result(data: dict[str, Any], *, source: Path | None = None) -> None:
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

    if not isinstance(data["candidates"], list):
        raise NativeProtocolError(
            f"Native stage candidates must be a list{location}"
        )
    if not isinstance(data["layered_images"], list):
        raise NativeProtocolError(
            f"Native stage layered_images must be a list{location}"
        )
    if not isinstance(data["image_definitions"], dict):
        raise NativeProtocolError(
            f"Native stage image_definitions must be an object{location}"
        )
    if not isinstance(data["characters"], dict):
        raise NativeProtocolError(
            f"Native stage characters must be an object{location}"
        )


__all__ = ["read_result", "validate_result"]
