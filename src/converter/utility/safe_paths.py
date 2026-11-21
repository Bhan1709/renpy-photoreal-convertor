"""Containment helpers for paths derived from manifests and archives.

Manifest bundles and Ren'Py archives contain path strings that were not
necessarily produced by the current converter.  Every such path must be
treated as untrusted before it is used to read, replace, move, or delete a
live file.
"""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath


class UnsafePathError(ValueError):
    """Raised when an untrusted path escapes its approved root."""


_GENERATED_IMAGE_STATUS_DIRS = frozenset({"static", "dynamic_review"})
_GENERATED_IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp"})
_RESERVED_OUTPUT_DIRS = frozenset({"reports", "rejected"})


def safe_relative_path(value: str | Path, *, field_name: str = "path") -> Path:
    """Return one portable relative path after rejecting unsafe syntax.

    Both POSIX and Windows path syntax are checked so a value authored on one
    platform cannot escape its intended root when consumed on another.
    Absolute paths, drive-qualified paths, UNC/rooted paths, empty paths,
    NUL characters, and any ``..`` component are rejected.
    """
    raw = os.fspath(value)
    if not isinstance(raw, str):  # pragma: no cover - os.fspath contract
        raise TypeError("path must be a string or path-like object")
    if not raw.strip() or "\x00" in raw:
        raise UnsafePathError(f"Unsafe {field_name}: {raw!r}")

    normalized = raw.replace("\\", "/")
    native = Path(normalized)
    windows = PureWindowsPath(normalized)
    if (
        native.is_absolute()
        or native.anchor
        or windows.is_absolute()
        or windows.drive
        or windows.root
        or not native.parts
        or ".." in native.parts
    ):
        raise UnsafePathError(f"Unsafe {field_name}: {raw!r}")
    return native


def resolve_absolute_under_root(
    root: str | Path,
    candidate: str | Path,
    *,
    field_name: str = "path",
) -> Path:
    """Resolve an operator-selected path and require containment under *root*.

    Relative candidates are interpreted relative to *root*.  Absolute
    candidates keep their absolute meaning but must still resolve inside the
    approved root.
    """
    root_path = Path(root).resolve()
    candidate_path = Path(candidate)
    if not candidate_path.is_absolute():
        candidate_path = root_path / candidate_path
    target = candidate_path.resolve()
    if not target.is_relative_to(root_path):
        raise UnsafePathError(
            f"Unsafe {field_name} escapes {root_path}: {candidate!r}"
        )
    return target


def resolve_under_root(
    root: str | Path,
    relative_path: str | Path,
    *,
    field_name: str = "path",
) -> Path:
    """Resolve *relative_path* under *root* and require containment.

    The returned path is fully resolved.  Existing symlinks are therefore
    followed before the containment check, preventing a link inside the root
    from redirecting a live write outside it.
    """
    root_path = Path(root).resolve()
    relative = safe_relative_path(relative_path, field_name=field_name)
    target = (root_path / relative).resolve()
    if not target.is_relative_to(root_path):
        raise UnsafePathError(
            f"Unsafe {field_name} escapes {root_path}: {relative_path!r}"
        )
    return target


def game_script_relative_path(
    game_root: str | Path,
    script: str | Path,
) -> Path:
    """Return a safe script path relative to the Ren'Py ``game`` directory.

    Manifest usage locations conventionally use ``game/foo.rpy`` paths that
    are relative to the project directory.  Older records may use ``foo.rpy``
    directly.  Both forms are accepted, but the resulting file must remain
    inside the game directory after symlink resolution.
    """
    relative = safe_relative_path(script, field_name="game script path")
    parts = list(relative.parts)
    if parts and parts[0].casefold() == "game":
        parts = parts[1:]
    if not parts:
        raise UnsafePathError(f"Unsafe game script path: {script!r}")

    relative = Path(*parts)
    root_path = Path(game_root).resolve()
    target = (root_path / relative).resolve()
    if not target.is_relative_to(root_path):
        raise UnsafePathError(
            f"Unsafe game script path escapes {root_path}: {script!r}"
        )
    return relative


def resolve_game_script_path(
    game_root: str | Path,
    script: str | Path,
) -> Path:
    """Resolve one manifest script reference to a contained live path."""
    relative = game_script_relative_path(game_root, script)
    root_path = Path(game_root).resolve()
    target = (root_path / relative).resolve()
    if not target.is_relative_to(root_path):  # pragma: no cover - defensive
        raise UnsafePathError(
            f"Unsafe game script path escapes {root_path}: {script!r}"
        )
    return target


def resolve_game_output_path(
    game_root: str | Path,
    output_root: str | Path,
    composite_path: str | Path,
    *,
    excluded_roots: tuple[str | Path, ...] = (),
) -> Path:
    """Resolve one generated-composite image under the configured output root.

    Composite records are relative to the project directory (the parent of
    the Ren'Py ``game`` directory).  The resolved target must be an image inside
    a generated image root (``static/`` or ``dynamic_review/``) below the
    configured composite output directory and must use a supported image
    suffix.  Report, rejected-item, and other explicitly excluded roots cannot
    be treated as generated images.
    """
    project_root = Path(game_root).parent.resolve()
    relative = safe_relative_path(
        composite_path,
        field_name="composite path",
    )
    target = (project_root / relative).resolve()
    output_path = Path(output_root).resolve()
    if not target.is_relative_to(output_path):
        raise UnsafePathError(
            f"Unsafe composite path escapes {output_path}: {composite_path!r}"
        )

    try:
        output_relative = target.relative_to(output_path)
    except ValueError as exc:  # pragma: no cover - guarded above
        raise UnsafePathError(
            f"Unsafe composite path escapes {output_path}: {composite_path!r}"
        ) from exc
    parts = output_relative.parts
    if (
        len(parts) < 3
        or parts[0] not in _GENERATED_IMAGE_STATUS_DIRS
        or not parts[1]
        or any(part.casefold() in _RESERVED_OUTPUT_DIRS for part in parts[:-1])
        or target.suffix.lower() not in _GENERATED_IMAGE_SUFFIXES
    ):
        raise UnsafePathError(
            f"Unsafe composite path is not a generated image: {composite_path!r}"
        )

    for excluded in excluded_roots:
        excluded_path = Path(excluded).resolve()
        if target == excluded_path or target.is_relative_to(excluded_path):
            raise UnsafePathError(
                f"Unsafe composite path is inside an excluded root "
                f"{excluded_path}: {composite_path!r}"
            )
    return target


__all__ = [
    "UnsafePathError",
    "game_script_relative_path",
    "resolve_absolute_under_root",
    "resolve_game_output_path",
    "resolve_game_script_path",
    "resolve_under_root",
    "safe_relative_path",
]
