"""Script file discovery for the compose pass.

Responsibility: return ``.rpy`` scripts under ``game_root`` suitable for
scanning and for staging into the native Ren'Py workspace.
"""

from __future__ import annotations

from pathlib import Path

from ...utility.safe_paths import UnsafePathError
from ..config import GameConfig

_SKIP_DIRS = frozenset({"archive", "cache"})
_GENERATED_SCRIPTS = frozenset({"composite_images.rpy", "_composite_export.rpy"})


def _is_live_script(path: Path, game_root: Path) -> bool:
    """Return True for a real game script, not an archive/generated copy."""
    relative = path.relative_to(game_root)
    return (
        path.name not in _GENERATED_SCRIPTS
        and not _SKIP_DIRS.intersection(relative.parts[:-1])
    )


def _require_contained(path: Path, game_root: Path) -> Path:
    """Return a script path after rejecting a symlink escape."""
    root = game_root.resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(root):
        raise UnsafePathError(
            f"Game script path escapes the game root: {path}"
        )
    return resolved


def files(
    config: GameConfig,
    include: set[str] | None = None,
) -> list[Path]:
    """Return ``.rpy`` scripts under ``game_root`` suitable for scanning.

    ``archive/`` and ``cache/`` copies are skipped, as are the generated
    ``composite_images.rpy`` and the injected ``_composite_export.rpy``.  An
    optional ``include`` set (forward-slash paths relative to ``game_root``)
    further restricts the result; it can never re-include a skipped path.
    """
    result: list[Path] = []
    for path in sorted(
        config.game_root.rglob("*.rpy"), key=lambda p: p.as_posix().lower()
    ):
        _require_contained(path, config.game_root)
        if not _is_live_script(path, config.game_root):
            continue
        if include is None:
            result.append(path)
            continue
        relative = path.relative_to(config.game_root).as_posix()
        if relative in include:
            result.append(path)
    return result


def live_script_paths(config: GameConfig) -> list[Path]:
    """Return every live script path used by compose, apply, and pruning.

    This includes regular ``.rpy`` scripts and Ren'Py's ``*_ren.py`` files,
    while excluding archive/cache copies and converter-generated scripts.  The
    same discovery boundary is shared so a script cannot be rendered by native
    compose but then omitted from apply-time path replacement or stale-image
    retention.
    """
    result = list(files(config))
    seen = {path.resolve() for path in result}
    for path in config.game_root.rglob("*_ren.py"):
        _require_contained(path, config.game_root)
        if not _is_live_script(path, config.game_root):
            continue
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        result.append(path)
    return sorted(result, key=lambda path: path.as_posix().lower())


def live_scripts(config: GameConfig) -> list[str]:
    """Return all live script paths, relative to ``game_root``.

    Used by the native stage so the child Ren'Py process loads the same scripts
    that are physically present in the game folder.  Removing a script from the
    game folder is therefore all a user needs to do to drop it from compose.
    """
    return [
        path.relative_to(config.game_root).as_posix()
        for path in live_script_paths(config)
    ]
