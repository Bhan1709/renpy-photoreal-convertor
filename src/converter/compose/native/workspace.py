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
        """Remove the complete disposable workspace.

        Cleanup is idempotent so a caller can safely attempt it after an
        earlier partial cleanup.  The stage's ``finally`` block relies on
        this not masking the original native-stage exception.
        """
        if not self.root.exists():
            return
        # Remove the result payload first.  If directory removal itself fails,
        # the potentially sensitive native result is still not left behind.
        try:
            self.result_path.unlink(missing_ok=True)
        except OSError:
            pass
        shutil.rmtree(self.root, ignore_errors=False)

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        try:
            self.cleanup()
        except OSError as cleanup_exc:
            if exc is not None:
                exc.add_note(
                    f"Native workspace cleanup also failed: {cleanup_exc}"
                )
                return
            raise


_SCRIPT_SUFFIXES = frozenset({".rpy", ".rpyc", ".rpym", ".rpymc"})

# Names used by the host/runtime inside a disposable workspace.  A result
# file must not be allowed to alias one of these paths: doing so could make a
# later batch consume a config, state cache, or log as its result payload.
_RESERVED_RESULT_NAMES = frozenset(
    {
        "game",
        "savedir",
        "native_config.json",
        "renders",
        "states.json",
        "progress.log",
        "batch_stdout.log",
        "batch_stderr.log",
        "logs",
        "save-root",
        "multipersistent",
    }
)


def create_workspace(
    *,
    game_root: Path,
    script_files: list[str] | None,
    engine_config: dict,
    result_path: Path | None = None,
    workspace_parent: Path | None = None,
    archive_dirs: list[Path] | None = None,
) -> NativeWorkspace:
    """Create an isolated copy of the live game scripts and referenced assets.

    Script files are copied as regular files so Ren'Py can compile them without
    ever touching the live game.  Large asset trees are mirrored with hard
    links where the filesystem supports them, falling back to ordinary copies.
    The original game tree is never modified.

    ``workspace_parent`` should be on the same filesystem as ``game_root`` when
    possible: that keeps the hardlink mirror fast.  When it is omitted the
    system temporary directory is used.

    ``archive_dirs`` lists the extracted asset roots that should be mirrored.
    When omitted, ``game_root / "archive"`` is used for compatibility.

    The result file always lives inside the disposable workspace.  The optional
    *result_path* argument is retained for source compatibility, but only its
    file name is used; an external path is never shared between native runs.
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
        _mirror_runtime_assets(source_game, game_dir, archive_dirs)
        _copy_runtime_script(game_dir)
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise

    config_path = root / "native_config.json"
    config_path.write_text(
        json.dumps(engine_config, indent=2, sort_keys=True),
        encoding="utf-8",
    )

    result_name = Path(result_path).name if result_path is not None else ""
    result_key = result_name.casefold()
    if (
        not result_name
        or result_name in {".", ".."}
        or result_key in _RESERVED_RESULT_NAMES
        or result_key == config_path.name.casefold()
    ):
        result_name = "native-result.json"
    isolated_result_path = root / result_name

    return NativeWorkspace(
        root=root,
        game_dir=game_dir,
        savedir=savedir,
        config_path=config_path,
        result_path=isolated_result_path,
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


def _mirror_runtime_assets(
    source_game: Path,
    target_game: Path,
    archive_dirs: list[Path] | None = None,
) -> None:
    """Mirror runtime assets into paths Ren'Py can resolve without an RPA.

    Extracted archives often contain a copy of the game scripts alongside the
    asset folders.  Those copies are skipped: the live scripts are staged
    separately, so mirroring them here would register duplicate labels and
    stale bytecode.
    """
    roots = archive_dirs or [source_game / "archive"]
    for archive in roots:
        if not archive.is_dir():
            continue
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
