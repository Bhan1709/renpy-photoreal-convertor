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


def _is_link_like(path: Path) -> bool:
    """Return whether one path is a symbolic link or Windows junction."""
    try:
        return path.is_symlink() or path.is_junction()
    except OSError:
        return True


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
        # Remove result payloads and child logs first.  If recursive directory
        # removal itself fails, potentially sensitive native output is still
        # not left behind in the disposable workspace.
        sensitive_files = [
            self.result_path,
            self.root / "batch_stdout.log",
            self.root / "batch_stderr.log",
            self.root / "worker_stdout.log",
            self.root / "worker_stderr.log",
        ]
        worker_results = self.root / "worker" / "results"
        if worker_results.is_dir() and not _is_link_like(worker_results):
            try:
                sensitive_files.extend(worker_results.rglob("*"))
            except OSError:
                pass
        for path in sensitive_files:
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink(missing_ok=True)
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

    Script files and referenced assets are copied as regular files so Ren'Py
    can compile and run them without writing through a shared inode into the
    live game.  Linked archive members are rejected instead of followed.  The
    original game tree is never modified.

    ``workspace_parent`` controls where the disposable root is created.  When
    it is omitted the system temporary directory is used.

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
    root = Path(
        tempfile.mkdtemp(
            prefix=".compose-native-",
            dir=str(parent) if parent else None,
        )
    )
    game_dir = root / "game"
    savedir = root / "savedir"
    config_path = root / "native_config.json"

    try:
        game_dir.mkdir(parents=True)
        savedir.mkdir(parents=True)
        _copy_scripts(source_game, game_dir, script_files)
        _mirror_runtime_assets(source_game, game_dir, archive_dirs)
        _copy_runtime_script(game_dir)
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
    except Exception:
        shutil.rmtree(root, ignore_errors=True)
        raise


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
        if _is_link_like(candidate):
            raise NativeExecutionError(
                f"Native workspace script must not be a symbolic link or "
                f"junction: {candidate}"
            )
        try:
            resolved = candidate.resolve(strict=True)
            script_relative = resolved.relative_to(source_game)
        except (OSError, ValueError) as exc:
            raise NativeExecutionError(
                f"Native workspace script escapes the game root: {candidate}"
            ) from exc
        if resolved in seen:
            continue
        seen.add(resolved)
        target = target_game / script_relative
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
        if _is_link_like(archive):
            raise NativeExecutionError(
                "Native workspace archive root must not be a symbolic link or "
                f"junction: {archive}"
            )
        if not archive.is_dir():
            continue
        try:
            with os.scandir(archive) as scan:
                entries = sorted(scan, key=lambda entry: entry.name)
        except OSError as exc:
            raise NativeExecutionError(
                f"Could not inspect native workspace archive root {archive}: {exc}"
            ) from exc
        for entry in entries:
            child = Path(entry.path)
            if _is_link_like(child):
                raise NativeExecutionError(
                    "Native workspace archive entries must not be symbolic links "
                    f"or junctions: {child}"
                )
            if child.suffix.lower() in _SCRIPT_SUFFIXES:
                continue
            if entry.is_dir(follow_symlinks=False):
                _mirror_tree(child, target_game / child.name)
            elif entry.is_file(follow_symlinks=False):
                _copy_asset(child, target_game / child.name)
            else:
                raise NativeExecutionError(
                    f"Native workspace archive entry is not a regular file or "
                    f"directory: {child}"
                )

    # Audio, fonts, and translations are usually small but may contain many
    # files.  They are still mirrored rather than referenced in place so the
    # disposable project remains self-contained.
    for name in ("audio", "fonts", "tl"):
        source = source_game / name
        if _is_link_like(source):
            raise NativeExecutionError(
                "Native workspace source directory must not be a symbolic link "
                f"or junction: {source}"
            )
        if source.is_dir():
            _mirror_tree(source, target_game / name)


def _mirror_tree(source: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    try:
        with os.scandir(source) as scan:
            entries = sorted(scan, key=lambda entry: entry.name)
    except OSError as exc:
        raise NativeExecutionError(
            f"Could not inspect native workspace asset directory {source}: {exc}"
        ) from exc
    for entry in entries:
        child = Path(entry.path)
        destination = target / child.name
        if _is_link_like(child):
            raise NativeExecutionError(
                "Native workspace asset entries must not be symbolic links or "
                f"junctions: {child}"
            )
        if entry.is_dir(follow_symlinks=False):
            _mirror_tree(child, destination)
        elif entry.is_file(follow_symlinks=False):
            _copy_asset(child, destination)
        else:
            raise NativeExecutionError(
                "Native workspace asset entry is not a regular file or directory: "
                f"{child}"
            )


def _copy_asset(source: Path, target: Path) -> None:
    """Copy one asset into the disposable workspace.

    Asset copies are independent from the live files.  Hard-linking would make
    a write by the game's own Python code modify the live asset through the
    shared inode, violating the workspace isolation boundary.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        return
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
