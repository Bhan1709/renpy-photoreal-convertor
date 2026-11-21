"""Atomic file replacement helpers shared by converter stages."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any


class AtomicWriteError(RuntimeError):
    """Raised when an atomic JSON publication cannot be fully rolled back."""


def _write_temporary_json(path: Path, payload: Mapping[str, Any]) -> Path:
    """Serialize *payload* to a temporary sibling of *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _write_temporary_bytes(path: Path, payload: bytes) -> Path:
    """Write *payload* to a temporary sibling of *path*."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise
    return temporary_path


def _restore_previous(target: Path, previous: bytes | None) -> None:
    """Atomically restore one target to its pre-publication contents."""
    if previous is None:
        target.unlink(missing_ok=True)
        return

    temporary_path = _write_temporary_bytes(target, previous)
    try:
        os.replace(temporary_path, target)
    finally:
        temporary_path.unlink(missing_ok=True)


def atomic_write_json(path: str | Path, payload: Mapping[str, Any]) -> Path:
    """Atomically replace *path* with one JSON document."""
    target = Path(path)
    temporary_path: Path | None = None
    try:
        temporary_path = _write_temporary_json(target, payload)
        os.replace(temporary_path, target)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
    return target


def atomic_write_json_files(payloads: Mapping[Path, Mapping[str, Any]]) -> None:
    """Stage and atomically replace a group of JSON documents.

    Every document is serialized before any live target is changed.  The files
    are then replaced in insertion order, with the caller's final entry acting
    as the commit marker.  If a replacement fails, already committed files are
    restored atomically from their previous contents before the original error
    is raised.  An incomplete rollback is reported as :class:`AtomicWriteError`.
    """
    if not payloads:
        return

    staged: list[tuple[Path, Path]] = []
    backups: dict[Path, bytes | None] = {}
    committed: list[Path] = []
    try:
        for target, payload in payloads.items():
            path = Path(target)
            backups[path] = path.read_bytes() if path.is_file() else None
            staged.append((path, _write_temporary_json(path, payload)))

        try:
            for target, temporary_path in staged:
                os.replace(temporary_path, target)
                committed.append(target)
        except BaseException as exc:
            rollback_failures: list[str] = []
            for target in reversed(committed):
                previous = backups.get(target)
                try:
                    _restore_previous(target, previous)
                except OSError as rollback_exc:
                    rollback_failures.append(f"{target}: {rollback_exc}")
            if rollback_failures:
                details = "; ".join(rollback_failures)
                error = AtomicWriteError(
                    "Atomic JSON publication failed "
                    f"({exc}) and rollback was incomplete: {details}"
                )
                if isinstance(exc, Exception):
                    raise error from exc
                exc.add_note(str(error))
            raise
    finally:
        for _target, temporary_path in staged:
            temporary_path.unlink(missing_ok=True)


__all__ = ["AtomicWriteError", "atomic_write_json", "atomic_write_json_files"]
