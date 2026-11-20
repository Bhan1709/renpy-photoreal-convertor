"""Disposable workspace construction for the native compose stage."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Self

from ..errors import NativeExecutionError
from .constants import INJECTED_SCRIPT

_RUNTIME_SCRIPT = Path(__file__).with_name("runtime") / "composite_export.rpy"


@dataclass
class NativeWorkspace:
    """A disposable Ren'Py game workspace and its isolated state paths."""

    root: Path
    game_dir: Path
    savedir: Path
    config_path: Path
    result_path: Path

    @property
    def injected_script(self) -> Path:
        """Return the injected script path in the disposable game."""
        return self.game_dir / INJECTED_SCRIPT

    def cleanup(self) -> None:
        """Remove the complete disposable workspace."""
        shutil.rmtree(self.root, ignore_errors=False)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()


_SCRIPT_SUFFIXES = frozenset({".rpy", ".rpyc", ".rpym", ".rpymc"})


def create_workspace(
    *,
    game_root: Path,
    script_files: list[str] | None,
    engine_config: dict,
    result_path: Path,
    workspace_parent: Path | None = None,
) -> NativeWorkspace:
    """Create an isolated copy of the live game scripts and referenced assets.

    Script files are copied as regular files so Ren'Py can compile them without
    ever touching the live game.  Large asset trees are mirrored with hard
    links where the filesystem supports them, falling back to ordinary copies.
    The original game tree is never modified.

    ``workspace_parent`` should be on the same filesystem as ``game_root`` when
    possible: that keeps the hardlink mirror fast.  When it is omitted the
    system temporary directory is used.
    """
    source_game = Path(game_root).resolve()
    if not source_game.is_dir():
        raise NativeExecutionError(f"Game directory does not exist: {source_game}")

    parent = Path(workspace_parent).resolve() if workspace_parent else None
    if parent is not None:
        parent.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(prefix=".compose-native-", dir=str(parent) if parent else None))
    game_dir = root / "game"
    savedir = root / "savedir"
    game_dir.mkdir(parents=True)
    savedir.mkdir(parents=True)

    try:
        _copy_scripts(source_game, game_dir, script_files)
        _mirror_runtime_assets(source_game, game_dir)
        _copy_runtime_script(game_dir)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    config_path = root / "native_config.json"
    config_path.write_text(
        json.dumps(engine_config, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    return NativeWorkspace(
        root=root,
        game_dir=game_dir,
        savedir=savedir,
        config_path=config_path,
        result_path=Path(result_path),
    )


def _copy_scripts(
    source_game: Path,
    target_game: Path,
    script_files: list[str] | None,
) -> None:
    if script_files is None:
        scripts = sorted(source_game.rglob("*.rpy"))
        scripts.extend(sorted(source_game.rglob("*_ren.py")))
    else:
        scripts = []
        for relative in script_files:
            candidate = source_game / relative
            if candidate.is_file():
                scripts.append(candidate)

    seen: set[Path] = set()
    for candidate in scripts:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        target = target_game / resolved.relative_to(source_game)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(resolved, target)


def _mirror_runtime_assets(source_game: Path, target_game: Path) -> None:
    """Mirror runtime assets into paths Ren'Py can resolve without an RPA.

    Extracted archives often contain a copy of the game scripts alongside the
    asset folders.  Those copies are skipped: the live scripts are copied
    separately (and only the configured ones), so mirroring them here would
    register duplicate labels and stale bytecode.
    """
    archive = source_game / "archive"
    if archive.is_dir():
        for child in sorted(archive.iterdir()):
            if child.suffix.lower() in _SCRIPT_SUFFIXES:
                continue
            if child.is_dir():
                _mirror_tree(child, target_game / child.name)
            elif child.is_file():
                _copy_or_link(child, target_game / child.name)

    # Audio, fonts, and translations are usually small but may contain many
    # files.  They are still mirrored rather than referenced in place so the
    # disposable project remains self-contained.
    for name in ("audio", "fonts", "tl"):
        source = source_game / name
        if source.is_dir():
            _mirror_tree(source, target_game / name)


def _mirror_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    for child in sorted(source.iterdir()):
        destination = target / child.name
        if child.is_dir():
            _mirror_tree(child, destination)
        elif child.is_file():
            _copy_or_link(child, destination)


def _copy_or_link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, target)
        return
    except FileExistsError:
        return
    except OSError:
        pass
    try:
        shutil.copy2(source, target)
    except OSError as exc:
        raise NativeExecutionError(
            f"Could not stage asset {source} into {target}: {exc}"
        ) from exc


def _copy_runtime_script(target_game: Path) -> None:
    try:
        text = _RUNTIME_SCRIPT.read_text(encoding="utf-8")
    except OSError as exc:
        raise NativeExecutionError(
            f"Native Ren'Py runtime script is missing: {_RUNTIME_SCRIPT}"
        ) from exc
    target = target_game / INJECTED_SCRIPT
    target.write_text(text, encoding="utf-8", newline="\n")


__all__ = ["NativeWorkspace", "create_workspace"]
