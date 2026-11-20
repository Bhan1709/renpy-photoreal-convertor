"""Game folder discovery."""

from __future__ import annotations

from pathlib import Path

from cli import error

from .constants import GAME_FOLDER, RENPY_GAMES, ROOT

# Cache for the located game folder relative path (kept for backward
# compatibility so other subpackages can still query it).
_game_folder_rel_path: Path | None = None


def set_game_folder_rel_path(rel_path: Path) -> None:
    r"""Cache the relative path of the \game folder for other subpackages."""
    global _game_folder_rel_path
    _game_folder_rel_path = rel_path


def get_game_folder_rel_path() -> Path | None:
    r"""Get the cached relative path of the \game folder."""
    return _game_folder_rel_path


def find_game_folder(
    renpy_games: Path = RENPY_GAMES,
    *,
    exclude: Path | None = None,
) -> Path | None:
    r"""Return the first directory named ``game`` found under ``renpy_games``.

    Ren'Py games come in all shapes, but they all ship a ``\game`` folder.
    We recursively walk ``src/renpy_game`` and return the first match so the
    rest of the flow operates on a single, well-known location.

    The consolidated output folder (``src/renpy_game/game``) is deliberately
    excluded so re-runs do not treat their own output as a source game.  A
    caller using a custom staging destination can exclude that path explicitly.
    """
    if not renpy_games.exists():
        error(f"No {renpy_games} directory found")
        return None

    try:
        excluded = (exclude or GAME_FOLDER).resolve()
    except Exception:  # pragma: no cover - defensive  # noqa: BLE001
        excluded = exclude or GAME_FOLDER

    matches: list[Path] = []
    for item in renpy_games.rglob("*"):
        if not (item.is_dir() and item.name == "game"):
            continue
        try:
            resolved = item.resolve()
        except Exception:  # pragma: no cover - defensive  # noqa: BLE001
            resolved = item
        if resolved == excluded or resolved.is_relative_to(excluded):
            # Skip our own consolidated output folder (and anything nested
            # below a custom staging destination) on re-runs.
            continue
        matches.append(item)

    if not matches:
        return None

    matches.sort()
    return matches[0]


def find_game_folders() -> list[tuple[Path, Path, Path]]:
    r"""Backward-compatible wrapper around :func:`find_game_folder`.

    Returns a single-element list of ``(game_dir, rel_path, target_folder)``
    tuples for the first game folder found, mirroring the legacy API used by
    :mod:`converter.pipeline`.  Prefer :func:`find_game_folder` /
    :func:`converter.decompile.flow.process_game` for the generalized
    consolidated-folder flow.
    """
    game_folder = find_game_folder()
    if game_folder is None:
        return []
    game_dir = game_folder.parent
    rel_path = game_folder.relative_to(ROOT)
    return [(game_dir, rel_path, game_folder)]
