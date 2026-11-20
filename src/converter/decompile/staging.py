"""Staging: copy relevant files from the source game folder into the
consolidated ``src/renpy_game/game`` folder."""

from __future__ import annotations

import shutil
from pathlib import Path

from cli import loading

from .constants import COPY_EXTENSIONS, COPY_FILENAMES, GAME_FOLDER


def _should_copy(path: Path) -> bool:
    return path.suffix.lower() in COPY_EXTENSIONS or path.name in COPY_FILENAMES


def _validate_staging_paths(game_folder: Path, dest: Path) -> None:
    """Reject destinations that overlap the source game tree."""
    source = game_folder.resolve()
    destination = dest.resolve()
    if (
        destination == source
        or destination.is_relative_to(source)
        or source.is_relative_to(destination)
    ):
        raise ValueError(
            "staging destination overlaps the source game folder: "
            f"{destination} and {source}"
        )


def _extracted_archive_roots(game_folder: Path) -> set[Path]:
    """Return sibling directories previously extracted from source archives."""
    roots: set[Path] = set()
    for archive in game_folder.rglob("*.rpa"):
        roots.add((archive.parent / archive.stem).resolve())
    return roots


def _is_inside_extracted_archive(path: Path, roots: set[Path]) -> bool:
    """Return whether a source file belongs to a prior archive extraction."""
    resolved = path.resolve()
    return any(resolved.is_relative_to(root) for root in roots)


def copy_game_files(game_folder: Path, dest: Path = GAME_FOLDER) -> list[Path]:
    r"""Copy relevant files from ``game_folder`` into ``dest``.

    Relative directory structure is preserved, so
    ``game/archive.rpa`` lands at ``dest/archive.rpa`` and
    ``game/tl/None/common.rpyc`` lands at ``dest/tl/None/common.rpyc``.
    Files inside a source archive's previous extraction directory are skipped;
    the archive is staged and extracted again in the destination. ``dest`` is
    recreated cleanly on every call.

    Returns the list of copied destination paths.
    """
    copied: list[Path] = []
    _validate_staging_paths(game_folder, dest)
    with loading("Copying game files into the consolidated folder..."):
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True, exist_ok=True)

        extracted_roots = _extracted_archive_roots(game_folder)
        for path in sorted(game_folder.rglob("*")):
            if not path.is_file():
                continue
            if not _should_copy(path):
                continue
            if _is_inside_extracted_archive(path, extracted_roots):
                continue
            rel = path.relative_to(game_folder)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target)
            copied.append(target)

    return copied
