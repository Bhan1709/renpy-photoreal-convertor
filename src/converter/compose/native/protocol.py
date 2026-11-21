"""Validation for the native Ren'Py compose-stage result protocol."""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, TypeGuard

from ..errors import NativeLimitError, NativeProtocolError
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


def read_result(
    path: Path,
    *,
    expected_batch_index: int | None = None,
    expected_state_offset: int | None = None,
) -> dict[str, Any]:
    """Read and validate one native-stage JSON result.

    When expected pagination coordinates are supplied, the result must belong
    to the requested batch.  This prevents a stale or foreign result file
    from being appended to the current run.
    """
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

    validate_result(
        data,
        source=path,
        expected_batch_index=expected_batch_index,
        expected_state_offset=expected_state_offset,
    )
    assert isinstance(data, dict)
    return data


def read_result_stream(
    path: Path,
    *,
    expected_batch_index: int | None = None,
    expected_state_offset: int | None = None,
    expected_session_id: str | None = None,
    expected_job_id: int | None = None,
    expected_attempt: int | None = None,
    header_cb: Callable[[dict[str, Any]], None] | None = None,
    candidate_cb: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Read and validate one schema-v2 JSONL result stream.

    A successful stream contains exactly one header record, zero or more
    candidate records, and one required footer record.  Requiring the footer
    distinguishes a complete batch from a child that stopped part-way through
    writing it.  Candidate records are validated into a disk-backed temporary
    spool first; only a completely valid batch is forwarded to the host
    callbacks, one candidate at a time.

    The legacy single-object result remains accepted for compatibility.  It is
    validated with :func:`validate_result`, but error results are rejected by
    this streaming entry point so callers cannot accidentally publish a failed
    child response.
    """
    location = f" ({path})"
    header: dict[str, Any] | None = None
    footer: dict[str, Any] | None = None
    legacy_result: dict[str, Any] | None = None
    candidate_count = 0

    try:
        # TemporaryFile is disk-backed on all supported platforms.  It keeps
        # the host's raw-candidate memory bounded without adding a persistent
        # artifact beside the native cache.
        with tempfile.TemporaryFile(mode="w+t", encoding="utf-8") as spool:
            try:
                handle = path.open("r", encoding="utf-8")
            except FileNotFoundError as exc:
                raise NativeProtocolError(
                    f"Native Ren'Py stage did not write its result file: {path}"
                ) from exc
            except OSError as exc:
                raise NativeProtocolError(
                    f"Could not read native Ren'Py stage result {path}: {exc}"
                ) from exc

            try:
                for line_number, raw_line in enumerate(handle, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise NativeProtocolError(
                            f"Native stage result stream has invalid JSON on line "
                            f"{line_number}: {exc}{location}"
                        ) from exc
                    if not isinstance(record, dict):
                        raise NativeProtocolError(
                            f"Native stage result stream records must be objects"
                            f"{location}"
                        )

                    record_type = record.get("record_type")
                    if record_type is not None and legacy_result is not None:
                        raise NativeProtocolError(
                            f"Native stage result stream mixes legacy and "
                            f"streaming records{location}"
                        )
                    if record_type is None:
                        # Legacy compatibility: exactly one complete result
                        # object.  Do not return immediately: any later record
                        # must be rejected as trailing data.
                        if legacy_result is not None:
                            raise NativeProtocolError(
                                f"Native stage result stream contains multiple "
                                f"legacy results{location}"
                            )
                        if header is not None or footer is not None:
                            raise NativeProtocolError(
                                f"Native stage result stream mixes legacy and "
                                f"streaming records{location}"
                            )
                        validate_result(
                            record,
                            source=path,
                            expected_batch_index=expected_batch_index,
                            expected_state_offset=expected_state_offset,
                        )
                        if record.get("status") == "ok":
                            _validate_expected_identity(
                                record["batch"],
                                expected_session_id=expected_session_id,
                                expected_job_id=expected_job_id,
                                expected_attempt=expected_attempt,
                                location=location,
                            )
                        if record.get("status") == "error":
                            _validate_error_identity(
                                record,
                                expected_session_id=expected_session_id,
                                expected_job_id=expected_job_id,
                                expected_attempt=expected_attempt,
                                location=location,
                            )
                            _raise_result_error(record, location)
                        legacy_result = dict(record)
                        continue

                    if record.get("protocol") != PROTOCOL_VERSION:
                        raise NativeProtocolError(
                            f"Unsupported native stage protocol "
                            f"{record.get('protocol')!r}; expected "
                            f"{PROTOCOL_VERSION!r}{location}"
                        )
                    if record.get("schema_version") != PROTOCOL_SCHEMA:
                        raise NativeProtocolError(
                            f"Unsupported native stage schema "
                            f"{record.get('schema_version')!r}; expected "
                            f"{PROTOCOL_SCHEMA!r}{location}"
                        )
                    if footer is not None:
                        raise NativeProtocolError(
                            f"Native stage result stream contains data after "
                            f"its footer{location}"
                        )

                    if record_type == "error":
                        _validate_error_identity(
                            record,
                            expected_session_id=expected_session_id,
                            expected_job_id=expected_job_id,
                            expected_attempt=expected_attempt,
                            location=location,
                        )
                        _raise_result_error(record, location)

                    if record_type == "header":
                        if header is not None:
                            raise NativeProtocolError(
                                f"Native stage result stream contains multiple "
                                f"headers{location}"
                            )
                        if record.get("status") != "ok":
                            _raise_result_error(record, location)
                        for key in (
                            "batch",
                            "layered_images",
                            "image_definitions",
                            "characters",
                        ):
                            if key not in record:
                                raise NativeProtocolError(
                                    f"Native stage result stream header is missing "
                                    f"{key!r}{location}"
                                )
                        _validate_batch(record["batch"], None, location)
                        _validate_global_metadata(record, location)
                        _validate_expected_coordinates(
                            record["batch"],
                            expected_batch_index=expected_batch_index,
                            expected_state_offset=expected_state_offset,
                            location=location,
                        )
                        _validate_expected_identity(
                            record["batch"],
                            expected_session_id=expected_session_id,
                            expected_job_id=expected_job_id,
                            expected_attempt=expected_attempt,
                            location=location,
                        )
                        header = record
                        continue

                    if header is None:
                        raise NativeProtocolError(
                            f"Native stage result stream record appeared before "
                            f"its header{location}"
                        )

                    if record_type == "candidate":
                        if record.get("status") not in (None, "ok"):
                            _raise_result_error(record, location)
                        _validate_candidate(record, location)
                        spool.write(
                            json.dumps(
                                record,
                                ensure_ascii=False,
                                separators=(",", ":"),
                                sort_keys=True,
                            )
                        )
                        spool.write("\n")
                        candidate_count += 1
                        continue

                    if record_type == "footer":
                        if record.get("status") != "ok":
                            _raise_result_error(record, location)
                        if "batch" not in record:
                            raise NativeProtocolError(
                                f"Native stage result stream footer is missing "
                                f"'batch'{location}"
                            )
                        _validate_batch(
                            record["batch"],
                            candidate_count,
                            location,
                        )
                        footer = record
                        continue

                    raise NativeProtocolError(
                        f"Native stage result stream has unknown record type "
                        f"{record_type!r}{location}"
                    )
            except OSError as exc:
                raise NativeProtocolError(
                    f"Could not read native Ren'Py stage result {path}: {exc}"
                ) from exc
            finally:
                handle.close()

            if legacy_result is not None:
                if header_cb is not None:
                    header_cb(legacy_result)
                if candidate_cb is not None:
                    for candidate in legacy_result.get("candidates") or []:
                        candidate_cb(candidate)
                return legacy_result

            if header is None:
                raise NativeProtocolError(
                    f"Native stage result stream is empty{location}"
                )
            if footer is None:
                raise NativeProtocolError(
                    f"Native stage result stream is incomplete; footer record is "
                    f"missing{location}"
                )

            header_batch = header["batch"]
            footer_batch = footer["batch"]
            # The footer may add the final empty-render count, but every other
            # batch field must describe the same immutable page.  Comparing
            # the complete mapping prevents a mixed header/footer from
            # changing pagination, walk state, budget, or worker identity
            # after the candidate records have already been accepted.
            for key in sorted(set(header_batch) | set(footer_batch)):
                if key == "empty_renders":
                    continue
                if header_batch.get(key) != footer_batch.get(key):
                    raise NativeProtocolError(
                        f"Native stage result stream header and footer disagree "
                        f"on {key}{location}"
                    )
            _validate_expected_coordinates(
                footer_batch,
                expected_batch_index=expected_batch_index,
                expected_state_offset=expected_state_offset,
                location=location,
            )
            _validate_expected_identity(
                footer_batch,
                expected_session_id=expected_session_id,
                expected_job_id=expected_job_id,
                expected_attempt=expected_attempt,
                location=location,
            )

            result = dict(header)
            result["batch"] = footer_batch
            result["candidate_count"] = candidate_count
            # The transport intentionally does not return the raw candidate
            # list.  Consumers receive the same candidates through the callback
            # after the complete batch has passed validation.
            result["candidates"] = []
            if header_cb is not None:
                header_cb(result)
            if candidate_cb is not None:
                spool.seek(0)
                for line_number, raw_line in enumerate(spool, start=1):
                    line = raw_line.strip()
                    if not line:
                        continue
                    try:
                        candidate = json.loads(line)
                    except json.JSONDecodeError as exc:  # pragma: no cover
                        raise NativeProtocolError(
                            f"Native stage candidate spool is invalid on line "
                            f"{line_number}: {exc}{location}"
                        ) from exc
                    candidate_cb(candidate)
            return result
    except OSError as exc:
        raise NativeProtocolError(
            f"Could not read native Ren'Py stage result {path}: {exc}"
        ) from exc


def _raise_result_error(record: dict[str, Any], location: str) -> None:
    """Raise the appropriate error for a failed native result."""
    error = record.get("error")
    if not isinstance(error, dict) or not error.get("message"):
        raise NativeProtocolError(
            f"Native stage error result has no error message{location}"
        )
    message = str(error["message"]).strip()
    if error.get("kind") == "limit":
        raise NativeLimitError(
            f"Native Ren'Py compose stage exceeded a global limit: {message}"
        )
    raise NativeProtocolError(
        f"Native Ren'Py compose stage failed: {message}{location}"
    )


def _validate_expected_coordinates(
    batch: dict[str, Any],
    *,
    expected_batch_index: int | None,
    expected_state_offset: int | None,
    location: str,
) -> None:
    """Validate a batch's pagination coordinates against the host request."""
    if expected_batch_index is not None and batch["index"] != expected_batch_index:
        raise NativeProtocolError(
            "Native stage batch index does not match the requested batch "
            f"(expected {expected_batch_index}, got {batch['index']}){location}"
        )
    if (
        expected_state_offset is not None
        and batch["state_offset"] != expected_state_offset
    ):
        raise NativeProtocolError(
            "Native stage batch state_offset does not match the requested batch "
            f"(expected {expected_state_offset}, got {batch['state_offset']})"
            f"{location}"
        )


def _validate_identity_mapping(
    identity: object,
    *,
    expected_session_id: str | None,
    expected_job_id: int | None,
    expected_attempt: int | None,
    location: str,
    label: str = "batch",
) -> None:
    """Validate one worker identity mapping against the host request."""
    if not isinstance(identity, dict):
        raise NativeProtocolError(
            f"Native stage {label} identity must be an object{location}"
        )
    session_id = identity.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        raise NativeProtocolError(
            f"Native stage {label} identity session_id must be a non-empty "
            f"string{location}"
        )
    job_id = identity.get("job_id")
    if not _is_integer(job_id) or job_id < 0:
        raise NativeProtocolError(
            f"Native stage {label} identity job_id must be a non-negative "
            f"integer{location}"
        )
    attempt = identity.get("attempt")
    if not _is_integer(attempt) or attempt < 1:
        raise NativeProtocolError(
            f"Native stage {label} identity attempt must be a positive integer"
            f"{location}"
        )
    if expected_session_id is not None and session_id != expected_session_id:
        raise NativeProtocolError(
            f"Native stage {label} session_id does not match the requested run "
            f"(expected {expected_session_id}, got {session_id}){location}"
        )
    if expected_job_id is not None and job_id != expected_job_id:
        raise NativeProtocolError(
            f"Native stage {label} job_id does not match the requested batch "
            f"(expected {expected_job_id}, got {job_id}){location}"
        )
    if expected_attempt is not None and attempt != expected_attempt:
        raise NativeProtocolError(
            f"Native stage {label} attempt does not match the requested batch "
            f"(expected {expected_attempt}, got {attempt}){location}"
        )


def _validate_expected_identity(
    batch: dict[str, Any],
    *,
    expected_session_id: str | None,
    expected_job_id: int | None,
    expected_attempt: int | None,
    location: str,
) -> None:
    """Validate optional worker identity fields against the host request."""
    identity = batch.get("identity")
    if identity is None:
        if any(
            value is not None
            for value in (
                expected_session_id,
                expected_job_id,
                expected_attempt,
            )
        ):
            raise NativeProtocolError(
                f"Native stage batch is missing its worker identity{location}"
            )
        return
    _validate_identity_mapping(
        identity,
        expected_session_id=expected_session_id,
        expected_job_id=expected_job_id,
        expected_attempt=expected_attempt,
        location=location,
    )


def _validate_error_identity(
    record: dict[str, Any],
    *,
    expected_session_id: str | None,
    expected_job_id: int | None,
    expected_attempt: int | None,
    location: str,
) -> None:
    """Validate the identity carried by one native error envelope."""
    identity = record.get("identity")
    if identity is None:
        batch = record.get("batch")
        if isinstance(batch, dict):
            identity = batch.get("identity")
    has_expected_identity = any(
        value is not None
        for value in (
            expected_session_id,
            expected_job_id,
            expected_attempt,
        )
    )
    if identity is None:
        if has_expected_identity:
            raise NativeProtocolError(
                f"Native stage error result is missing its worker identity"
                f"{location}"
            )
        return
    _validate_identity_mapping(
        identity,
        expected_session_id=expected_session_id,
        expected_job_id=expected_job_id,
        expected_attempt=expected_attempt,
        location=location,
        label="error result",
    )


def _is_integer(value: object) -> TypeGuard[int]:
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
        not isinstance(content_hash, str)
        or len(content_hash) != 64
        or any(char not in "0123456789abcdef" for char in content_hash.lower())
    ):
        raise NativeProtocolError(
            f"Native stage candidate content_hash must be a SHA-256 digest or null"
            f"{location}"
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
    if render_file is not None and (
        "/" in render_file
        or "\\" in render_file
        or Path(render_file).name != render_file
    ):
        raise NativeProtocolError(
            f"Native stage candidate render_file must be a file name{location}"
        )
    if content_hash is None and render_file is not None:
        raise NativeProtocolError(
            f"Native stage candidate render_file requires content_hash{location}"
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
        if render_file != f"{content_hash}.png":
            raise NativeProtocolError(
                "Native stage candidate render_file must match its content_hash"
                f"{location}"
            )


def _validate_batch(
    batch: object,
    candidate_count: int | None,
    location: str,
) -> None:
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
    if candidate_count is not None and rendered != candidate_count:
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

    budget = batch.get("budget")
    if budget is not None:
        if not isinstance(budget, dict):
            raise NativeProtocolError(
                f"Native stage batch budget must be an object{location}"
            )
        for key in (
            "max_total_states",
            "max_total_variants",
            "cfg_states",
            "synthetic_states",
            "condition_variants",
            "variants",
            "total_states",
            "skipped_variants",
        ):
            value = budget.get(key)
            if not _is_integer(value) or int(value) < 0:
                raise NativeProtocolError(
                    f"Native stage batch budget {key} must be a non-negative "
                    f"integer{location}"
                )
        if not isinstance(budget.get("truncated"), bool):
            raise NativeProtocolError(
                f"Native stage batch budget truncated must be a boolean{location}"
            )

    _validate_expected_identity(
        batch,
        expected_session_id=None,
        expected_job_id=None,
        expected_attempt=None,
        location=location,
    )


def _validate_global_metadata(data: dict[str, Any], location: str) -> None:
    """Validate discovery metadata shared by legacy and streaming results."""
    layered_images = data.get("layered_images")
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

    image_definitions = data.get("image_definitions")
    if not isinstance(image_definitions, dict) or not all(
        isinstance(definition, dict) or definition is None
        for definition in image_definitions.values()
    ):
        raise NativeProtocolError(
            f"Native stage image_definitions must be an object{location}"
        )

    characters = data.get("characters")
    if not isinstance(characters, dict) or not all(
        isinstance(entry, dict) for entry in characters.values()
    ):
        raise NativeProtocolError(
            f"Native stage characters must be an object{location}"
        )


def validate_result(
    data: object,
    *,
    source: Path | None = None,
    expected_batch_index: int | None = None,
    expected_state_offset: int | None = None,
) -> None:
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

    batch = data["batch"]
    _validate_batch(batch, len(candidates), location)

    if expected_batch_index is not None and batch["index"] != expected_batch_index:
        raise NativeProtocolError(
            "Native stage batch index does not match the requested batch "
            f"(expected {expected_batch_index}, got {batch['index']}){location}"
        )
    if (
        expected_state_offset is not None
        and batch["state_offset"] != expected_state_offset
    ):
        raise NativeProtocolError(
            "Native stage batch state_offset does not match the requested batch "
            f"(expected {expected_state_offset}, got {batch['state_offset']}){location}"
        )

    _validate_global_metadata(data, location)


__all__ = ["read_result", "read_result_stream", "validate_result"]
