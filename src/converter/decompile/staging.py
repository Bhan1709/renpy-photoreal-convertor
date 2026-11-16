"""Staging: copy relevant files from the source game folder into the
consolidated ``src/renpy_game/game`` folder."""

from __future__ import annotations

import shutil
from pathlib import Path

from .constants import COPY_EXTENSIONS, COPY_FILENAMES, GAME_FOLDER


def _should_copy(path: Path) -> bool:
    return path.suffix in COPY_EXTENSIONS or path.name in COPY_FILENAMES


def copy_game_files(
    game_folder: Path, dest: Path = GAME_FOLDER
) -> list[Path]:
    r"""Copy relevant files from ``game_folder`` into ``dest``.

    Relative directory structure is preserved, so
    ``game/archive.rpa`` lands at ``dest/archive.rpa`` and
    ``game/tl/None/common.rpyc`` lands at ``dest/tl/None/common.rpyc``.
    ``dest`` is recreated cleanly on every call.

    Returns the list of copied destination paths.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True, exist_ok=True)

    copied: list[Path] = []
    for path in sorted(game_folder.rglob("*")):
        if not path.is_file():
            continue
        if not _should_copy(path):
            continue
        rel = path.relative_to(game_folder)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(target)

    return copied
