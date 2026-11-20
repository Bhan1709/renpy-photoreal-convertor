"""Script file discovery for the compose pass.

Responsibility: return ``.rpy`` scripts under ``game_root`` suitable for
scanning and for staging into the native Ren'Py workspace.
"""

from __future__ import annotations

from pathlib import Path

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
        if not _is_live_script(path, config.game_root):
            continue
        if include is None:
            result.append(path)
            continue
        relative = path.relative_to(config.game_root).as_posix()
        if relative in include:
            result.append(path)
    return result


def live_scripts(config: GameConfig) -> list[str]:
    """Return all live script paths, relative to ``game_root``.

    Used by the native stage so the child Ren'Py process loads the same scripts
    that are physically present in the game folder.  Removing a script from the
    game folder is therefore all a user needs to do to drop it from compose.
    """
    scripts = {
        path.relative_to(config.game_root).as_posix()
        for path in files(config)
    }
    scripts.update(
        path.relative_to(config.game_root).as_posix()
        for path in config.game_root.rglob("*_ren.py")
        if _is_live_script(path, config.game_root)
    )
    return sorted(scripts)
